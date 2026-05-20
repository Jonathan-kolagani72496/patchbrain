"""
PatchBrain ingestion worker
Run with: python worker.py

This script runs forever, claiming and processing ingestion jobs.
It downloads each file from Supabase Storage, parses it, chunks it,
embeds it, and writes the chunks back to the database.
"""

import os
import sys
import time
import uuid
import tempfile
import traceback
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client, Client
from openai import OpenAI

# Parsers
from parsers.pdf_parser import parse_pdf
from parsers.excel_parser import parse_excel
from parsers.docx_parser import parse_docx

load_dotenv()

WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"
POLL_INTERVAL = int(os.environ.get("WORKER_POLL_INTERVAL", 5))
MAX_RETRIES = int(os.environ.get("WORKER_MAX_RETRIES", 3))
EMBED_BATCH_SIZE = 100

def get_supabase() -> Client:
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )

openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def claim_next_job(sb: Client) -> dict | None:
    """
    Claims the next queued job using a Postgres advisory approach.
    Returns the job row or None if queue is empty.
    """
    # Use a raw query with FOR UPDATE SKIP LOCKED to safely claim one job
    result = sb.rpc("claim_next_job", {"p_worker_id": WORKER_ID}).execute()
    return result.data[0] if result.data else None


def update_file_status(sb: Client, version_id: str, status: str, error: str = None):
    update = {"status": status}
    if error:
        update["error_message"] = error[:1000]
    if status in ("parsing", "embedding"):
        update["started_at"] = "now()"
    if status in ("ready", "failed"):
        update["finished_at"] = "now()"
        update["worker_id"] = WORKER_ID
    sb.table("file_versions").update(update).eq("id", version_id).execute()


def embed_chunks(texts: list[str]) -> list[list[float]]:
    """Embeds a list of texts in batches. Returns list of embedding vectors."""
    all_embeddings = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i : i + EMBED_BATCH_SIZE]
        response = openai_client.embeddings.create(
            model="text-embedding-3-small",
            input=batch,
        )
        all_embeddings.extend([item.embedding for item in response.data])
    return all_embeddings


def process_job(sb: Client, job: dict):
    """
    Full ingestion pipeline for one file version.
    """
    version_id = job["file_version_id"]
    company_id = job["company_id"]

    print(f"[{WORKER_ID}] Processing version {version_id}")

    # ── Get file version details ──
    version = sb.table("file_versions")\
        .select("*")\
        .eq("id", version_id)\
        .single()\
        .execute().data

    if not version:
        raise ValueError(f"File version {version_id} not found")

    file_id = version["file_id"]
    storage_path = version["storage_path"]
    mime_type = version.get("mime_type", "")
    original_filename = version["original_filename"]
    ext = Path(original_filename).suffix.lower()

    # ── Download file to temp dir ──
    update_file_status(sb, version_id, "parsing")

    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = os.path.join(tmpdir, original_filename)

        print(f"  Downloading {storage_path}")
        file_bytes = sb.storage.from_("company-files")\
            .download(storage_path)

        with open(local_path, "wb") as f:
            f.write(file_bytes)

        # ── Parse into chunks ──
        print(f"  Parsing {ext} file")
        chunks = []

        if ext == ".pdf" or mime_type == "application/pdf":
            chunks = parse_pdf(local_path)
        elif ext in (".xlsx", ".xls") or "spreadsheet" in mime_type:
            chunks = parse_excel(local_path)
        elif ext == ".docx" or "wordprocessingml" in mime_type:
            chunks = parse_docx(local_path)
        elif ext == ".txt" or mime_type == "text/plain":
            with open(local_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            # Simple fixed-size chunks for plain text
            chunk_size = 800
            for i in range(0, len(text), chunk_size - 100):
                chunks.append({
                    "content": text[i : i + chunk_size],
                    "source_type": "txt_chunk",
                    "page_number": None,
                    "sheet_name": None,
                    "row_start": None,
                    "row_end": None,
                    "metadata_jsonb": {},
                })
        else:
            raise ValueError(f"Unsupported file type: {ext} ({mime_type})")

    if not chunks:
        raise ValueError("Parser returned no chunks — file may be empty or unreadable")

    print(f"  Got {len(chunks)} chunks, embedding now")

    # ── Embed all chunks ──
    update_file_status(sb, version_id, "embedding")
    texts = [c["content"] for c in chunks]
    embeddings = embed_chunks(texts)

    # ── Write chunks to database ──
    print(f"  Writing chunks to database")
    rows = []
    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        rows.append({
            "company_id": company_id,
            "file_id": file_id,
            "file_version_id": version_id,
            "chunk_index": i,
            "content": chunk["content"],
            "embedding": embedding,
            "token_count": len(chunk["content"].split()),  # rough estimate
            "source_type": chunk["source_type"],
            "page_number": chunk.get("page_number"),
            "sheet_name": chunk.get("sheet_name"),
            "row_start": chunk.get("row_start"),
            "row_end": chunk.get("row_end"),
            "bbox_json": chunk.get("bbox_json"),
            "metadata_jsonb": chunk.get("metadata_jsonb", {}),
        })

    # Insert in batches of 50 to avoid request size limits
    for i in range(0, len(rows), 50):
        sb.table("file_chunks").insert(rows[i : i + 50]).execute()

    # ── Mark ready ──
    update_file_status(sb, version_id, "ready")

    # Update files.current_file_version_id
    sb.table("files")\
        .update({"current_file_version_id": version_id})\
        .eq("id", file_id)\
        .execute()

    # Mark job done
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
                    # Requeue with incremented retry count
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