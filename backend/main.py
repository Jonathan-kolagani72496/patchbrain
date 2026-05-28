"""
PatchBrain FastAPI backend - v3
Performance: async/await everywhere, asyncpg connection pool,
streaming Claude responses, smart context window, parallel DB calls.
"""

import os
import re
import uuid
import json
import asyncio
import time
from datetime import datetime
from typing import Optional, AsyncGenerator

import asyncpg
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from supabase import create_client, Client
from openai import AsyncOpenAI
import anthropic
import httpx

load_dotenv()

app = FastAPI(title="PatchBrain API", version="3.0.0")

FRONTEND_URL = os.environ.get("FRONTEND_URL", "").rstrip("/")

allowed_origins = [
    "http://localhost:3000",
    "https://patchbrain.vercel.app",
    "https://patchbrain-dv999vhsf-jonathan-kolagani-s-projects.vercel.app",
]

if FRONTEND_URL:
    allowed_origins.append(FRONTEND_URL)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Connection pool ────────────────────────────────────────────────────────────
# CRITICAL: use port 6543 (transaction pooler), NOT 5432 (session mode)
# 5432 caps at ~20 connections. 6543 handles hundreds.

DB_POOL: asyncpg.Pool | None = None

def _db_url() -> str:
    url     = os.environ["SUPABASE_URL"]
    ref     = url.replace("https://", "").split(".")[0]
    pw      = os.environ.get("SUPABASE_DB_PASSWORD", "")
    return f"postgresql://postgres.{ref}:{pw}@aws-0-us-east-1.pooler.supabase.com:6543/postgres"

@app.on_event("startup")
async def startup():
    global DB_POOL
    try:
        DB_POOL = await asyncpg.create_pool(
            _db_url(),
            min_size=2,
            max_size=10,
            command_timeout=30,
            statement_cache_size=0,   # required for Supabase transaction pooler
        )
        print("✓ DB pool ready (port 6543 transaction mode)")
    except Exception as e:
        print(f"⚠ DB pool failed — will use Supabase client fallback: {e}")

@app.on_event("shutdown")
async def shutdown():
    if DB_POOL:
        await DB_POOL.close()

def _sb() -> Client:
    """Sync Supabase client — for auth + storage only. Singleton."""
    return _SB_CLIENT

# Module-level singleton — created once, reused on every request
_SB_CLIENT: Client = create_client(
    os.environ.get("SUPABASE_URL", ""),
    os.environ.get("SUPABASE_SERVICE_KEY", ""),
)

ai   = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
ant  = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# ── Auth ───────────────────────────────────────────────────────────────────────

async def get_user(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    sb    = _sb()
    try:
        r = sb.auth.get_user(token)
        if not r or not r.user:
            raise HTTPException(401, "Invalid token")
        uid = r.user.id
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(401, f"Auth error: {e}")

    if DB_POOL:
        async with DB_POOL.acquire() as c:
            row = await c.fetchrow(
                "SELECT u.*, co.name AS company_name, co.slug AS company_slug "
                "FROM users u JOIN companies co ON co.id=u.company_id WHERE u.id=$1",
                uuid.UUID(uid))
            if not row:
                raise HTTPException(403, f"User not in users table: {uid}")
            return dict(row)
    else:
        row = sb.table("users").select("*, companies(name,slug)").eq("id", uid).single().execute()
        if not row.data:
            raise HTTPException(403, f"User not in users table: {uid}")
        return row.data

# ── /me ────────────────────────────────────────────────────────────────────────

@app.get("/me")
async def me(u=Depends(get_user)):
    return {
        "user_id":      str(u["id"]),
        "email":        u["email"],
        "role":         u["role"],
        "company_id":   str(u["company_id"]),
        "company_name": u.get("company_name") or "Your Company",
        "company_slug": u.get("company_slug") or "",
    }

# ── Upload ─────────────────────────────────────────────────────────────────────

class UploadReq(BaseModel):
    filename: str; mime_type: str; size_bytes: int

@app.post("/upload/request")
async def upload_request(b: UploadReq, u=Depends(get_user)):
    sb   = _sb()
    cid  = str(u["company_id"])
    fid  = str(uuid.uuid4())
    vid  = str(uuid.uuid4())
    path = f"companies/{cid}/files/{fid}/{vid}/{b.filename}"

    if DB_POOL:
        async with DB_POOL.acquire() as c:
            await c.execute("INSERT INTO files(id,company_id,logical_name) VALUES($1,$2,$3)",
                            uuid.UUID(fid), uuid.UUID(cid), b.filename)
            await c.execute(
                "INSERT INTO file_versions(id,file_id,company_id,storage_path,"
                "original_filename,mime_type,size_bytes,status,created_by) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,'uploaded',$8)",
                uuid.UUID(vid), uuid.UUID(fid), uuid.UUID(cid), path,
                b.filename, b.mime_type, b.size_bytes, uuid.UUID(str(u["id"])))
    else:
        sb.table("files").insert({"id":fid,"company_id":cid,"logical_name":b.filename}).execute()
        sb.table("file_versions").insert({"id":vid,"file_id":fid,"company_id":cid,
            "storage_path":path,"original_filename":b.filename,"mime_type":b.mime_type,
            "size_bytes":b.size_bytes,"status":"uploaded","created_by":str(u["id"])}).execute()

    signed = sb.storage.from_("company-files").create_signed_upload_url(path)
    return {"file_id":fid,"file_version_id":vid,
            "upload_url":signed.get("signedURL") or signed.get("signedUrl"),
            "upload_token":signed.get("token"),"storage_path":path}

class ConfirmReq(BaseModel):
    file_version_id: str

@app.post("/upload/confirm")
async def upload_confirm(b: ConfirmReq, u=Depends(get_user)):
    cid = str(u["company_id"])
    jid = str(uuid.uuid4())
    if DB_POOL:
        async with DB_POOL.acquire() as c:
            row = await c.fetchrow(
                "SELECT id FROM file_versions WHERE id=$1 AND company_id=$2",
                uuid.UUID(b.file_version_id), uuid.UUID(cid))
            if not row:
                raise HTTPException(404, "File version not found")
            await c.execute("UPDATE file_versions SET status='queued' WHERE id=$1",
                            uuid.UUID(b.file_version_id))
            await c.execute(
                "INSERT INTO processing_jobs(id,company_id,file_version_id,job_type,status,priority)"
                " VALUES($1,$2,$3,'ingest','queued',5)",
                uuid.UUID(jid), uuid.UUID(cid), uuid.UUID(b.file_version_id))
    else:
        sb = _sb()
        sb.table("file_versions").update({"status":"queued"}).eq("id",b.file_version_id).execute()
        sb.table("processing_jobs").insert({"id":jid,"company_id":cid,
            "file_version_id":b.file_version_id,"job_type":"ingest","status":"queued","priority":5}).execute()
    return {"job_id":jid,"status":"queued"}

# ── Files ──────────────────────────────────────────────────────────────────────

@app.get("/files")
async def list_files(u=Depends(get_user)):
    cid = uuid.UUID(str(u["company_id"]))
    if DB_POOL:
        async with DB_POOL.acquire() as c:
            rows = await c.fetch(
                """SELECT f.id,f.logical_name,f.created_at,
                   json_agg(json_build_object('id',fv.id,'status',fv.status,
                     'original_filename',fv.original_filename,'size_bytes',fv.size_bytes,
                     'created_at',fv.created_at,'error_message',fv.error_message)
                   ORDER BY fv.created_at DESC) AS file_versions
                   FROM files f LEFT JOIN file_versions fv ON fv.file_id=f.id
                   WHERE f.company_id=$1 AND f.deleted_at IS NULL
                   GROUP BY f.id ORDER BY f.created_at DESC""", cid)
            return [dict(r) for r in rows]
    sb  = _sb()
    res = sb.table("files").select("*,file_versions(id,status,original_filename,size_bytes,created_at,error_message)")\
          .eq("company_id",str(cid)).is_("deleted_at","null").order("created_at",desc=True).execute()
    return res.data

class FileIdsBody(BaseModel):
    file_ids: list[str]

@app.post("/files/delete")
async def delete_files(body: FileIdsBody, u=Depends(get_user)):
    """
    Hard-deletes selected files:
    - Supabase Storage objects
    - citations linked to chunks
    - file_chunks
    - processing_jobs
    - file_versions
    - files
    """
    sb         = _sb()
    company_id = str(u["company_id"])

    file_ids = list({fid for fid in body.file_ids if fid})
    if not file_ids:
        return {"deleted_count": 0}

    # Verify files belong to user's company
    owned_files = sb.table("files")\
        .select("id").eq("company_id", company_id)\
        .in_("id", file_ids).execute().data or []
    owned_file_ids = [f["id"] for f in owned_files]
    if not owned_file_ids:
        raise HTTPException(404, "No matching files found")

    # Get versions + storage paths
    versions = sb.table("file_versions")\
        .select("id,file_id,storage_bucket,storage_path,status")\
        .eq("company_id", company_id)\
        .in_("file_id", owned_file_ids).execute().data or []

    version_ids  = [v["id"] for v in versions]
    storage_items = [
        {"bucket": v.get("storage_bucket") or "company-files", "path": v["storage_path"]}
        for v in versions if v.get("storage_path")
    ]

    # Avoid deleting files while actively being processed
    active_statuses = {"parsing", "chunking", "embedding"}
    active = [v for v in versions if v.get("status") in active_statuses]
    if active:
        raise HTTPException(409,
            "One or more files are currently processing. Wait until processing finishes, then delete.")

    # Remove storage objects first
    paths_by_bucket: dict = {}
    for item in storage_items:
        paths_by_bucket.setdefault(item["bucket"], []).append(item["path"])
    for bucket, paths in paths_by_bucket.items():
        if paths:
            sb.storage.from_(bucket).remove(paths)

    # Delete citations linked to chunks from these files
    chunks_rows = sb.table("file_chunks")\
        .select("id").eq("company_id", company_id)\
        .in_("file_id", owned_file_ids).execute().data or []
    chunk_ids = [c["id"] for c in chunks_rows]
    if chunk_ids:
        for i in range(0, len(chunk_ids), 100):
            sb.table("citations").delete()\
                .in_("chunk_id", chunk_ids[i:i+100]).execute()

    # Delete jobs
    if version_ids:
        sb.table("processing_jobs").delete()\
            .eq("company_id", company_id)\
            .in_("file_version_id", version_ids).execute()

    # Delete chunks
    sb.table("file_chunks").delete()\
        .eq("company_id", company_id)\
        .in_("file_id", owned_file_ids).execute()

    # Delete versions
    sb.table("file_versions").delete()\
        .eq("company_id", company_id)\
        .in_("file_id", owned_file_ids).execute()

    # Delete files
    sb.table("files").delete()\
        .eq("company_id", company_id)\
        .in_("id", owned_file_ids).execute()

    return {"deleted_count": len(owned_file_ids), "deleted_file_ids": owned_file_ids}


@app.post("/files/download-url")
async def get_download_urls(body: FileIdsBody, u=Depends(get_user)):
    """Returns signed download URLs for selected files."""
    sb         = _sb()
    company_id = str(u["company_id"])

    file_ids = list({fid for fid in body.file_ids if fid})
    if not file_ids:
        return {"files": []}

    files = sb.table("files")\
        .select("id,logical_name,current_file_version_id")\
        .eq("company_id", company_id)\
        .in_("id", file_ids).execute().data or []
    if not files:
        raise HTTPException(404, "No matching files found")

    current_version_ids = [
        f["current_file_version_id"]
        for f in files if f.get("current_file_version_id")
    ]

    versions = []
    if current_version_ids:
        versions = sb.table("file_versions")\
            .select("id,file_id,storage_bucket,storage_path,original_filename")\
            .eq("company_id", company_id)\
            .in_("id", current_version_ids).execute().data or []

    # Fallback: use latest version per file if current_file_version_id missing
    found_file_ids  = {v["file_id"] for v in versions}
    missing_file_ids = [f["id"] for f in files if f["id"] not in found_file_ids]
    if missing_file_ids:
        fallback = sb.table("file_versions")\
            .select("id,file_id,storage_bucket,storage_path,original_filename,created_at")\
            .eq("company_id", company_id)\
            .in_("file_id", missing_file_ids)\
            .order("created_at", desc=False).execute().data or []
        seen: set = set()
        for v in fallback:
            if v["file_id"] not in seen:
                versions.append(v)
                seen.add(v["file_id"])

    results = []
    for v in versions:
        bucket = v.get("storage_bucket") or "company-files"
        signed = sb.storage.from_(bucket).create_signed_url(v["storage_path"], 3600)
        results.append({
            "file_id":         v["file_id"],
            "file_version_id": v["id"],
            "filename":        v.get("original_filename") or "download",
            "url":             signed.get("signedURL") or signed.get("signedUrl") or signed.get("signed_url"),
        })
    return {"files": results}


@app.delete("/files/{file_id}")
async def delete_file(file_id: str, u=Depends(get_user)):
    cid = str(u["company_id"])
    if DB_POOL:
        async with DB_POOL.acquire() as c:
            await c.execute("UPDATE files SET deleted_at=now() WHERE id=$1 AND company_id=$2",
                            uuid.UUID(file_id), uuid.UUID(cid))
    else:
        _sb().table("files").update({"deleted_at":datetime.utcnow().isoformat()})\
             .eq("id",file_id).eq("company_id",cid).execute()
    return {"deleted":True}

# ── Conversations ──────────────────────────────────────────────────────────────

@app.get("/conversations")
async def list_convos(u=Depends(get_user)):
    uid = uuid.UUID(str(u["id"]))
    if DB_POOL:
        async with DB_POOL.acquire() as c:
            rows = await c.fetch(
                "SELECT id,title,message_count,created_at,updated_at "
                "FROM conversation_sessions WHERE user_id=$1 ORDER BY updated_at DESC LIMIT 50", uid)
            return [dict(r) for r in rows]
    res = _sb().table("conversation_sessions")\
          .select("id,title,message_count,created_at,updated_at")\
          .eq("user_id",str(uid)).order("updated_at",desc=True).limit(50).execute()
    return res.data

@app.post("/conversations")
async def create_conversation(u=Depends(get_user)):
    """Explicitly create a new named conversation session."""
    sid = str(uuid.uuid4())
    cid = str(u["company_id"])
    uid = str(u["id"])
    if DB_POOL:
        async with DB_POOL.acquire() as c:
            await c.execute(
                "INSERT INTO conversation_sessions(id,company_id,user_id,title,message_count)"
                " VALUES($1,$2,$3,'New conversation',0)",
                uuid.UUID(sid), uuid.UUID(cid), uuid.UUID(uid))
    else:
        _sb().table("conversation_sessions").insert({
            "id": sid, "company_id": cid, "user_id": uid, "title": "New conversation",
        }).execute()
    return {"session_id": sid, "title": "New conversation"}

@app.get("/conversations/{sid}/messages")
async def get_msgs(sid: str, u=Depends(get_user)):
    uid  = uuid.UUID(str(u["id"]))
    sess = uuid.UUID(sid)
    if DB_POOL:
        async with DB_POOL.acquire() as c:
            s = await c.fetchrow("SELECT * FROM conversation_sessions WHERE id=$1 AND user_id=$2",sess,uid)
            if not s: raise HTTPException(404,"Session not found")
            m = await c.fetch("SELECT * FROM conversation_messages WHERE session_id=$1 ORDER BY created_at",sess)
            return {"session":dict(s),"messages":[dict(x) for x in m]}
    sb  = _sb()
    s   = sb.table("conversation_sessions").select("*").eq("id",sid).eq("user_id",str(uid)).single().execute()
    if not s.data: raise HTTPException(404,"Session not found")
    m   = sb.table("conversation_messages").select("*").eq("session_id",sid).order("created_at",desc=False).execute()
    return {"session":s.data,"messages":m.data}

@app.delete("/conversations/{sid}")
async def del_convo(sid: str, u=Depends(get_user)):
    uid = uuid.UUID(str(u["id"]))
    if DB_POOL:
        async with DB_POOL.acquire() as c:
            await c.execute("DELETE FROM conversation_sessions WHERE id=$1 AND user_id=$2",uuid.UUID(sid),uid)
    else:
        _sb().table("conversation_sessions").delete().eq("id",sid).eq("user_id",str(uid)).execute()
    return {"deleted":True}

# ── Context builder ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are PatchBrain, an AI assistant for oilfield service companies.
Answer ONLY from the provided sources.
- Cite every claim with [S1], [S2] etc. and mention the filename.
- If sources conflict, say so explicitly.
- If you cannot answer from sources: "I don't have enough information in the uploaded files."
- Never invent numbers, names, dates, or ticket IDs.
- For calculations, show your work step by step.
- Be direct — oilfield operators are busy."""

def _build_context(session: dict, recent: list) -> str:
    parts = []
    if session.get("summary"):
        parts.append(f"[Earlier conversation summary]\n{session['summary']}")
    if recent:
        lines = ["[Recent messages]"]
        for m in recent[-10:]:
            label = "User" if m.get("role") == "user" else "PatchBrain"
            lines.append(f"{label}: {(m.get('content') or '')[:400]}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def _rewrite_search_query(question: str, recent: list) -> str:
    """Rewrite follow-up questions into standalone retrieval queries.

    Claude should still answer the user's original question. This rewritten text is
    used only for embedding, keyword search, and reranking. Fallback is always the
    original question so rewrite failures never break chat.
    """
    q = (question or "").strip()
    if not q:
        return q

    # First-turn questions are usually already standalone. Avoid spending Haiku
    # calls unless prior conversation context exists.
    prior = [m for m in (recent or []) if (m.get("content") or "").strip()]
    if not prior:
        return q

    # Keep this short and cheap. We need retrieval wording, not a polished answer.
    convo_lines = []
    for m in prior[-6:]:
        role = "User" if m.get("role") == "user" else "Assistant"
        content = " ".join((m.get("content") or "").split())[:500]
        convo_lines.append(f"{role}: {content}")
    convo = "\n".join(convo_lines)

    try:
        model = os.environ.get("ANTHROPIC_QUERY_REWRITE_MODEL", "claude-haiku-4-5-20251001")
        r = await ant.messages.create(
            model=model,
            max_tokens=120,
            temperature=0,
            messages=[{
                "role": "user",
                "content": (
                    "Rewrite the current user question into a standalone search query for retrieving oilfield documents.\n"
                    "Resolve pronouns, ellipses, dates, trucks, wells, invoices, tickets, and references from the conversation.\n"
                    "Do not answer the question. Do not add facts not implied by the conversation.\n"
                    "Return only the rewritten search query as plain text.\n\n"
                    f"Conversation:\n{convo}\n\n"
                    f"Current user question: {q}"
                ),
            }],
        )
        rewritten = (r.content[0].text or "").strip().strip('"')
        if not rewritten:
            return q
        if len(rewritten) > 500:
            return rewritten[:500]
        return rewritten
    except Exception as e:
        print(f"query rewrite failed; using original question: {e}")
        return q

async def _maybe_summarize(sid: uuid.UUID, uid: uuid.UUID,
                           count: int, history: str):
    if count < 20 or count % 20 != 0:
        return
    try:
        r = await ant.messages.create(
            model=os.environ.get("ANTHROPIC_SUMMARY_MODEL", "claude-haiku-4-5-20251001"), max_tokens=300,
            messages=[{"role":"user","content":
                f"Summarize this conversation in 150 words keeping all key facts and numbers:\n\n{history}"}])
        summ = r.content[0].text
        if DB_POOL:
            async with DB_POOL.acquire() as c:
                await c.execute("UPDATE conversation_sessions SET summary=$1 WHERE id=$2", summ, sid)
        else:
            _sb().table("conversation_sessions").update({"summary":summ}).eq("id",str(sid)).execute()
    except Exception as e:
        print(f"Summarize failed (non-critical): {e}")


# ── Retrieval intelligence helpers ────────────────────────────────────────────


DETERMINISTIC_COUNT_PATTERNS = (
    "how many times", "count the word", "count word", "count phrase",
    "how often", "number of times", "appears", "appeared", "occur", "occurs",
)

DETERMINISTIC_MENTION_PATTERNS = (
    "which files mention", "what files mention", "files mention",
    "which documents mention", "what documents mention", "documents mention",
    "search for", "find exact", "exact phrase",
)


def _extract_quoted_phrase(q: str) -> str | None:
    patterns = [
        r'"([^"]+)"',
        r"'([^']+)'",
        r"“([^”]+)”",
        r"‘([^’]+)’",
    ]
    for pat in patterns:
        m = re.search(pat, q)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def _extract_after_phrase(q: str, markers: tuple[str, ...]) -> str | None:
    q_clean = q.strip().rstrip("?")
    q_lower = q_clean.lower()
    for marker_text in markers:
        idx = q_lower.find(marker_text)
        if idx >= 0:
            val = q_clean[idx + len(marker_text):].strip(" :,-")
            if val:
                val = re.sub(r"\s+(across|in)\s+(all\s+)?(my\s+)?(files|documents)$", "", val, flags=re.I).strip()
                return val.strip('"').strip("'").strip()
    return None


def _detect_deterministic_tool_intent(q: str) -> tuple[str, str] | None:
    ql = _normalize_question(q) if "_normalize_question" in globals() else " ".join(q.lower().replace("?", "").split())

    quoted = _extract_quoted_phrase(q)

    if any(p in ql for p in DETERMINISTIC_COUNT_PATTERNS):
        term = quoted or _extract_after_phrase(q, (
            "how many times does", "how many times did", "how many times has",
            "count the word", "count word", "count phrase", "how often does",
        ))
        if term:
            term = re.sub(r"\b(appear|appears|appeared|occur|occurs)\b.*$", "", term, flags=re.I).strip()
            term = re.sub(r"^\b(the word|word|phrase)\b", "", term, flags=re.I).strip()
            term = _clean_extracted_term(term)
            if term:
                return ("exact_count", term)

    if any(p in ql for p in DETERMINISTIC_MENTION_PATTERNS):
        term = quoted or _extract_after_phrase(q, (
            "which files mention", "what files mention", "files mention",
            "which documents mention", "what documents mention", "documents mention",
            "search for", "find exact", "exact phrase",
        ))
        if term:
            term = _clean_extracted_term(term)
            if term:
                return ("files_mentioning", term)

    return None


def _clean_extracted_term(term: str) -> str:
    """Strip surrounding quotes, trailing punctuation, and noise words from extracted search terms."""
    if not term:
        return term

    term = term.strip()

    # Strip matching quote wrappers.
    for lq, rq in [('"', '"'), ("'", "'"), ("\u201c", "\u201d"), ("\u2018", "\u2019")]:
        if term.startswith(lq) and term.endswith(rq) and len(term) > 2:
            term = term[1:-1]
            break

    # Strip trailing garbage after an unmatched quote.
    term = re.sub(r'["\u201c\u201d\u2018\u2019].*$', '', term).strip()
    term = re.sub(r'[?!.,:;]+$', '', term).strip()
    return term


def _safe_sql_like_pattern(term: str) -> str:
    """Escape a user term for PostgREST/Supabase ilike pattern matching."""
    term = (term or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{term}%"


def _count_term_in_text(text: str, term: str) -> int:
    if not text or not term:
        return 0

    if re.fullmatch(r"[A-Za-z0-9_-]+", term):
        return len(re.findall(rf"(?<![A-Za-z0-9_-]){re.escape(term)}(?![A-Za-z0-9_-])", text, flags=re.I))

    return len(re.findall(re.escape(term), text, flags=re.I))


async def _load_chunks_matching_term(company_id: str, term: str, limit: int = 1000) -> list[dict]:
    """Load chunks that contain an exact term/phrase.

    Prefer asyncpg DB_POOL when available. If the pool is unavailable because
    SUPABASE_DB_PASSWORD is not configured, fall back to the existing Supabase
    service client. This matches the rest of the backend.
    """
    pattern = _safe_sql_like_pattern(term)

    if DB_POOL:
        try:
            async with DB_POOL.acquire() as c:
                rows = await c.fetch("""
                    select
                      fc.id as chunk_id,
                      fc.content,
                      fc.file_id,
                      fc.file_version_id,
                      fc.source_type,
                      fc.page_number,
                      fc.sheet_name,
                      fc.row_start,
                      fc.row_end,
                      fv.original_filename,
                      fv.status
                    from file_chunks fc
                    join file_versions fv on fv.id = fc.file_version_id
                    join files f on f.id = fc.file_id
                    where fc.company_id = $1
                      and fv.status = 'ready'
                      and f.deleted_at is null
                      and fc.content ilike $2
                    order by fv.created_at desc, fc.chunk_index asc
                    limit $3
                """, uuid.UUID(str(company_id)), pattern, limit)
            return [dict(r) for r in rows]
        except Exception as e:
            print(f"Deterministic asyncpg query failed; falling back to Supabase client: {e}")

    # Supabase client fallback. This avoids requiring SUPABASE_DB_PASSWORD.
    # NOTE: file_chunks may have RLS enabled. The service key bypasses RLS only
    # when the Supabase client is initialized with the SERVICE role key (not anon).
    # _sb() uses SUPABASE_SERVICE_KEY so this should bypass RLS correctly.
    sb = _sb()
    cid_str = str(company_id)

    try:
        chunk_res = (
            sb.table("file_chunks")
            .select("id,content,file_id,file_version_id,source_type,page_number,sheet_name,row_start,row_end,chunk_index")
            .eq("company_id", cid_str)
            .ilike("content", pattern)
            .order("chunk_index", desc=False)
            .limit(limit)
            .execute()
        )
        chunks = chunk_res.data or []
    except Exception as e:
        print(f"_load_chunks_matching_term Supabase ilike query failed: {e!r}")
        return []

    if not chunks:
        return []

    version_ids = sorted({str(c.get("file_version_id")) for c in chunks if c.get("file_version_id")})
    file_ids    = sorted({str(c.get("file_id"))          for c in chunks if c.get("file_id")})

    versions_by_id: dict[str, dict] = {}
    if version_ids:
        try:
            v_res = (
                sb.table("file_versions")
                .select("id,original_filename,status,created_at")
                .eq("company_id", cid_str)   # ← was missing — scope to company
                .in_("id", version_ids)
                .eq("status", "ready")
                .execute()
            )
            versions_by_id = {str(v["id"]): v for v in (v_res.data or [])}
        except Exception as e:
            print(f"_load_chunks_matching_term versions lookup failed: {e!r}")
            return []

    active_file_ids: set[str] = set()
    if file_ids:
        try:
            f_res = (
                sb.table("files")
                .select("id,deleted_at")
                .eq("company_id", cid_str)   # ← was missing — scope to company
                .in_("id", file_ids)
                .is_("deleted_at", "null")
                .execute()
            )
            active_file_ids = {str(f["id"]) for f in (f_res.data or [])}
        except Exception as e:
            print(f"_load_chunks_matching_term files lookup failed: {e!r}")
            return []

    rows: list[dict] = []
    for c in chunks:
        vid = str(c.get("file_version_id"))
        fid = str(c.get("file_id"))

        if vid not in versions_by_id:
            continue
        if fid not in active_file_ids:
            continue

        v = versions_by_id[vid]
        rows.append({
            "chunk_id":         c.get("id"),
            "content":          c.get("content"),
            "file_id":          c.get("file_id"),
            "file_version_id":  c.get("file_version_id"),
            "source_type":      c.get("source_type"),
            "page_number":      c.get("page_number"),
            "sheet_name":       c.get("sheet_name"),
            "row_start":        c.get("row_start"),
            "row_end":          c.get("row_end"),
            "original_filename": v.get("original_filename"),
            "status":           v.get("status"),
        })

    return rows


def _build_exact_count_answer(term: str, rows: list[dict]) -> str:
    if not rows:
        return f'I found 0 occurrences of "{term}" in ready uploaded files.'

    by_file: dict[str, dict] = {}
    total = 0

    for row in rows:
        count = _count_term_in_text(row.get("content") or "", term)
        if count <= 0:
            continue
        filename = row.get("original_filename") or "Unnamed file"
        item = by_file.setdefault(filename, {"count": 0, "chunks": 0})
        item["count"] += count
        item["chunks"] += 1
        total += count

    if total == 0:
        return f'I found 0 exact occurrences of "{term}" in ready uploaded files.'

    lines = [
        f'I found {total} occurrence{"s" if total != 1 else ""} of "{term}" across {len(by_file)} ready file{"s" if len(by_file) != 1 else ""}.',
        "",
    ]

    for i, (filename, info) in enumerate(sorted(by_file.items(), key=lambda x: x[1]["count"], reverse=True), 1):
        lines.append(f"{i}. {filename} — {info['count']} occurrence{'s' if info['count'] != 1 else ''} in {info['chunks']} chunk{'s' if info['chunks'] != 1 else ''}")

    if len(rows) >= 1000:
        lines.append("\nNote: count may be incomplete because the result limit was reached.")
    return "\n".join(lines)


def _build_files_mentioning_answer(term: str, rows: list[dict]) -> str:
    if not rows:
        return f'I found no ready uploaded files mentioning "{term}".'

    by_file: dict[str, dict] = {}

    for row in rows:
        filename = row.get("original_filename") or "Unnamed file"
        count = _count_term_in_text(row.get("content") or "", term)
        info = by_file.setdefault(filename, {
            "matches": 0,
            "chunks": 0,
            "examples": [],
        })
        info["matches"] += max(count, 1)
        info["chunks"] += 1

        if len(info["examples"]) < 2:
            loc_parts = []
            if row.get("page_number") is not None:
                loc_parts.append(f"page {row.get('page_number')}")
            if row.get("sheet_name"):
                loc_parts.append(f"sheet {row.get('sheet_name')}")
            if row.get("row_start") is not None or row.get("row_end") is not None:
                loc_parts.append(f"rows {row.get('row_start')}-{row.get('row_end')}")
            loc = ", ".join(loc_parts) if loc_parts else row.get("source_type") or "source chunk"
            snippet = (row.get("content") or "").replace("\n", " ")
            idx = snippet.lower().find(term.lower())
            if idx >= 0:
                start = max(0, idx - 80)
                end = min(len(snippet), idx + len(term) + 80)
                snippet = snippet[start:end]
            else:
                snippet = snippet[:180]
            info["examples"].append(f"{loc}: {snippet[:220]}")

    lines = [
        f'I found {len(by_file)} ready file{"s" if len(by_file) != 1 else ""} mentioning "{term}".',
        "",
    ]

    for i, (filename, info) in enumerate(sorted(by_file.items(), key=lambda x: x[1]["matches"], reverse=True), 1):
        lines.append(f"{i}. {filename} — {info['matches']} match{'es' if info['matches'] != 1 else ''} across {info['chunks']} chunk{'s' if info['chunks'] != 1 else ''}")
        for ex in info["examples"]:
            lines.append(f"   - {ex}")

    if len(rows) >= 1000:
        lines.append("\nNote: search may be incomplete because the result limit was reached.")
    return "\n".join(lines)


async def _run_deterministic_tool(company_id: str, tool_name: str, term: str) -> str:
    try:
        rows = await _load_chunks_matching_term(company_id, term)
        if tool_name == "exact_count":
            return _build_exact_count_answer(term, rows)
        if tool_name == "files_mentioning":
            return _build_files_mentioning_answer(term, rows)
        return "I could not route that tool request."
    except Exception as e:
        import traceback
        print(f"Deterministic tool failed: tool={tool_name} term={term!r} company={company_id!r}")
        print(traceback.format_exc())
        return (
            f'I could not complete the exact search for "{term}". '
            "Please try rephrasing your question, or ask me to summarize documents instead."
        )


FILE_INVENTORY_PATTERNS = (
    "what files", "list files", "list my files",
    "uploaded files", "uploaded documents", "documents do i have",
    "files do i have", "files do i have access", "show files",
    "show my files", "file library", "my documents",
)

FILE_FAILED_PATTERNS = (
    "failed", "fail", "error", "errors", "upload error", "processing error",
    "why did my file fail", "why did the file fail", "broken file",
    "unsupported file", "did not upload", "didn't upload",
)

FILE_PROCESSING_PATTERNS = (
    "processing", "still processing", "queued", "parsing", "indexing",
    "embedding", "not ready", "still running", "being processed",
)

FILE_READY_PATTERNS = (
    "ready", "searchable", "available", "can i ask", "can ask",
    "ready files", "files are ready", "ask questions about",
)


def _normalize_question(q: str) -> str:
    return " ".join(q.lower().replace("?", "").replace("'", "").split())


def _detect_file_status_intent(q: str) -> str | None:
    """Route product/file-library questions away from RAG.

    Important: do NOT route every question containing "which files".
    "Which files mention Reeves 14H?" should go through RAG.
    "Which files failed?" should use database state.
    """
    ql = _normalize_question(q)

    talks_about_files = any(x in ql for x in (
        "file", "files", "document", "documents", "upload", "uploaded", "library"
    ))
    if not talks_about_files:
        return None

    if any(p in ql for p in FILE_FAILED_PATTERNS):
        return "file_failed_status"

    if any(p in ql for p in FILE_PROCESSING_PATTERNS):
        return "file_processing_status"

    if any(p in ql for p in FILE_READY_PATTERNS):
        return "file_ready_status"

    if any(p in ql for p in FILE_INVENTORY_PATTERNS):
        return "file_inventory"

    return None


def _is_file_inventory_question(q: str) -> bool:
    return _detect_file_status_intent(q) is not None


def _format_bytes(n) -> str:
    try:
        n = int(n or 0)
    except Exception:
        return "unknown size"
    if n <= 0:
        return "unknown size"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


async def _load_file_inventory(company_id: uuid.UUID) -> list[dict]:
    """Load latest visible file version per file for direct file-list questions."""
    if DB_POOL:
        async with DB_POOL.acquire() as c:
            rows = await c.fetch(
                """
                select
                  f.id,
                  f.logical_name,
                  f.created_at,
                  fv.id as file_version_id,
                  fv.original_filename,
                  fv.status,
                  fv.size_bytes,
                  fv.error_message,
                  fv.created_at as version_created_at
                from files f
                left join lateral (
                  select id, original_filename, status, size_bytes, error_message, created_at
                  from file_versions
                  where file_id = f.id
                  order by created_at desc
                  limit 1
                ) fv on true
                where f.company_id = $1
                  and f.deleted_at is null
                order by f.created_at desc
                limit 100
                """,
                company_id,
            )
            return [dict(r) for r in rows]

    sb = _sb()
    data = (
        sb.table("files")
        .select("id,logical_name,created_at,file_versions(id,original_filename,status,size_bytes,error_message,created_at)")
        .eq("company_id", str(company_id))
        .is_("deleted_at", "null")
        .order("created_at", desc=True)
        .limit(100)
        .execute()
        .data
        or []
    )
    out = []
    for f in data:
        versions = sorted(f.get("file_versions") or [], key=lambda x: x.get("created_at") or "", reverse=True)
        v = versions[0] if versions else {}
        out.append({
            "id": f.get("id"),
            "logical_name": f.get("logical_name"),
            "created_at": f.get("created_at"),
            "file_version_id": v.get("id"),
            "original_filename": v.get("original_filename"),
            "status": v.get("status"),
            "size_bytes": v.get("size_bytes"),
            "error_message": v.get("error_message"),
            "version_created_at": v.get("created_at"),
        })
    return out


PROCESSING_FILE_STATUSES = {"uploaded", "queued", "parsing", "parsed", "chunking", "embedding"}


def _file_display_name(f: dict) -> str:
    return f.get("logical_name") or f.get("original_filename") or "Unnamed file"


def _format_file_line(i: int, f: dict, include_error: bool = True) -> str:
    name = _file_display_name(f)
    status = f.get("status") or "unknown"
    size = _format_bytes(f.get("size_bytes"))
    line = f"{i}. {name} — {status} — {size}"
    err = f.get("error_message")
    if include_error and err:
        line += f" — error: {str(err)[:180]}"
    return line


def _build_file_inventory_answer(files: list[dict], intent_type: str = "file_inventory") -> str:
    if not files:
        return "You do not have any uploaded files yet. Upload files from the dashboard first."

    ready_files = [f for f in files if f.get("status") == "ready"]
    processing_files = [f for f in files if f.get("status") in PROCESSING_FILE_STATUSES]
    failed_files = [f for f in files if f.get("status") == "failed"]
    other_files = [
        f for f in files
        if f.get("status") not in PROCESSING_FILE_STATUSES
        and f.get("status") not in {"ready", "failed"}
    ]

    if intent_type == "file_failed_status":
        if not failed_files:
            return f"No files are currently failed. You have {len(ready_files)} ready and {len(processing_files)} still processing."
        lines = [f"{len(failed_files)} file{'s' if len(failed_files) != 1 else ''} failed:", ""]
        lines.extend(_format_file_line(i, f, include_error=True) for i, f in enumerate(failed_files[:25], 1))
        if len(failed_files) > 25:
            lines.append(f"\nShowing first 25 of {len(failed_files)} failed files.")
        return "\n".join(lines)

    if intent_type == "file_processing_status":
        if not processing_files:
            return f"No files are currently processing. You have {len(ready_files)} ready and {len(failed_files)} failed."
        lines = [f"{len(processing_files)} file{'s are' if len(processing_files) != 1 else ' is'} still processing:", ""]
        lines.extend(_format_file_line(i, f, include_error=False) for i, f in enumerate(processing_files[:25], 1))
        if len(processing_files) > 25:
            lines.append(f"\nShowing first 25 of {len(processing_files)} processing files.")
        return "\n".join(lines)

    if intent_type == "file_ready_status":
        if not ready_files:
            return f"No files are ready to search yet. {len(processing_files)} are processing and {len(failed_files)} failed."
        lines = [f"{len(ready_files)} file{'s are' if len(ready_files) != 1 else ' is'} ready to search:", ""]
        lines.extend(_format_file_line(i, f, include_error=False) for i, f in enumerate(ready_files[:25], 1))
        if len(ready_files) > 25:
            lines.append(f"\nShowing first 25 of {len(ready_files)} ready files.")
        if failed_files:
            lines.append(f"\nNote: {len(failed_files)} file{'s' if len(failed_files) != 1 else ''} failed processing.")
        if processing_files:
            lines.append(f"\nNote: {len(processing_files)} file{'s are' if len(processing_files) != 1 else ' is'} still processing.")
        return "\n".join(lines)

    lines = [
        f"You currently have {len(files)} file{'s' if len(files) != 1 else ''} in your library: "
        f"{len(ready_files)} ready, {len(processing_files)} processing, {len(failed_files)} failed.",
        "",
    ]
    for i, f in enumerate(files[:25], 1):
        lines.append(_format_file_line(i, f, include_error=f.get("status") == "failed"))

    if other_files:
        lines.append(f"\n{len(other_files)} file{'s have' if len(other_files) != 1 else ' has'} another status.")
    if len(files) > 25:
        lines.append(f"\nShowing first 25 of {len(files)} files.")
    return "\n".join(lines)


async def _stream_direct_answer(sid: uuid.UUID, cid: uuid.UUID, uid: uuid.UUID, q: str,
                                answer: str, new_count: int, history: str, srow: dict,
                                started_at: float, intent_type: str = "file_inventory"):
    sids = str(sid)
    yield f"data: {json.dumps({'type':'session','session_id':sids})}\n\n"
    yield f"data: {json.dumps({'type':'token','text':answer})}\n\n"
    yield f"data: {json.dumps({'type':'done','citations':[]})}\n\n"
    asyncio.create_task(_save(sid, cid, uid, q, answer, [], new_count, history, srow, 0))
    asyncio.create_task(_insert_rag_log(
        cid=cid, uid=uid, sid=sid, question=q, intent_type=intent_type,
        retrieval_used=False, rerank_used=False, refused=False, refusal_reason=None,
        chunks=[], fname_map={}, citations_count=0, model=None, tokens_used=0, started_at=started_at,
    ))


def _chunk_doc_for_rerank(chunk: dict, filename: str) -> str:
    parts = [
        f"file: {filename}",
        f"source_type: {chunk.get('source_type') or ''}",
    ]
    if chunk.get("page_number") is not None:
        parts.append(f"page: {chunk.get('page_number')}")
    if chunk.get("sheet_name"):
        parts.append(f"sheet: {chunk.get('sheet_name')}")
    if chunk.get("row_start") is not None or chunk.get("row_end") is not None:
        parts.append(f"rows: {chunk.get('row_start')} - {chunk.get('row_end')}")
    parts.append("content: |")
    content = (chunk.get("content") or "")[:1800]
    parts.append("  " + content.replace("\n", "\n  "))
    return "\n".join(parts)


async def _rerank_chunks(question: str, chunks: list[dict], fname_map: dict, top_n: int = 8) -> list[dict]:
    """Cohere rerank with safe fallback to original retrieval order."""
    api_key = os.environ.get("COHERE_API_KEY")
    if not api_key or len(chunks) <= 1:
        return chunks[:top_n]

    docs = [
        _chunk_doc_for_rerank(c, fname_map.get(c.get("file_version_id", ""), "unknown"))
        for c in chunks
    ]
    payload = {
        "model": os.environ.get("COHERE_RERANK_MODEL", "rerank-v3.5"),
        "query": question,
        "documents": docs,
        "top_n": min(top_n, len(docs)),
        "max_tokens_per_doc": int(os.environ.get("COHERE_RERANK_MAX_TOKENS_PER_DOC", "1200")),
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.cohere.com/v2/rerank",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Client-Name": "patchbrain",
                },
                json=payload,
            )
            r.raise_for_status()
            data = r.json()

        ranked = []
        for item in data.get("results", []):
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(chunks):
                c = dict(chunks[idx])
                c["rerank_score"] = item.get("relevance_score")
                ranked.append(c)
        return ranked or chunks[:top_n]
    except Exception as e:
        print(f"Rerank failed; using hybrid_search order: {e}")
        return chunks[:top_n]


def _evidence_is_too_weak(chunks: list[dict]) -> bool:
    if not chunks:
        return True
    top = chunks[0]
    try:
        combined = float(top.get("combined_score") or 0)
    except Exception:
        combined = 0.0
    try:
        text_score = float(top.get("text_score") or 0)
    except Exception:
        text_score = 0.0
    rerank_score = top.get("rerank_score")

    # Conservative defaults; tune with real logs. Set env vars lower/higher after testing.
    min_combined = float(os.environ.get("RETRIEVAL_MIN_COMBINED_SCORE", "0.05"))
    min_rerank = float(os.environ.get("COHERE_RERANK_MIN_SCORE", "0.05"))

    if rerank_score is not None:
        try:
            return float(rerank_score) < min_rerank
        except Exception:
            return False

    return combined < min_combined and text_score <= 0


def _log_retrieval(question: str, chunks: list[dict], fname_map: dict):
    try:
        rows = []
        for c in chunks[:5]:
            rows.append({
                "file": fname_map.get(c.get("file_version_id", ""), "unknown"),
                "combined": c.get("combined_score"),
                "vector": c.get("vector_score"),
                "text": c.get("text_score"),
                "rerank": c.get("rerank_score"),
                "source": c.get("source_type"),
            })
        print("retrieval_debug", json.dumps({"question": question[:120], "top": rows}, default=str))
    except Exception:
        pass



def _safe_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _build_retrieved_chunks_log(chunks: list[dict], fname_map: dict) -> list[dict]:
    out = []
    for rank, c in enumerate(chunks[:20], 1):
        out.append({
            "rank": rank,
            "chunk_id": str(c.get("chunk_id") or ""),
            "file_id": str(c.get("file_id") or ""),
            "file_version_id": str(c.get("file_version_id") or ""),
            "filename": fname_map.get(c.get("file_version_id", ""), "unknown"),
            "source_type": c.get("source_type"),
            "page_number": c.get("page_number"),
            "sheet_name": c.get("sheet_name"),
            "row_start": c.get("row_start"),
            "row_end": c.get("row_end"),
            "combined_score": _safe_float(c.get("combined_score")),
            "vector_score": _safe_float(c.get("vector_score")),
            "text_score": _safe_float(c.get("text_score")),
            "rerank_score": _safe_float(c.get("rerank_score")),
            "preview": (c.get("content") or "")[:240],
        })
    return out


async def _insert_rag_log(
    *,
    cid: uuid.UUID,
    uid: uuid.UUID,
    sid: uuid.UUID,
    question: str,
    rewritten_query: str | None = None,
    intent_type: str = "rag",
    retrieval_used: bool = False,
    rerank_used: bool,
    refused: bool,
    refusal_reason: str | None,
    chunks: list[dict] | None,
    fname_map: dict | None,
    citations_count: int,
    model: str | None,
    tokens_used: int,
    started_at: float,
):
    """Best-effort RAG observability. This must never break user-facing chat."""
    try:
        chunks = chunks or []
        fname_map = fname_map or {}
        top = chunks[0] if chunks else {}
        latency_ms = int((time.perf_counter() - started_at) * 1000)

        row = {
            "company_id": str(cid),
            "user_id": str(uid),
            "session_id": str(sid),
            "question": question,
            "rewritten_query": rewritten_query,
            "intent_type": intent_type,
            "retrieval_used": retrieval_used,
            "rerank_used": rerank_used,
            "refused": refused,
            "refusal_reason": refusal_reason,
            "top_combined_score": _safe_float(top.get("combined_score")),
            "top_vector_score": _safe_float(top.get("vector_score")),
            "top_text_score": _safe_float(top.get("text_score")),
            "top_rerank_score": _safe_float(top.get("rerank_score")),
            "retrieved_chunks_json": _build_retrieved_chunks_log(chunks, fname_map),
            "citations_count": citations_count,
            "model": model,
            "tokens_used": tokens_used or 0,
            "latency_ms": latency_ms,
        }

        if DB_POOL:
            async with DB_POOL.acquire() as c:
                await c.execute(
                    """
                    insert into rag_query_logs(
                        company_id, user_id, session_id, question, rewritten_query, intent_type,
                        retrieval_used, rerank_used, refused, refusal_reason,
                        top_combined_score, top_vector_score, top_text_score, top_rerank_score,
                        retrieved_chunks_json, citations_count, model, tokens_used, latency_ms
                    )
                    values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::jsonb,$16,$17,$18,$19)
                    """,
                    cid, uid, sid, question, rewritten_query, intent_type,
                    retrieval_used, rerank_used, refused, refusal_reason,
                    row["top_combined_score"], row["top_vector_score"], row["top_text_score"], row["top_rerank_score"],
                    json.dumps(row["retrieved_chunks_json"]), citations_count, model, tokens_used or 0, latency_ms,
                )
        else:
            _sb().table("rag_query_logs").insert(row).execute()
    except Exception as e:
        print(f"rag_query_log failed (non-critical): {e}")


# ── /ask  (streaming) ──────────────────────────────────────────────────────────

class AskBody(BaseModel):
    question:   str
    session_id: Optional[str] = None

@app.post("/ask")
async def ask(b: AskBody, u=Depends(get_user)):
    cid  = uuid.UUID(str(u["company_id"]))
    uid  = uuid.UUID(str(u["id"]))
    q    = b.question.strip()
    started_at = time.perf_counter()
    sid  = uuid.UUID(b.session_id) if b.session_id else uuid.uuid4()
    sids = str(sid)

    # Load / create session + recent messages
    if DB_POOL:
        async with DB_POOL.acquire() as c:
            srow = await c.fetchrow(
                "SELECT * FROM conversation_sessions WHERE id=$1 AND user_id=$2", sid, uid)
            if not srow:
                await c.execute(
                    "INSERT INTO conversation_sessions(id,company_id,user_id,title,message_count)"
                    " VALUES($1,$2,$3,$4,0)", sid, cid, uid, q[:60])
                srow = {"id":sid,"summary":None,"message_count":0,"title":q[:60]}
            else:
                srow = dict(srow)
            recent = list(reversed([dict(r) for r in await c.fetch(
                "SELECT role,content FROM conversation_messages "
                "WHERE session_id=$1 ORDER BY created_at DESC LIMIT 10", sid)]))
    else:
        sb = _sb()

        session_rows = (
            sb.table("conversation_sessions")
            .select("*")
            .eq("id", sids)
            .eq("user_id", str(uid))
            .limit(1)
            .execute()
            .data
            or []
        )

        if not session_rows:
            created = (
                sb.table("conversation_sessions")
                .insert({
                    "id": sids,
                    "company_id": str(cid),
                    "user_id": str(uid),
                    "title": q[:60],
                    "message_count": 0,
                })
                .execute()
                .data
                or []
            )

            if created:
                srow = created[0]
            else:
                srow = {
                    "id": sids,
                    "summary": None,
                    "message_count": 0,
                    "title": q[:60],
                }
        else:
            srow = session_rows[0]

        recent = list(reversed(
            sb.table("conversation_messages")
            .select("role,content")
            .eq("session_id", sids)
            .order("created_at", desc=False)
            .limit(10)
            .execute()
            .data
            or []
        ))
    history = _build_context(srow, recent)
    new_count = (srow.get("message_count") or 0) + 2

    # Direct tool answer: file/status questions should not go through RAG/Claude.
    file_status_intent = _detect_file_status_intent(q)
    if file_status_intent:
        files = await _load_file_inventory(cid)
        answer = _build_file_inventory_answer(files, file_status_intent)
        return StreamingResponse(
            _stream_direct_answer(
                sid, cid, uid, q, answer, new_count, history, srow, started_at,
                intent_type=file_status_intent,
            ),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering":"no","Cache-Control":"no-cache"},
        )

    # Deterministic tools: exact counts/searches should use SQL/Python, not Claude.
    deterministic_intent = _detect_deterministic_tool_intent(q)
    if deterministic_intent:
        tool_name, term = deterministic_intent
        answer = await _run_deterministic_tool(cid, tool_name, term)
        return StreamingResponse(
            _stream_direct_answer(
                sid, cid, uid, q, answer, new_count, history, srow, started_at,
                intent_type=tool_name,
            ),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering":"no","Cache-Control":"no-cache"},
        )

    # Rewrite follow-up questions into standalone retrieval queries before embedding.
    # Claude still receives and answers the original user question.
    rewritten_query = await _rewrite_search_query(q, recent)

    # Embed
    try:
        emb = await ai.embeddings.create(model="text-embedding-3-small", input=rewritten_query)
    except Exception as e:
        err = str(e).lower()
        if any(w in err for w in ["quota", "billing", "insufficient", "credit"]):
            raise HTTPException(402, "OpenAI billing issue — add credits at platform.openai.com")
        raise HTTPException(500, f"Embedding error: {e}")

    # Search
    sb     = _sb()
    query_embedding = emb.data[0].embedding

    chunks = (
        sb.rpc("hybrid_search", {
            "p_company_id": str(cid),
            "p_query_embedding": query_embedding,
            "p_query_text": rewritten_query,
            "p_match_count": 20,
        })
        .execute()
        .data
        or []
    )

    # Filenames
    fname_map: dict = {}
    if chunks:
        fv_ids = list({c["file_version_id"] for c in chunks if c.get("file_version_id")})
        if DB_POOL:
            async with DB_POOL.acquire() as c:
                rows = await c.fetch("SELECT id,original_filename FROM file_versions WHERE id=ANY($1)",
                                     [uuid.UUID(x) for x in fv_ids])
                fname_map = {str(r["id"]): r["original_filename"] for r in rows}
        else:
            for r in (sb.table("file_versions").select("id,original_filename").in_("id",fv_ids).execute().data or []):
                fname_map[r["id"]] = r["original_filename"]

    # Rerank: first-stage retrieval gets 20 candidates, second-stage picks best 8.
    chunks = await _rerank_chunks(rewritten_query, chunks, fname_map, top_n=8)
    _log_retrieval(rewritten_query, chunks, fname_map)

    if _evidence_is_too_weak(chunks):
        async def weak_evidence():
            yield f"data: {json.dumps({'type':'session','session_id':sids})}\n\n"
            msg = "I don't have enough information in the uploaded files to answer that confidently."
            yield f"data: {json.dumps({'type':'token','text':msg})}\n\n"
            yield f"data: {json.dumps({'type':'done','citations':[]})}\n\n"
            asyncio.create_task(_insert_rag_log(
                cid=cid, uid=uid, sid=sid, question=q, rewritten_query=rewritten_query, intent_type="rag",
                retrieval_used=True,
                rerank_used=any(c.get("rerank_score") is not None for c in chunks),
                refused=True,
                refusal_reason="weak_evidence",
                chunks=chunks,
                fname_map=fname_map,
                citations_count=0,
                model=None,
                tokens_used=0,
                started_at=started_at,
            ))
        return StreamingResponse(weak_evidence(), media_type="text/event-stream")

    # Source blocks
    src_blocks = []
    citations  = []
    for i, chunk in enumerate(chunks[:8], 1):
        fname = fname_map.get(chunk.get("file_version_id",""), "unknown")
        st    = chunk.get("source_type","")
        lines = [f"File: {fname}"]
        if st in ("pdf_text","pdf_table"):    lines.append(f"Page: {chunk.get('page_number','?')}")
        elif st == "sheet_rows":              lines.append(f"Sheet: {chunk.get('sheet_name','?')}, rows {chunk.get('row_start','?')}–{chunk.get('row_end','?')}")
        elif st == "whatsapp_message":        lines.append(f"WhatsApp — {(chunk.get('metadata_jsonb') or {}).get('sender','unknown')}")
        lines.append(f"Content:\n{(chunk.get('content') or '')[:600]}")
        src_blocks.append(f"[S{i}]\n"+"\n".join(lines))
        citations.append({"index":i,"chunk_id":chunk["chunk_id"],"source_type":st,
            "page_number":chunk.get("page_number"),"sheet_name":chunk.get("sheet_name"),
            "row_start":chunk.get("row_start"),"row_end":chunk.get("row_end"),
            "filename":fname,"preview":(chunk.get("content") or "")[:300],
            "score":chunk.get("combined_score"),"rerank_score":chunk.get("rerank_score")})

    if not chunks:
        async def no_data():
            yield f"data: {json.dumps({'type':'session','session_id':sids})}\n\n"
            msg = "I don't have enough information in the uploaded files to answer that confidently."
            yield f"data: {json.dumps({'type':'token','text':msg})}\n\n"
            yield f"data: {json.dumps({'type':'done','citations':[]})}\n\n"
        return StreamingResponse(no_data(), media_type="text/event-stream")

    user_msg = (f"Conversation context:\n{history}\n\n" if history else "")
    user_msg += f"Current question: {q}\n\nSources:\n" + "\n\n---\n\n".join(src_blocks)

    async def stream_answer() -> AsyncGenerator[str, None]:
        full        = ""
        tokens_used = 0
        yield f"data: {json.dumps({'type':'session','session_id':sids})}\n\n"
        try:
            answer_model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
            async with ant.messages.stream(
                model=answer_model,
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": user_msg,
                    }
                ],
            ) as stream:
                async for text in stream.text_stream:
                    full += text
                    yield f"data: {json.dumps({'type':'token','text':text})}\n\n"

                final_msg = await stream.get_final_message()
                tokens_used = final_msg.usage.input_tokens + final_msg.usage.output_tokens
        except anthropic.APIStatusError as e:
            msg = "Anthropic billing issue — add credits at console.anthropic.com" \
                  if e.status_code in (402,403) else f"Claude error: {e}"
            yield f"data: {json.dumps({'type':'error','message':msg})}\n\n"
            return
        except Exception as e:
            yield f"data: {json.dumps({'type':'error','message':str(e)})}\n\n"
            return

        yield f"data: {json.dumps({'type':'done','citations':citations})}\n\n"

        # Save after streaming — non-blocking
        asyncio.create_task(_save(sid, cid, uid, q, full, citations, new_count, history, srow, tokens_used))
        asyncio.create_task(_insert_rag_log(
            cid=cid, uid=uid, sid=sid, question=q, rewritten_query=rewritten_query, intent_type="rag",
            retrieval_used=True,
            rerank_used=any(c.get("rerank_score") is not None for c in chunks),
            refused=False,
            refusal_reason=None,
            chunks=chunks,
            fname_map=fname_map,
            citations_count=len(citations),
            model=answer_model,
            tokens_used=tokens_used,
            started_at=started_at,
        ))

    return StreamingResponse(stream_answer(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering":"no","Cache-Control":"no-cache"})

async def _save(sid, cid, uid, q, answer, citations, count, history, srow, tokens_used=0):
    try:
        if DB_POOL:
            async with DB_POOL.acquire() as c:
                await c.execute(
                    "INSERT INTO conversation_messages(id,session_id,company_id,user_id,role,content)"
                    " VALUES($1,$2,$3,$4,'user',$5)",
                    uuid.uuid4(),sid,cid,uid,q)
                await c.execute(
                    "INSERT INTO conversation_messages(id,session_id,company_id,user_id,role,content,citations_json,tokens_used)"
                    " VALUES($1,$2,$3,$4,'assistant',$5,$6,$7)",
                    uuid.uuid4(),sid,cid,uid,answer,json.dumps(citations),tokens_used)
                upd  = "UPDATE conversation_sessions SET message_count=$1,updated_at=now()"
                args = [count, sid]
                if srow.get("title") in (None, "New conversation"):
                    upd += ",title=$3"
                    args.append(q[:60])
                await c.execute(upd + " WHERE id=$2", *args)
        else:
            sb = _sb()
            sb.table("conversation_messages").insert([
                {"session_id":str(sid),"company_id":str(cid),"user_id":str(uid),"role":"user","content":q},
                {"session_id":str(sid),"company_id":str(cid),"user_id":str(uid),"role":"assistant",
                 "content":answer,"citations_json":citations,"tokens_used":tokens_used}]).execute()
            upd: dict = {"message_count":count,"updated_at":datetime.utcnow().isoformat()}
            if srow.get("title") in (None,"New conversation"): upd["title"] = q[:60]
            sb.table("conversation_sessions").update(upd).eq("id",str(sid)).execute()
        await _maybe_summarize(sid,uid,count,history)
    except Exception as e:
        print(f"_save error: {e}")

# ── Evidence viewer ───────────────────────────────────────────────────────────

@app.get("/chunks/{chunk_id}")
async def get_chunk_evidence(chunk_id: str, u=Depends(get_user)):
    """Return the full source chunk behind a citation.

    Company-scoped: users can only read chunks that belong to their company.
    This endpoint returns original `file_chunks.content`, not generated embedding text.
    """
    try:
        chunk_uuid = uuid.UUID(chunk_id)
    except ValueError:
        raise HTTPException(400, "Invalid chunk_id")

    cid = uuid.UUID(str(u["company_id"]))

    if DB_POOL:
        async with DB_POOL.acquire() as c:
            row = await c.fetchrow(
                """
                select
                    fc.id as chunk_id,
                    fc.company_id,
                    fc.file_id,
                    fc.file_version_id,
                    fc.chunk_index,
                    fc.content,
                    fc.source_type,
                    fc.page_number,
                    fc.sheet_name,
                    fc.row_start,
                    fc.row_end,
                    fc.metadata_jsonb,
                    fc.token_count,
                    fc.contextual_summary,
                    fc.contextualized_at,
                    f.logical_name,
                    fv.original_filename,
                    fv.status as file_status,
                    fv.created_at as file_version_created_at
                from file_chunks fc
                join file_versions fv on fv.id = fc.file_version_id
                join files f on f.id = fc.file_id
                where fc.id = $1
                  and fc.company_id = $2
                  and fv.company_id = $2
                  and f.company_id = $2
                  and f.deleted_at is null
                limit 1
                """,
                chunk_uuid,
                cid,
            )
            if not row:
                raise HTTPException(404, "Chunk not found")

            d = dict(row)
            return {
                "chunk_id": str(d["chunk_id"]),
                "company_id": str(d["company_id"]),
                "file_id": str(d["file_id"]),
                "file_version_id": str(d["file_version_id"]),
                "chunk_index": d.get("chunk_index"),
                "filename": d.get("original_filename") or d.get("logical_name"),
                "logical_name": d.get("logical_name"),
                "original_filename": d.get("original_filename"),
                "file_status": d.get("file_status"),
                "source_type": d.get("source_type"),
                "page_number": d.get("page_number"),
                "sheet_name": d.get("sheet_name"),
                "row_start": d.get("row_start"),
                "row_end": d.get("row_end"),
                "metadata_jsonb": d.get("metadata_jsonb") or {},
                "token_count": d.get("token_count"),
                "content": d.get("content") or "",
                "contextual_summary": d.get("contextual_summary"),
                "contextualized_at": d.get("contextualized_at").isoformat() if d.get("contextualized_at") else None,
                "file_version_created_at": d.get("file_version_created_at").isoformat() if d.get("file_version_created_at") else None,
            }

    sb = _sb()
    chunk = (
        sb.table("file_chunks")
        .select("*")
        .eq("id", str(chunk_uuid))
        .eq("company_id", str(cid))
        .single()
        .execute()
        .data
    )
    if not chunk:
        raise HTTPException(404, "Chunk not found")

    file_row = (
        sb.table("files")
        .select("id,logical_name,deleted_at")
        .eq("id", chunk["file_id"])
        .eq("company_id", str(cid))
        .single()
        .execute()
        .data
    )
    if not file_row or file_row.get("deleted_at") is not None:
        raise HTTPException(404, "Chunk not found")

    version_row = (
        sb.table("file_versions")
        .select("id,original_filename,status,created_at")
        .eq("id", chunk["file_version_id"])
        .eq("company_id", str(cid))
        .single()
        .execute()
        .data
        or {}
    )

    return {
        "chunk_id": chunk["id"],
        "company_id": chunk["company_id"],
        "file_id": chunk["file_id"],
        "file_version_id": chunk["file_version_id"],
        "chunk_index": chunk.get("chunk_index"),
        "filename": version_row.get("original_filename") or file_row.get("logical_name"),
        "logical_name": file_row.get("logical_name"),
        "original_filename": version_row.get("original_filename"),
        "file_status": version_row.get("status"),
        "source_type": chunk.get("source_type"),
        "page_number": chunk.get("page_number"),
        "sheet_name": chunk.get("sheet_name"),
        "row_start": chunk.get("row_start"),
        "row_end": chunk.get("row_end"),
        "metadata_jsonb": chunk.get("metadata_jsonb") or {},
        "token_count": chunk.get("token_count"),
        "content": chunk.get("content") or "",
        "contextual_summary": chunk.get("contextual_summary"),
        "contextualized_at": chunk.get("contextualized_at"),
        "file_version_created_at": version_row.get("created_at"),
    }


@app.get("/health")
async def health():
    return {"status":"ok","version":"3.0.0","pool": DB_POOL is not None}

@app.get("/debug/rag-logs")
async def get_rag_logs(limit: int = 50, u=Depends(get_user)):
    """Company-scoped RAG observability. Owner/admin only."""
    if str(u.get("role", "")).lower() not in {"owner", "admin"}:
        raise HTTPException(403, "Only owner/admin users can view RAG logs")

    limit = max(1, min(limit, 200))
    cid = uuid.UUID(str(u["company_id"]))

    if DB_POOL:
        async with DB_POOL.acquire() as c:
            rows = await c.fetch(
                """
                select *
                from rag_query_logs
                where company_id = $1
                order by created_at desc
                limit $2
                """,
                cid,
                limit,
            )
            return [dict(r) for r in rows]

    rows = (
        _sb().table("rag_query_logs")
        .select("*")
        .eq("company_id", str(cid))
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )
    return rows

@app.get("/conflicts")
async def list_conflicts(u=Depends(get_user)):
    cid = str(u["company_id"])
    sb = _sb()
    rows = sb.table("data_conflicts") \
        .select("*") \
        .eq("company_id", cid) \
        .order("created_at", desc=True) \
        .limit(100) \
        .execute().data or []
    return rows
class FollowUpBody(BaseModel):
    question: str
    answer: str

@app.post("/suggest-followups")
async def suggest_followups(b: FollowUpBody, u=Depends(get_user)):
    try:
        r = await ant.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=120,
            messages=[{"role": "user", "content":
                f"Given this Q&A about oilfield documents, suggest 3 short follow-up questions (max 8 words each). Return ONLY a JSON array of strings, nothing else.\n\nQ: {b.question}\nA: {b.answer[:400]}"}])
        raw = r.content[0].text.strip().lstrip('```json').rstrip('```').strip()
        suggestions = json.loads(raw)
        return {"suggestions": suggestions[:3] if isinstance(suggestions, list) else []}
    except Exception:
        return {"suggestions": []}