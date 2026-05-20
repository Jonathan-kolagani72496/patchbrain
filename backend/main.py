"""
PatchBrain FastAPI backend - v2
- Persistent conversation sessions (multiple chats per user like ChatGPT)
- Rolling summary memory (token-efficient, never loses context)
- Anthropic prompt caching (90% discount on system prompt)
- Filename in every source block
- Chunk preview in citations
- Proper billing error messages
- /me endpoint for company name
"""

import os
import uuid
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from supabase import create_client, Client
from openai import OpenAI
import anthropic

load_dotenv()

app = FastAPI(title="PatchBrain API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://patchbrain-dv999vhsf-jonathan-kolagani-s-projects.vercel.app",
        "https://patchbrain.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_supabase() -> Client:
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
claude_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# ── Auth ───────────────────────────────────────────────────────────────────────

def get_current_user(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    sb = get_supabase()
    try:
        result = sb.auth.get_user(token)
        if not result or not result.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        user_id = result.user.id
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Auth error: {str(e)}")
    row = sb.table("users").select("*, companies(name, slug)").eq("id", user_id).single().execute()
    if not row.data:
        raise HTTPException(status_code=403, detail=f"User not in users table: {user_id}")
    return row.data

# ── /me ────────────────────────────────────────────────────────────────────────

@app.get("/me")
def get_me(user=Depends(get_current_user)):
    company = user.get("companies") or {}
    return {
        "user_id":      user["id"],
        "email":        user["email"],
        "role":         user["role"],
        "company_id":   user["company_id"],
        "company_name": company.get("name", "Your Company"),
        "company_slug": company.get("slug", ""),
    }

# ── Upload ─────────────────────────────────────────────────────────────────────

class UploadRequestBody(BaseModel):
    filename:   str
    mime_type:  str
    size_bytes: int

@app.post("/upload/request")
def request_upload(body: UploadRequestBody, user=Depends(get_current_user)):
    sb             = get_supabase()
    company_id     = user["company_id"]
    file_id        = str(uuid.uuid4())
    file_version_id = str(uuid.uuid4())
    storage_path   = f"companies/{company_id}/files/{file_id}/{file_version_id}/{body.filename}"

    sb.table("files").insert({
        "id": file_id, "company_id": company_id, "logical_name": body.filename,
    }).execute()
    sb.table("file_versions").insert({
        "id": file_version_id, "file_id": file_id, "company_id": company_id,
        "storage_path": storage_path, "original_filename": body.filename,
        "mime_type": body.mime_type, "size_bytes": body.size_bytes,
        "status": "uploaded", "created_by": user["id"],
    }).execute()

    signed = sb.storage.from_("company-files").create_signed_upload_url(storage_path)
    return {
        "file_id":          file_id,
        "file_version_id":  file_version_id,
        "upload_url":       signed.get("signedURL") or signed.get("signedUrl") or signed.get("signed_url"),
        "upload_token":     signed.get("token"),
        "storage_path":     storage_path,
    }

class ConfirmUploadBody(BaseModel):
    file_version_id: str

@app.post("/upload/confirm")
def confirm_upload(body: ConfirmUploadBody, user=Depends(get_current_user)):
    sb = get_supabase()
    version = sb.table("file_versions").select("*")\
        .eq("id", body.file_version_id).eq("company_id", user["company_id"])\
        .single().execute()
    if not version.data:
        raise HTTPException(status_code=404, detail="File version not found")
    sb.table("file_versions").update({"status": "queued"})\
        .eq("id", body.file_version_id).execute()
    job_id = str(uuid.uuid4())
    sb.table("processing_jobs").insert({
        "id": job_id, "company_id": user["company_id"],
        "file_version_id": body.file_version_id,
        "job_type": "ingest", "status": "queued", "priority": 5,
    }).execute()
    return {"job_id": job_id, "status": "queued"}

# ── Files ──────────────────────────────────────────────────────────────────────

@app.get("/files")
def list_files(user=Depends(get_current_user)):
    sb = get_supabase()
    files = sb.table("files")\
        .select("*, file_versions(id, status, original_filename, created_at, size_bytes, error_message)")\
        .eq("company_id", user["company_id"]).is_("deleted_at", "null")\
        .order("created_at", desc=True).execute()
    return files.data

class FileIdsBody(BaseModel):
    file_ids: List[str]


@app.post("/files/delete")
def delete_files(body: FileIdsBody, user=Depends(get_current_user)):
    """
    Hard-deletes selected files:
    - Supabase Storage objects
    - citations linked to chunks
    - file_chunks
    - processing_jobs
    - file_versions
    - files
    """
    sb = get_supabase()
    company_id = user["company_id"]

    file_ids = list({fid for fid in body.file_ids if fid})
    if not file_ids:
        return {"deleted_count": 0}

    # Verify files belong to user's company
    owned_files = sb.table("files")\
        .select("id")\
        .eq("company_id", company_id)\
        .in_("id", file_ids)\
        .execute().data or []

    owned_file_ids = [f["id"] for f in owned_files]

    if not owned_file_ids:
        raise HTTPException(status_code=404, detail="No matching files found")

    # Get versions + storage paths
    versions = sb.table("file_versions")\
        .select("id, file_id, storage_bucket, storage_path, status")\
        .eq("company_id", company_id)\
        .in_("file_id", owned_file_ids)\
        .execute().data or []

    version_ids = [v["id"] for v in versions]
    storage_items = [
        {
            "bucket": v.get("storage_bucket") or "company-files",
            "path": v["storage_path"],
        }
        for v in versions
        if v.get("storage_path")
    ]

    # Avoid deleting files while actively being processed
    active_statuses = {"parsing", "chunking", "embedding"}
    active = [v for v in versions if v.get("status") in active_statuses]
    if active:
        raise HTTPException(
            status_code=409,
            detail="One or more files are currently processing. Wait until processing finishes, then delete."
        )

    # Remove storage objects first
    paths_by_bucket = {}
    for item in storage_items:
        paths_by_bucket.setdefault(item["bucket"], []).append(item["path"])

    for bucket, paths in paths_by_bucket.items():
        if paths:
            sb.storage.from_(bucket).remove(paths)

    # Delete citations that point at chunks from these files
    chunks = sb.table("file_chunks")\
        .select("id")\
        .eq("company_id", company_id)\
        .in_("file_id", owned_file_ids)\
        .execute().data or []

    chunk_ids = [c["id"] for c in chunks]

    if chunk_ids:
        # Batch in case there are many chunks
        for i in range(0, len(chunk_ids), 100):
            sb.table("citations")\
                .delete()\
                .in_("chunk_id", chunk_ids[i:i + 100])\
                .execute()

    # Delete jobs
    if version_ids:
        sb.table("processing_jobs")\
            .delete()\
            .eq("company_id", company_id)\
            .in_("file_version_id", version_ids)\
            .execute()

    # Delete chunks
    sb.table("file_chunks")\
        .delete()\
        .eq("company_id", company_id)\
        .in_("file_id", owned_file_ids)\
        .execute()

    # Delete versions
    sb.table("file_versions")\
        .delete()\
        .eq("company_id", company_id)\
        .in_("file_id", owned_file_ids)\
        .execute()

    # Delete files
    sb.table("files")\
        .delete()\
        .eq("company_id", company_id)\
        .in_("id", owned_file_ids)\
        .execute()

    return {
        "deleted_count": len(owned_file_ids),
        "deleted_file_ids": owned_file_ids,
    }


@app.post("/files/download-url")
def get_download_urls(body: FileIdsBody, user=Depends(get_current_user)):
    """
    Returns signed download URLs for selected files.
    """
    sb = get_supabase()
    company_id = user["company_id"]

    file_ids = list({fid for fid in body.file_ids if fid})
    if not file_ids:
        return {"files": []}

    files = sb.table("files")\
        .select("id, logical_name, current_file_version_id")\
        .eq("company_id", company_id)\
        .in_("id", file_ids)\
        .execute().data or []

    if not files:
        raise HTTPException(status_code=404, detail="No matching files found")

    current_version_ids = [
        f["current_file_version_id"]
        for f in files
        if f.get("current_file_version_id")
    ]

    versions = []
    if current_version_ids:
        versions = sb.table("file_versions")\
            .select("id, file_id, storage_bucket, storage_path, original_filename")\
            .eq("company_id", company_id)\
            .in_("id", current_version_ids)\
            .execute().data or []

    # Fallback: if current version missing, use latest version per file
    found_file_ids = {v["file_id"] for v in versions}
    missing_file_ids = [f["id"] for f in files if f["id"] not in found_file_ids]

    if missing_file_ids:
        fallback_versions = sb.table("file_versions")\
            .select("id, file_id, storage_bucket, storage_path, original_filename, created_at")\
            .eq("company_id", company_id)\
            .in_("file_id", missing_file_ids)\
            .order("created_at", desc=True)\
            .execute().data or []

        seen = set()
        for v in fallback_versions:
            if v["file_id"] not in seen:
                versions.append(v)
                seen.add(v["file_id"])

    results = []

    for v in versions:
        bucket = v.get("storage_bucket") or "company-files"
        signed = sb.storage.from_(bucket).create_signed_url(v["storage_path"], 3600)

        results.append({
            "file_id": v["file_id"],
            "file_version_id": v["id"],
            "filename": v.get("original_filename") or "download",
            "url": signed.get("signedURL") or signed.get("signedUrl") or signed.get("signed_url"),
        })

    return {"files": results}

# ── Conversations ──────────────────────────────────────────────────────────────

@app.get("/conversations")
def list_conversations(user=Depends(get_current_user)):
    sb = get_supabase()
    result = sb.table("conversation_sessions")\
        .select("id, title, message_count, created_at, updated_at")\
        .eq("user_id", user["id"])\
        .order("updated_at", desc=True).limit(50).execute()
    return result.data

@app.post("/conversations")
def create_conversation(user=Depends(get_current_user)):
    sb = get_supabase()
    session_id = str(uuid.uuid4())
    sb.table("conversation_sessions").insert({
        "id": session_id, "company_id": user["company_id"],
        "user_id": user["id"], "title": "New conversation",
    }).execute()
    return {"session_id": session_id, "title": "New conversation"}

@app.get("/conversations/{session_id}/messages")
def get_messages(session_id: str, user=Depends(get_current_user)):
    sb = get_supabase()
    session = sb.table("conversation_sessions")\
        .select("*").eq("id", session_id).eq("user_id", user["id"])\
        .single().execute()
    if not session.data:
        raise HTTPException(status_code=404, detail="Session not found")
    messages = sb.table("conversation_messages")\
        .select("*").eq("session_id", session_id)\
        .order("created_at", asc=True).execute()
    return {"session": session.data, "messages": messages.data}

@app.delete("/conversations/{session_id}")
def delete_conversation(session_id: str, user=Depends(get_current_user)):
    sb = get_supabase()
    sb.table("conversation_sessions")\
        .delete().eq("id", session_id).eq("user_id", user["id"]).execute()
    return {"deleted": True}

# ── Ask ────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are PatchBrain, an AI assistant built specifically for oilfield service companies.
You answer questions ONLY from the provided source documents.

Rules:
- Cite every factual claim with the source tag [S1], [S2] etc.
- Always mention the filename the answer came from.
- If two sources conflict, say so explicitly.
- If sources don't contain enough to answer confidently, say so clearly.
- Never invent numbers, dates, names, or ticket numbers.
- For calculations show your work step by step.
- Keep answers direct and concise — oilfield operators are busy.
- When the user refers to something from earlier in the conversation, use the conversation history provided."""

class AskBody(BaseModel):
    question:   str
    session_id: Optional[str] = None

@app.post("/ask")
def ask(body: AskBody, user=Depends(get_current_user)):
    sb         = get_supabase()
    company_id = user["company_id"]
    question   = body.question.strip()

    # ── 1. Get or create session ──
    session_id = body.session_id
    if not session_id:
        session_id = str(uuid.uuid4())
        sb.table("conversation_sessions").insert({
            "id": session_id, "company_id": company_id,
            "user_id": user["id"], "title": question[:60],
        }).execute()

    session_row = sb.table("conversation_sessions")\
        .select("*").eq("id", session_id).eq("user_id", user["id"])\
        .single().execute().data
    if not session_row:
        raise HTTPException(status_code=404, detail="Session not found")

    # ── 2. Load conversation history (last 10 messages + rolling summary) ──
    recent_rows = sb.table("conversation_messages")\
        .select("role, content").eq("session_id", session_id)\
        .order("created_at", desc=True).limit(10).execute().data or []
    recent_rows.reverse()

    history_text = ""
    if session_row.get("summary"):
        history_text += f"[Summary of earlier conversation]\n{session_row['summary']}\n\n"
    if recent_rows:
        history_text += "[Recent messages]\n"
        for m in recent_rows:
            label = "User" if m["role"] == "user" else "PatchBrain"
            history_text += f"{label}: {m['content'][:500]}\n"

    # ── 3. Embed question ──
    try:
        embed_resp = openai_client.embeddings.create(
            model="text-embedding-3-small", input=question)
    except Exception as e:
        err = str(e).lower()
        if any(w in err for w in ["quota", "billing", "insufficient", "credit"]):
            raise HTTPException(status_code=402,
                detail="OpenAI billing issue — add credits at platform.openai.com")
        raise HTTPException(status_code=500, detail=f"Embedding error: {str(e)}")

    query_embedding = embed_resp.data[0].embedding

    # ── 4. Hybrid search (60% text, 40% vector for oilfield exact matches) ──
    chunks = sb.rpc("hybrid_search", {
        "p_company_id":      company_id,
        "p_query_embedding": query_embedding,
        "p_query_text":      question,
        "p_match_count":     12,
    }).execute().data or []

    if not chunks:
        # Save the question even if no results
        sb.table("conversation_messages").insert([
            {"session_id": session_id, "company_id": company_id, "user_id": user["id"],
             "role": "user", "content": question},
            {"session_id": session_id, "company_id": company_id, "user_id": user["id"],
             "role": "assistant",
             "content": "I don't have enough information in the uploaded files to answer that confidently."},
        ]).execute()
        sb.table("conversation_sessions").update({
            "message_count": (session_row.get("message_count") or 0) + 2,
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", session_id).execute()
        return {
            "answer":     "I don't have enough information in the uploaded files to answer that confidently.",
            "citations":  [],
            "session_id": session_id,
        }

    # ── 5. Get filenames ──
    fv_ids = list({c["file_version_id"] for c in chunks if c.get("file_version_id")})
    filename_map = {}
    if fv_ids:
        rows = sb.table("file_versions").select("id, original_filename")\
            .in_("id", fv_ids).execute().data or []
        filename_map = {r["id"]: r["original_filename"] for r in rows}

    # ── 6. Build source blocks ──
    source_blocks = []
    for i, chunk in enumerate(chunks[:10], start=1):
        fname = filename_map.get(chunk.get("file_version_id"), "unknown file")
        lines = [f"File: {fname}"]
        st = chunk.get("source_type", "")
        if st in ("pdf_text", "pdf_table"):
            lines.append(f"Page: {chunk.get('page_number', '?')}")
        elif st == "sheet_rows":
            lines.append(f"Sheet: {chunk.get('sheet_name','?')}, rows {chunk.get('row_start','?')}–{chunk.get('row_end','?')}")
        lines.append(f"Content:\n{(chunk.get('content') or '')[:800]}")
        source_blocks.append(f"[S{i}]\n" + "\n".join(lines))

    sources_text = "\n\n---\n\n".join(source_blocks)

    # ── 7. Build user message ──
    user_msg = ""
    if history_text:
        user_msg += f"Conversation context:\n{history_text}\n\n"
    user_msg += f"Current question: {question}\n\nSources:\n{sources_text}"

    # ── 8. Call Claude with prompt caching ──
    try:
        response = claude_client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=1024,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"}
            }],
            messages=[{"role": "user", "content": user_msg}],
        )
    except anthropic.APIStatusError as e:
        if e.status_code in (402, 403) or any(w in str(e).lower() for w in ["credit", "billing"]):
            raise HTTPException(status_code=402,
                detail="Anthropic billing issue — add credits at console.anthropic.com")
        if e.status_code == 529:
            raise HTTPException(status_code=503, detail="Claude is overloaded — try again shortly")
        raise HTTPException(status_code=500, detail=f"Claude error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI error: {str(e)}")

    answer_text = response.content[0].text

    # ── 9. Save messages ──
    citations_json = [{
        "index": i+1, "chunk_id": c["chunk_id"],
        "source_type": c.get("source_type"), "page_number": c.get("page_number"),
        "sheet_name": c.get("sheet_name"), "row_start": c.get("row_start"),
        "row_end": c.get("row_end"),
        "filename": filename_map.get(c.get("file_version_id"), ""),
        "score": c.get("combined_score"),
    } for i, c in enumerate(chunks[:10])]

    sb.table("conversation_messages").insert([
        {"session_id": session_id, "company_id": company_id, "user_id": user["id"],
         "role": "user", "content": question},
        {"session_id": session_id, "company_id": company_id, "user_id": user["id"],
         "role": "assistant", "content": answer_text,
         "citations_json": citations_json,
         "tokens_used": response.usage.input_tokens + response.usage.output_tokens},
    ]).execute()

    # ── 10. Update session + rolling summary every 20 messages ──
    new_count = (session_row.get("message_count") or 0) + 2
    update: dict = {
        "message_count": new_count,
        "updated_at": datetime.utcnow().isoformat(),
    }
    if session_row.get("title") == "New conversation":
        update["title"] = question[:60]

    if new_count >= 20 and new_count % 20 == 0:
        try:
            sum_resp = claude_client.messages.create(
                model=os.environ.get("ANTHROPIC_SUMMARY_MODEL", "claude-haiku-4-5-20251001"),
                max_tokens=300,
                messages=[{"role": "user",
                    "content": f"Summarize this conversation in 150 words, keeping all key facts, numbers, and decisions:\n\n{history_text}"}]
            )
            update["summary"] = sum_resp.content[0].text
        except Exception:
            pass

    sb.table("conversation_sessions").update(update).eq("id", session_id).execute()

    # ── 11. Return ──
    return {
        "answer":     answer_text,
        "session_id": session_id,
        "citations": [{
            "index": i+1, "chunk_id": c["chunk_id"],
            "source_type": c.get("source_type"),
            "page_number": c.get("page_number"),
            "sheet_name": c.get("sheet_name"),
            "row_start": c.get("row_start"), "row_end": c.get("row_end"),
            "filename": filename_map.get(c.get("file_version_id"), ""),
            "preview": (c.get("content") or "")[:300],
            "score": c.get("combined_score"),
        } for i, c in enumerate(chunks[:10])],
    }

@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0"}