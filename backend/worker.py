"""
PatchBrain ingestion worker
Run with: python worker.py

This script runs forever, claiming and processing ingestion jobs.
It downloads each file from Supabase Storage, parses it, chunks it,
embeds it, writes chunks back to the database, then runs lightweight
cross-document conflict detection for the company.
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

# Parsers
from parsers.pdf_parser import parse_pdf
from parsers.excel_parser import parse_excel
from parsers.docx_parser import parse_docx
from parsers.whatsapp_parser import parse_whatsapp

# Intelligence
try:
    from intelligence.conflict_detector import run_conflict_detection
except Exception as import_error:
    run_conflict_detection = None
    print(f"⚠ Conflict detector not loaded: {import_error}")

load_dotenv()

WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"
POLL_INTERVAL = int(os.environ.get("WORKER_POLL_INTERVAL", 5))
MAX_RETRIES = int(os.environ.get("WORKER_MAX_RETRIES", 3))
EMBED_BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", 100))
CONFLICT_SCAN_LIMIT = int(os.environ.get("CONFLICT_SCAN_LIMIT", 750))

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
    """Embeds texts in batches. Returns embedding vectors in same order."""
    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i : i + EMBED_BATCH_SIZE]
        response = openai_client.embeddings.create(
            model=os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
            input=batch,
        )
        all_embeddings.extend([item.embedding for item in response.data])
    return all_embeddings


def looks_like_whatsapp_export(text: str) -> bool:
    """Cheap heuristic so normal .txt files still use plain text chunking."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    sample = lines[:80]
    hits = sum(1 for ln in sample if WHATSAPP_LINE_RE.match(ln) and ":" in ln)
    return hits >= 2 or (hits == 1 and len(sample) < 10)


def parse_plain_text(text: str, chunk_size: int = 800, overlap: int = 100) -> list[dict]:
    chunks = []
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

    if ext == ".pdf" or mime_type == "application/pdf":
        return parse_pdf(local_path)

    if ext in (".xlsx", ".xls") or "spreadsheet" in mime_type:
        return parse_excel(local_path)

    if ext == ".docx" or "wordprocessingml" in mime_type:
        return parse_docx(local_path)

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


def fetch_company_chunks_for_conflicts(sb: Client, company_id: str) -> list[dict]:
    """Fetch recent chunks and attach filenames for conflict detection."""
    chunks = sb.table("file_chunks")\
        .select("id,content,source_type,file_version_id,metadata_jsonb,created_at")\
        .eq("company_id", company_id)\
        .order("created_at", desc=True)\
        .limit(CONFLICT_SCAN_LIMIT)\
        .execute().data or []

    if not chunks:
        return []

    fv_ids = list({c.get("file_version_id") for c in chunks if c.get("file_version_id")})
    filename_map: dict[str, str] = {}
    if fv_ids:
        versions = sb.table("file_versions")\
            .select("id,original_filename")\
            .in_("id", fv_ids)\
            .execute().data or []
        filename_map = {v["id"]: v.get("original_filename", "unknown") for v in versions}

    for c in chunks:
        c["filename"] = filename_map.get(c.get("file_version_id"), "unknown")

    return chunks


def normalize_conflict_for_insert(conflict: dict, company_id: str) -> dict:
    files = conflict.get("files")
    if not files:
        files = list(set((conflict.get("files_a") or []) + (conflict.get("files_b") or [])))

    return {
        "company_id": company_id,
        "conflict_type": conflict.get("type", "unknown"),
        "severity": conflict.get("severity", "medium"),
        "description": conflict.get("description", ""),
        "detail": conflict.get("detail", ""),
        "files": files or [],
        "chunk_ids": conflict.get("chunk_ids") or [],
        "metadata_jsonb": conflict,
        "status": "open",
    }


def run_and_store_conflicts(sb: Client, company_id: str):
    """
    Recomputes lightweight company-level conflicts.
    Non-critical by design: if this fails, ingestion should still succeed.
    """
    if run_conflict_detection is None:
        return

    try:
        chunks = fetch_company_chunks_for_conflicts(sb, company_id)
        conflicts = run_conflict_detection(chunks)

        # Avoid duplicate rows every time a file is ingested.
        # For v1, recompute the open conflict snapshot for the company.
        sb.table("detected_conflicts").delete().eq("company_id", company_id).execute()

        if not conflicts:
            print("  Conflict detector: 0 conflicts")
            return

        rows = [normalize_conflict_for_insert(c, company_id) for c in conflicts]
        for i in range(0, len(rows), 50):
            sb.table("detected_conflicts").insert(rows[i : i + 50]).execute()

        print(f"  Conflict detector: stored {len(rows)} conflicts")

    except Exception as e:
        # Do not fail file ingestion because the intelligence layer failed.
        print(f"  ⚠ Conflict detection skipped/failed: {e}")


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

    print(f"  Got {len(chunks)} chunks, embedding now")

    update_file_status(sb, version_id, "embedding")
    texts = [c["content"] for c in chunks]
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
            "embedding": embedding,
            "token_count": len(chunk["content"].split()),
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

    # Current file version should be set before conflict detection reads across chunks.
    sb.table("files")\
        .update({"current_file_version_id": version_id})\
        .eq("id", file_id)\
        .execute()

    # Run non-blocking-ish intelligence step before marking job done.
    # It catches its own errors, so ingestion still completes.
    run_and_store_conflicts(sb, str(company_id))

    update_file_status(sb, version_id, "ready")

    sb.table("processing_jobs")\
        .update({"status": "done", "updated_at": "now()"})\
        .eq("id", job["id"])\
        .execute()

    print(f"  ✓ Done — {len(chunks)} chunks ready for search")


def run_worker():
    print(f"PatchBrain worker starting — ID: {WORKER_ID}")
    print(f"Polling every {POLL_INTERVAL}s")

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
