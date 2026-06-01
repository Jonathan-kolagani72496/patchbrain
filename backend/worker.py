"""
PatchBrain ingestion worker
Run with: python worker.py

This script runs forever, claiming and processing ingestion jobs.
It downloads each file from Supabase Storage, parses it, chunks it,
optionally contextualizes chunks, embeds them, and writes chunks back to the database.

Conflict detection is intentionally disabled in this worker version.
"""

import os
import sys
import time
import uuid
import tempfile
import traceback
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from supabase import create_client, Client
from openai import OpenAI
import anthropic

# Parsers
from parsers.pdf_parser import parse_pdf
from parsers.excel_parser import parse_excel
from parsers.csv_parser import parse_csv
from parsers.docx_parser import parse_docx
from parsers.whatsapp_parser import parse_whatsapp

# Intelligence pass — runs after chunks are inserted, non-fatal
from intel_pass import run_intelligence_pass

load_dotenv()

WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"
POLL_INTERVAL = int(os.environ.get("WORKER_POLL_INTERVAL", 5))
MAX_RETRIES = int(os.environ.get("WORKER_MAX_RETRIES", 3))
EMBED_BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", 100))

# Contextual retrieval controls.
# Keep disabled unless explicitly enabled so deploys do not unexpectedly increase Anthropic spend.
ENABLE_CONTEXTUAL_RETRIEVAL = os.environ.get("ENABLE_CONTEXTUAL_RETRIEVAL", "false").lower() in {"1", "true", "yes", "on"}
CONTEXTUALIZATION_MODEL = os.environ.get("CONTEXTUALIZATION_MODEL", "claude-haiku-4-5-20251001")
CONTEXTUALIZATION_MAX_CHUNKS = int(os.environ.get("CONTEXTUALIZATION_MAX_CHUNKS", 120))
CONTEXTUALIZATION_DOC_SAMPLE_CHARS = int(os.environ.get("CONTEXTUALIZATION_DOC_SAMPLE_CHARS", 12000))
CONTEXTUALIZATION_CHUNK_CHARS = int(os.environ.get("CONTEXTUALIZATION_CHUNK_CHARS", 2400))
CONTEXTUALIZATION_MAX_TOKENS = int(os.environ.get("CONTEXTUALIZATION_MAX_TOKENS", 90))
CONTEXTUALIZATION_SLEEP_MS = int(os.environ.get("CONTEXTUALIZATION_SLEEP_MS", 0))

WHATSAPP_LINE_RE = re.compile(
    r"^(?:"
    r"\[\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}"
    r"|\[?\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}"
    r")"
)


def get_supabase() -> Client:
    """Supabase service client. Supports either env var naming convention."""
    service_key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not service_key:
        raise RuntimeError("Missing SUPABASE_SERVICE_KEY or SUPABASE_SERVICE_ROLE_KEY")
    return create_client(os.environ["SUPABASE_URL"], service_key)


openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY")) if os.environ.get("ANTHROPIC_API_KEY") else None


def claim_next_job(sb: Client) -> dict | None:
    """Claims the next queued job via the claim_next_job SQL function."""
    result = sb.rpc("claim_next_job", {"p_worker_id": WORKER_ID}).execute()
    return result.data[0] if result.data else None


def update_file_status(sb: Client, version_id: str, status: str, error: str | None = None):
    update: dict[str, Any] = {"status": status}

    if error:
        update["error_message"] = error[:1000]

    if status in ("parsing", "embedding"):
        update["started_at"] = "now()"

    if status in ("ready", "failed"):
        update["finished_at"] = "now()"
        update["worker_id"] = WORKER_ID

    sb.table("file_versions").update(update).eq("id", version_id).execute()


def embed_chunks(texts: list[str]) -> list[list[float]]:
    """Embeds texts in batches. Returns embedding vectors in the same order."""
    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i : i + EMBED_BATCH_SIZE]
        response = openai_client.embeddings.create(
            model=os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
            input=batch,
        )
        all_embeddings.extend([item.embedding for item in response.data])

    return all_embeddings



def _truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def build_document_context(chunks: list[dict], original_filename: str, mime_type: str) -> str:
    """Build a compact document-level context string for chunk contextualization.

    This is intentionally deterministic and cheap. Claude sees this plus one chunk
    and writes a short retrieval-oriented summary. We do not store this as source text.
    """
    source_types = sorted({str(c.get("source_type") or "unknown") for c in chunks})
    sheets = sorted({str(c.get("sheet_name")) for c in chunks if c.get("sheet_name")})
    pages = [c.get("page_number") for c in chunks if c.get("page_number") is not None]

    metadata_lines = [
        f"Filename: {original_filename}",
        f"MIME type: {mime_type or 'unknown'}",
        f"Chunk count: {len(chunks)}",
        f"Source types: {', '.join(source_types) if source_types else 'unknown'}",
    ]
    if sheets:
        metadata_lines.append(f"Sheets: {', '.join(sheets[:20])}")
    if pages:
        metadata_lines.append(f"Page range: {min(pages)}-{max(pages)}")

    sample_parts: list[str] = []
    used = 0
    for i, c in enumerate(chunks[:25], 1):
        content = _truncate(c.get("content") or "", 700)
        if not content:
            continue
        label_parts = [f"sample chunk {i}", str(c.get("source_type") or "unknown")]
        if c.get("sheet_name"):
            label_parts.append(f"sheet={c.get('sheet_name')}")
        if c.get("page_number") is not None:
            label_parts.append(f"page={c.get('page_number')}")
        if c.get("row_start") is not None or c.get("row_end") is not None:
            label_parts.append(f"rows={c.get('row_start')}-{c.get('row_end')}")
        block = f"[{'; '.join(label_parts)}]\n{content}"
        if used + len(block) > CONTEXTUALIZATION_DOC_SAMPLE_CHARS:
            break
        sample_parts.append(block)
        used += len(block)

    return "\n".join(metadata_lines) + "\n\nDocument sample:\n" + "\n\n---\n\n".join(sample_parts)


def _chunk_location(chunk: dict) -> str:
    parts = [str(chunk.get("source_type") or "unknown")]
    if chunk.get("page_number") is not None:
        parts.append(f"page {chunk.get('page_number')}")
    if chunk.get("sheet_name"):
        parts.append(f"sheet {chunk.get('sheet_name')}")
    if chunk.get("row_start") is not None or chunk.get("row_end") is not None:
        parts.append(f"rows {chunk.get('row_start')}-{chunk.get('row_end')}")
    md = chunk.get("metadata_jsonb") or {}
    if isinstance(md, dict):
        if md.get("heading"):
            parts.append(f"section {md.get('heading')}")
        if md.get("sender"):
            parts.append(f"sender {md.get('sender')}")
        if md.get("timestamp_start"):
            parts.append(f"from {md.get('timestamp_start')}")
    return ", ".join(parts)


def contextualize_chunk(chunk: dict, document_context: str, original_filename: str) -> dict:
    """Return contextual_summary, embedding_text, and error for a single chunk.

    Failure is non-fatal. The worker will embed original content if contextualization
    fails or is disabled.
    """
    original_content = chunk.get("content") or ""
    if not ENABLE_CONTEXTUAL_RETRIEVAL:
        return {
            "contextual_summary": None,
            "embedding_text": original_content,
            "contextualization_model": None,
            "contextualization_error": None,
        }

    if anthropic_client is None:
        return {
            "contextual_summary": None,
            "embedding_text": original_content,
            "contextualization_model": None,
            "contextualization_error": "ANTHROPIC_API_KEY missing; contextual retrieval skipped",
        }

    try:
        instructions = (
            "You are improving retrieval for an oilfield document RAG system. "
            "Given cached document context and one chunk, write retrieval context for the chunk. "
            "Mention concrete identifiers if present: invoice number, ticket number, truck ID, driver, "
            "vendor, customer, well/lease, job number, date, sheet, page, or row range. "
            "Do not answer questions. Do not invent facts. "
            "Output exactly one sentence, starting directly with the content description. "
            "No labels, bullets, prefixes, or explanations."
        )
        user_prompt = (
            f"Chunk location: {_chunk_location(chunk)}\n\n"
            f"Chunk text:\n{_truncate(original_content, CONTEXTUALIZATION_CHUNK_CHARS)}"
        )

        # Cache the repeated document context across all chunk-contextualization
        # calls for this file. The chunk text stays uncached/dynamic.
        response = anthropic_client.messages.create(
            model=CONTEXTUALIZATION_MODEL,
            max_tokens=CONTEXTUALIZATION_MAX_TOKENS,
            temperature=0,
            system=[
                {"type": "text", "text": instructions},
                {
                    "type": "text",
                    "text": f"Document context:\n{document_context}",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )
        summary = " ".join((response.content[0].text or "").strip().strip('"').split())
        if not summary:
            return {
                "contextual_summary": None,
                "embedding_text": original_content,
                "contextualization_model": CONTEXTUALIZATION_MODEL,
                "contextualization_error": "empty contextual summary",
            }

        # Keep artificial context out of citations by storing it separately and
        # embedding this combined text only.
        embedding_text = f"{summary}\n\n{original_content}"
        return {
            "contextual_summary": summary,
            "embedding_text": embedding_text,
            "contextualization_model": CONTEXTUALIZATION_MODEL,
            "contextualization_error": None,
        }
    except Exception as e:
        return {
            "contextual_summary": None,
            "embedding_text": original_content,
            "contextualization_model": CONTEXTUALIZATION_MODEL,
            "contextualization_error": str(e)[:500],
        }


def contextualize_chunks(chunks: list[dict], original_filename: str, mime_type: str) -> list[dict]:
    """Attach contextual retrieval fields to chunks.

    If a document has more chunks than CONTEXTUALIZATION_MAX_CHUNKS, only the first
    N are contextualized. The rest still embed original content and process normally.
    This protects cost and prevents one massive file from stalling the worker.
    """
    if not chunks:
        return chunks

    document_context = build_document_context(chunks, original_filename, mime_type)
    out: list[dict] = []

    if ENABLE_CONTEXTUAL_RETRIEVAL and len(chunks) > CONTEXTUALIZATION_MAX_CHUNKS:
        print(
            f"  Warning: {len(chunks)} chunks found; contextualizing first "
            f"{CONTEXTUALIZATION_MAX_CHUNKS} and embedding the rest without generated context"
        )

    for i, chunk in enumerate(chunks):
        enriched = dict(chunk)

        if ENABLE_CONTEXTUAL_RETRIEVAL and i >= CONTEXTUALIZATION_MAX_CHUNKS:
            ctx = {
                "contextual_summary": None,
                "embedding_text": chunk.get("content") or "",
                "contextualization_model": CONTEXTUALIZATION_MODEL,
                "contextualization_error": f"skipped: exceeds per-file contextualization limit ({CONTEXTUALIZATION_MAX_CHUNKS})",
            }
        else:
            ctx = contextualize_chunk(chunk, document_context, original_filename)

        enriched.update(ctx)
        out.append(enriched)

        if CONTEXTUALIZATION_SLEEP_MS > 0 and ENABLE_CONTEXTUAL_RETRIEVAL:
            time.sleep(CONTEXTUALIZATION_SLEEP_MS / 1000)

    return out



def looks_like_whatsapp_export(text: str) -> bool:
    """Cheap heuristic so normal .txt files still use plain text chunking."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False

    sample = lines[:80]
    hits = sum(1 for ln in sample if WHATSAPP_LINE_RE.match(ln) and ":" in ln)
    return hits >= 2 or (hits == 1 and len(sample) < 10)


def parse_plain_text(text: str, chunk_size: int = 800, overlap: int = 100) -> list[dict]:
    chunks: list[dict] = []
    step = max(1, chunk_size - overlap)

    for i in range(0, len(text), step):
        piece = text[i : i + chunk_size].strip()
        if not piece:
            continue

        chunks.append({
            "content": piece,
            "source_type": "txt_chunk",
            "page_number": None,
            "sheet_name": None,
            "row_start": None,
            "row_end": None,
            "bbox_json": None,
            "metadata_jsonb": {"char_start": i, "char_end": i + len(piece)},
        })

    return chunks


def parse_file(local_path: str, original_filename: str, mime_type: str) -> list[dict]:
    ext = Path(original_filename).suffix.lower()
    mime_type = mime_type or ""

    if ext == ".pdf" or mime_type == "application/pdf":
        return parse_pdf(local_path)

    if ext in (".xlsx", ".xls") or "spreadsheet" in mime_type:
        return parse_excel(local_path)

    if ext == ".docx" or "wordprocessingml" in mime_type:
        return parse_docx(local_path)

    if ext == ".csv" or "text/csv" in mime_type or "csv" in mime_type:
        return parse_csv(local_path)

    if ext == ".txt" or mime_type == "text/plain":
        with open(local_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()

        if looks_like_whatsapp_export(text):
            print("  Detected WhatsApp export")
            whatsapp_chunks = parse_whatsapp(local_path)
            if whatsapp_chunks:
                return whatsapp_chunks
            print("  WhatsApp parser returned no chunks; falling back to plain text")

        return parse_plain_text(text)

    raise ValueError(f"Unsupported file type: {ext} ({mime_type})")


def process_job(sb: Client, job: dict):
    """Full ingestion pipeline for one file version."""
    version_id = job["file_version_id"]
    company_id = job["company_id"]

    print(f"[{WORKER_ID}] Processing version {version_id}")

    version = sb.table("file_versions")\
        .select("*")\
        .eq("id", version_id)\
        .single()\
        .execute().data

    if not version:
        raise ValueError(f"File version {version_id} not found")

    file_id = version["file_id"]
    storage_path = version["storage_path"]
    mime_type = version.get("mime_type", "") or ""
    original_filename = version["original_filename"]

    update_file_status(sb, version_id, "parsing")

    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = os.path.join(tmpdir, original_filename)

        print(f"  Downloading {storage_path}")
        file_bytes = sb.storage.from_("company-files").download(storage_path)

        with open(local_path, "wb") as f:
            f.write(file_bytes)

        print(f"  Parsing {Path(original_filename).suffix.lower()} file")
        chunks = parse_file(local_path, original_filename, mime_type)

    if not chunks:
        raise ValueError("Parser returned no chunks — file may be empty or unreadable")

    print(f"  Got {len(chunks)} chunks")

    if ENABLE_CONTEXTUAL_RETRIEVAL:
        print(f"  Contextual retrieval enabled — contextualizing up to {CONTEXTUALIZATION_MAX_CHUNKS} chunks")
    chunks = contextualize_chunks(chunks, original_filename, mime_type)

    print("  Embedding chunks")
    update_file_status(sb, version_id, "embedding")
    texts = [c.get("embedding_text") or c["content"] for c in chunks]
    embeddings = embed_chunks(texts)

    print("  Writing chunks to database")
    rows = []

    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        rows.append({
            "company_id": company_id,
            "file_id": file_id,
            "file_version_id": version_id,
            "chunk_index": i,
            "content": chunk["content"],
            "embedding_text": chunk.get("embedding_text") or chunk["content"],
            "contextual_summary": chunk.get("contextual_summary"),
            "contextualized_at": "now()" if chunk.get("contextual_summary") else None,
            "contextualization_model": chunk.get("contextualization_model"),
            "contextualization_error": chunk.get("contextualization_error"),
            "embedding": embedding,
            "token_count": len((chunk.get("embedding_text") or chunk["content"]).split()),
            "source_type": chunk["source_type"],
            "page_number": chunk.get("page_number"),
            "sheet_name": chunk.get("sheet_name"),
            "row_start": chunk.get("row_start"),
            "row_end": chunk.get("row_end"),
            "bbox_json": chunk.get("bbox_json"),
            "metadata_jsonb": chunk.get("metadata_jsonb", {}),
        })

    for i in range(0, len(rows), 50):
        sb.table("file_chunks").insert(rows[i : i + 50]).execute()

    sb.table("files")\
        .update({"current_file_version_id": version_id})\
        .eq("id", file_id)\
        .execute()

    update_file_status(sb, version_id, "ready")

    # ── Intelligence pass (non-fatal) ─────────────────────────────────────────
    # Runs after chunks are committed and version is 'ready'.
    # Failures here do NOT roll back the job or change the file status.
    run_intelligence_pass(
        sb=sb,
        version_id=version_id,
        file_id=file_id,
        company_id=company_id,
        chunks=chunks,
        original_filename=original_filename,
        version_status="ready",
    )
    # ─────────────────────────────────────────────────────────────────────────

    sb.table("processing_jobs")\
        .update({"status": "done", "updated_at": "now()"})\
        .eq("id", job["id"])\
        .execute()

    print(f"  ✓ Done — {len(chunks)} chunks ready for search")


def run_worker():
    print(f"PatchBrain worker starting — ID: {WORKER_ID}")
    print(f"Polling every {POLL_INTERVAL}s")
    print(f"ENABLE_CONTEXTUAL_RETRIEVAL={ENABLE_CONTEXTUAL_RETRIEVAL}")
    print(f"CONTEXTUALIZATION_MODEL={CONTEXTUALIZATION_MODEL}")
    print(f"ANTHROPIC_API_KEY_PRESENT={bool(os.environ.get('ANTHROPIC_API_KEY'))}")

    sb = get_supabase()

    while True:
        try:
            job = claim_next_job(sb)

            if job is None:
                time.sleep(POLL_INTERVAL)
                continue

            try:
                process_job(sb, job)
            except Exception as e:
                error_msg = traceback.format_exc()
                print(f"  ✗ Job failed: {e}")
                print(error_msg)

                version_id = job["file_version_id"]
                retry_count = job.get("retry_count", 0)

                if retry_count < MAX_RETRIES:
                    sb.table("processing_jobs").update({
                        "status": "queued",
                        "retry_count": retry_count + 1,
                        "worker_id": None,
                        "locked_at": None,
                        "error_message": str(e)[:500],
                        "updated_at": "now()",
                    }).eq("id", job["id"]).execute()
                    update_file_status(sb, version_id, "queued", str(e)[:500])
                else:
                    sb.table("processing_jobs").update({
                        "status": "failed",
                        "error_message": str(e)[:500],
                        "updated_at": "now()",
                    }).eq("id", job["id"]).execute()
                    update_file_status(sb, version_id, "failed", str(e)[:500])

        except KeyboardInterrupt:
            print("\nWorker stopped.")
            sys.exit(0)
        except Exception as e:
            print(f"Worker loop error: {e}")
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run_worker()
