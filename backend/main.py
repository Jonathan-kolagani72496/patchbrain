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

from calc_tool import detect_calc_intent, run_structured_calc
from issue_rules import run_issue_detection

load_dotenv()

app = FastAPI(title="PatchBrain API", version="3.0.0")

@app.get("/debug/version")
async def debug_version():
    return {
        "app": "patchbrain-api",
        "deterministic_version": "v10-structured-calc-v4",
        "message": "If you see this, Render is running the updated backend/main.py"
    }
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

    if detect_calc_intent(q):
        return ("structured_calc", "")

    return None


# ── Entity-aware intent detection (Task 1) ────────────────────────────────────

ENTITY_SUMMARY_PATTERNS = (
    "what trucks are in the system", "what drivers are in the system",
    "what leases are in the system", "what clients are in the system",
    "what invoice ids are in the system", "what ticket ids are in the system",
    "what po numbers are in the system", "what job ids are in the system",
    "list all trucks", "list all drivers", "list all leases", "list all clients",
    "show all trucks", "show all drivers", "show all leases", "show all clients",
    "how many trucks", "how many drivers", "how many leases", "how many clients",
    "what entities are in the system", "show entities", "list entities",
)

ENTITY_LOOKUP_PATTERNS = (
    "show everything about", "tell me everything about", "what do we know about",
    "where does", "where do", "show me everything about", "what is known about",
    "all mentions of", "all references to",
)

ENTITY_FILES_PATTERNS = (
    "which files mention", "what files mention", "show files related to",
    "files related to", "documents that mention", "which documents mention",
    "files mentioning",
)

_ENTITY_TYPE_KEYWORDS: dict[str, list[str]] = {
    "truck": ["truck", "trucks", "unit", "units"],
    "driver": ["driver", "drivers"],
    "client": ["client", "clients", "customer", "customers", "operator", "operators"],
    "lease": ["lease", "leases", "well", "wells", "location", "locations", "site", "sites"],
    "invoice_id": ["invoice id", "invoice ids", "invoices"],
    "ticket_id": ["ticket id", "ticket ids", "tickets", "run tickets"],
    "po_number": ["po number", "po numbers", "purchase orders"],
}


def detect_entity_summary_intent(q: str) -> str | None:
    """Returns specific entity type ('truck','driver', etc.) for type-specific questions,
    'all' for broad summary questions, or None if not an entity summary intent."""
    ql = _normalize_question(q)
    # Check type-specific FIRST so "what trucks are in the system?" → 'truck', not 'all'
    for etype, keywords in _ENTITY_TYPE_KEYWORDS.items():
        for kw in keywords:
            if (f"what {kw}" in ql or f"list {kw}" in ql or f"show {kw}" in ql
                    or f"how many {kw}" in ql):
                return etype
    # Broad summary questions → return 'all'
    if any(p in ql for p in ENTITY_SUMMARY_PATTERNS):
        return "all"
    return None


def detect_entity_lookup_intent(q: str) -> tuple[str, str] | None:
    ql = _normalize_question(q)
    for p in ENTITY_LOOKUP_PATTERNS:
        if p in ql:
            after = q[q.lower().find(p) + len(p):].strip(" ?.,:")
            if after:
                return ("entity_lookup", after)
    # "where does X appear?" pattern
    m = re.search(r"where does?\s+(.+?)\s+(appear|show up|come from|get mentioned)", q, re.I)
    if m:
        return ("entity_lookup", m.group(1).strip())
    return None


def detect_entity_files_intent(q: str) -> tuple[str, str] | None:
    ql = _normalize_question(q)
    # Avoid stealing from entity_lookup
    for p in ENTITY_FILES_PATTERNS:
        if p in ql:
            idx = q.lower().find(p)
            after = q[idx + len(p):].strip(" ?.,:")
            if after:
                return ("entity_files", after)
    return None


async def run_entity_summary(company_id: uuid.UUID, entity_type_filter: str = "all") -> str:
    """Return entity summary. entity_type_filter: specific type like 'truck', or 'all'."""
    types_to_count = ["truck", "driver", "client", "lease", "ticket_id", "invoice_id", "po_number", "job_id"]
    label_map = {
        "truck": "Trucks", "driver": "Drivers", "client": "Clients",
        "lease": "Leases", "ticket_id": "Ticket IDs", "invoice_id": "Invoice IDs",
        "po_number": "PO Numbers", "job_id": "Job IDs",
    }
    label_singular = {
        "truck": "truck", "driver": "driver", "client": "client",
        "lease": "lease", "ticket_id": "ticket ID", "invoice_id": "invoice ID",
        "po_number": "PO number", "job_id": "job ID",
    }

    query_types = (
        [entity_type_filter]
        if entity_type_filter != "all" and entity_type_filter in types_to_count
        else types_to_count
    )

    if DB_POOL:
        async with DB_POOL.acquire() as c:
            rows = await c.fetch(
                """
                select entity_type, count(distinct canonical_value) as cnt,
                       array_agg(distinct canonical_value order by canonical_value) as examples
                from entity_mentions
                where company_id = $1
                  and entity_type = any($2::text[])
                group by entity_type
                """,
                company_id, query_types,
            )
            raw = {r["entity_type"]: {"count": r["cnt"], "examples": list(r["examples"] or [])} for r in rows}
    else:
        sb = _sb()
        cid_str = str(company_id)
        raw: dict = {}
        for etype in query_types:
            try:
                res = (
                    sb.table("entity_mentions")
                    .select("canonical_value")
                    .eq("company_id", cid_str)
                    .eq("entity_type", etype)
                    .execute()
                )
                vals = sorted({r["canonical_value"] for r in (res.data or []) if r.get("canonical_value")})
                raw[etype] = {"count": len(vals), "examples": vals}
            except Exception:
                raw[etype] = {"count": 0, "examples": []}

    # ── Type-specific response ────────────────────────────────────────────────
    if entity_type_filter != "all" and entity_type_filter in types_to_count:
        info = raw.get(entity_type_filter, {"count": 0, "examples": []})
        label = label_map.get(entity_type_filter, entity_type_filter.title())
        sing  = label_singular.get(entity_type_filter, entity_type_filter)
        if info["count"] == 0:
            return f"[entity-intel-v2] No {sing}s found in the entity index. Files may not have been processed yet."
        examples = info["examples"]
        lines = [f"## {label} in the system", ""]
        for e in examples[:30]:
            lines.append(f"- {e}")
        if info["count"] > 30:
            lines.append(f"- *(+{info['count'] - 30} more)*")
        lines.append("")
        lines.append(f"**{info['count']} unique {sing}{'s' if info['count'] != 1 else ''} found.**")
        if examples:
            lines.append(f"\nAsk: *show everything about {examples[0]}* for details.")
        return "\n".join(lines)

    # ── Full summary ──────────────────────────────────────────────────────────
    has_data = any(v.get("count", 0) > 0 for v in raw.values())
    if not has_data:
        return "[entity-intel-v2] No entity data found. Make sure files have been processed by the intel pass."

    lines = ["## Entity summary", ""]
    lines.append("| Type | Unique | Examples |")
    lines.append("|------|:------:|---------|")
    for etype in types_to_count:
        info = raw.get(etype, {"count": 0, "examples": []})
        if info["count"] == 0:
            continue
        examples = info["examples"][:3]
        sample = ", ".join(str(e) for e in examples)
        more = f" (+{info['count'] - len(examples)} more)" if info["count"] > len(examples) else ""
        lines.append(f"| {label_map[etype]} | {info['count']} | {sample}{more} |")
    lines.append("")
    lines.append("*Ask: 'show everything about T-04' or 'which files mention Carlos Mendez'*")
    return "\n".join(lines)


async def run_entity_lookup(company_id: uuid.UUID, search_term: str) -> str:
    try:
        return await _run_entity_lookup_inner(company_id, search_term)
    except Exception as e:
        import traceback as _tb
        print(f"run_entity_lookup error for {search_term!r}: {e}")
        _tb.print_exc()
        return (
            f"[entity-intel-v2] Error looking up **\"{search_term}\"**: {str(e)[:200]}\n\n"
            "The entity index may still be building. Try again in a moment."
        )


async def _run_entity_lookup_inner(company_id: uuid.UUID, search_term: str) -> str:
    cid_str = str(company_id)
    search_lower = search_term.lower().strip()

    # Build normalized variants for entity-like tokens (T-04 → t04, t-04, t 04)
    search_variants = [search_lower]
    dehyphen = search_lower.replace("-", "").replace(" ", "")
    rehyphen = re.sub(r"([a-z]+)(\d+)", r"\1-\2", dehyphen)
    for v in [dehyphen, rehyphen]:
        if v and v != search_lower:
            search_variants.append(v)

    if DB_POOL:
        async with DB_POOL.acquire() as c:
            conditions = " or ".join(
                f"(lower(em.canonical_value) ilike ${2+i} or lower(em.value) ilike ${2+i})"
                for i in range(len(search_variants))
            )
            params: list = [company_id] + [f"%{v}%" for v in search_variants]
            rows = await c.fetch(
                f"""
                select em.entity_type, em.canonical_value, em.value, em.source_field,
                       em.source_row, em.confidence_score, fv.original_filename, em.file_version_id
                from entity_mentions em
                join file_versions fv on fv.id = em.file_version_id
                where em.company_id = $1
                  and ({conditions})
                order by em.entity_type, em.canonical_value
                limit 200
                """,
                *params,
            )
            mentions = [dict(r) for r in rows]

            # Risk flags from file_intelligence (non-fatal — table may not exist)
            try:
                risk_rows = await c.fetch(
                    """
                    select fi.risk_flags, fv.original_filename
                    from file_intelligence fi
                    join file_versions fv on fv.id = fi.file_version_id
                    where fi.company_id = $1
                      and fi.short_summary ilike $2
                    limit 10
                    """,
                    company_id, f"%{search_lower}%",
                )
                risk_data = [dict(r) for r in risk_rows]
            except Exception as _re:
                print(f"run_entity_lookup risk_data fetch failed (non-fatal): {_re}")
                risk_data = []
    else:
        sb = _sb()
        mentions = []
        for variant in search_variants:
            try:
                # ilike for partial match (may time out on large tables without index)
                res = (
                    sb.table("entity_mentions")
                    .select("entity_type,canonical_value,value,source_field,source_row,confidence_score,file_version_id")
                    .eq("company_id", cid_str)
                    .ilike("canonical_value", f"%{variant}%")
                    .limit(200)
                    .execute()
                    .data or []
                )
            except Exception as _ie:
                print(f"entity_mentions ilike failed for {variant!r}, trying exact eq: {_ie}")
                try:
                    # Exact eq fallback — fast, works well for entity IDs like T-04
                    res = (
                        sb.table("entity_mentions")
                        .select("entity_type,canonical_value,value,source_field,source_row,confidence_score,file_version_id")
                        .eq("company_id", cid_str)
                        .eq("canonical_value", variant)
                        .limit(200)
                        .execute()
                        .data or []
                    )
                except Exception:
                    res = []
            if res:
                mentions = res
                break
        # Enrich with filenames
        fv_ids = list({str(m.get("file_version_id")) for m in mentions if m.get("file_version_id")})
        fname_map: dict = {}
        if fv_ids:
            fv_rows = (
                sb.table("file_versions")
                .select("id,original_filename")
                .in_("id", fv_ids)
                .execute()
                .data or []
            )
            fname_map = {r["id"]: r["original_filename"] for r in fv_rows}
        for m in mentions:
            m["original_filename"] = fname_map.get(str(m.get("file_version_id")), "unknown")
        risk_data = []

    if not mentions:
        return (
            f"[entity-intel-v2] No entity records found for **\"{search_term}\"**.\n\n"
            "Try a broader search or use *'search for ...'* for full-text search."
        )

    # Group by entity_type
    by_type: dict[str, list] = {}
    by_file: dict[str, int] = {}
    for m in mentions:
        etype = m.get("entity_type") or "unknown"
        by_type.setdefault(etype, []).append(m)
        fname = m.get("original_filename") or "unknown"
        by_file[fname] = by_file.get(fname, 0) + 1

    lines = [
        f"## Entity: {search_term}",
        "",
        f"Found **{len(mentions)} mention{'s' if len(mentions) != 1 else ''}** across **{len(by_file)} file{'s' if len(by_file) != 1 else ''}**.",
        "",
        "### Entity types",
    ]

    for etype, items in sorted(by_type.items()):
        canonical = sorted({i.get("canonical_value") for i in items if i.get("canonical_value")})
        lines.append(
            f"- **{etype}**: {', '.join(canonical[:5])}"
            + (f" (+{len(canonical)-5} more)" if len(canonical) > 5 else "")
        )

    lines.append("")
    lines.append("### Files containing this entity")
    for fname, count in sorted(by_file.items(), key=lambda x: -x[1])[:10]:
        lines.append(f"- {fname} — {count} mention{'s' if count != 1 else ''}")

    # Source previews
    preview_items = mentions[:3]
    if preview_items:
        lines.append("")
        lines.append("### Sample source context")
        for m in preview_items:
            field = m.get("source_field") or ""
            row   = m.get("source_row") or ""
            loc   = f" (field: {field}, row: {row})" if field or row else ""
            lines.append(f"- {m.get('original_filename')}: `{m.get('value')}`{loc}")

    # Risk flags
    if risk_data:
        all_flags: list[str] = []
        for rd in risk_data:
            for f in _extract_risk_flags(rd.get("risk_flags")):
                all_flags.append(f)
        if all_flags:
            lines.append("")
            lines.append(f"**Related risk flags:** {', '.join(sorted(set(all_flags))[:5])}")

    lines.append("")
    lines.append(f"*Follow-up: 'which files mention {search_term}?' or 'show total revenue for {search_term}'*")
    return "\n".join(lines)


async def run_entity_files(company_id: uuid.UUID, search_term: str) -> str:
    try:
        return await _run_entity_files_inner(company_id, search_term)
    except Exception as e:
        import traceback as _tb
        print(f"run_entity_files error for {search_term!r}: {e}")
        _tb.print_exc()
        return f"[entity-intel-v2] Error searching files for **\"{search_term}\"**: {str(e)[:200]}"


async def _run_entity_files_inner(company_id: uuid.UUID, search_term: str) -> str:
    """Return files mentioning an entity — combines entity_mentions index AND full-text chunk search."""
    cid_str = str(company_id)
    search_lower = search_term.lower().strip()

    # ── Entity index results ──────────────────────────────────────────────────
    if DB_POOL:
        async with DB_POOL.acquire() as c:
            rows = await c.fetch(
                """
                select fv.original_filename, count(*) as mention_count,
                       array_agg(distinct em.entity_type) as entity_types
                from entity_mentions em
                join file_versions fv on fv.id = em.file_version_id
                where em.company_id = $1
                  and (lower(em.canonical_value) ilike $2 or lower(em.value) ilike $2)
                group by fv.original_filename
                order by mention_count desc
                limit 50
                """,
                company_id, f"%{search_lower}%",
            )
            entity_results = [dict(r) for r in rows]
    else:
        sb = _sb()
        mentions = (
            sb.table("entity_mentions")
            .select("canonical_value,value,entity_type,file_version_id")
            .eq("company_id", cid_str)
            .ilike("canonical_value", f"%{search_lower}%")
            .limit(500)
            .execute()
            .data or []
        )
        fv_ids = list({str(m.get("file_version_id")) for m in mentions if m.get("file_version_id")})
        fname_map: dict = {}
        if fv_ids:
            fv_rows = (
                sb.table("file_versions").select("id,original_filename")
                .in_("id", fv_ids).execute().data or []
            )
            fname_map = {r["id"]: r["original_filename"] for r in fv_rows}
        agg: dict = {}
        for m in mentions:
            fname = fname_map.get(str(m.get("file_version_id")), "unknown")
            if fname not in agg:
                agg[fname] = {"mention_count": 0, "entity_types": set()}
            agg[fname]["mention_count"] += 1
            agg[fname]["entity_types"].add(m.get("entity_type") or "unknown")
        entity_results = [
            {"original_filename": fn, "mention_count": v["mention_count"],
             "entity_types": list(v["entity_types"])}
            for fn, v in sorted(agg.items(), key=lambda x: -x[1]["mention_count"])
        ]

    # ── Full-text chunk search (always run to catch PDF/email/TXT) ────────────
    chunk_rows = await _load_chunks_matching_term(cid_str, search_term, limit=500)
    text_by_file: dict[str, int] = {}
    for row in chunk_rows:
        fname = row.get("original_filename") or "Unnamed file"
        count = max(_count_substring(row.get("content") or "", search_term), 1)
        text_by_file[fname] = text_by_file.get(fname, 0) + count

    entity_filenames = {r["original_filename"] for r in entity_results}
    text_only = {fn: cnt for fn, cnt in text_by_file.items() if fn not in entity_filenames}

    if not entity_results and not text_by_file:
        return f"[entity-intel-v2] No files found mentioning **\"{search_term}\"** in the entity index or document text."

    lines = [f"## Files mentioning: {search_term}", ""]

    if entity_results:
        lines.append("**From entity index:**")
        for r in entity_results[:20]:
            etypes = ", ".join(r.get("entity_types") or [])
            cnt = r["mention_count"]
            lines.append(
                f"- {r['original_filename']} — {cnt} mention{'s' if cnt != 1 else ''}"
                + (f" [{etypes}]" if etypes else "")
            )
    else:
        lines.append("*(No structured entity-index matches)*")

    if text_only:
        lines.append("")
        lines.append("**Additional text matches:**")
        for fn, cnt in sorted(text_only.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"- {fn} — {cnt} match{'es' if cnt != 1 else ''}")

    total_files = len(entity_filenames | set(text_by_file.keys()))
    lines.append("")
    lines.append(f"*Total: {total_files} file{'s' if total_files != 1 else ''} mention \"{search_term}\".*")
    return "\n".join(lines)


async def _dispatch_entity_intent(q: str, company_id: uuid.UUID) -> str | None:
    """Returns an answer string if the question is an entity intent, else None."""
    try:
        # Order matters: files intent before lookup to avoid mis-routing
        entity_type = detect_entity_summary_intent(q)
        if entity_type is not None:
            return await run_entity_summary(company_id, entity_type_filter=entity_type)

        files_intent = detect_entity_files_intent(q)
        if files_intent:
            _, term = files_intent
            return await run_entity_files(company_id, term)

        lookup_intent = detect_entity_lookup_intent(q)
        if lookup_intent:
            _, term = lookup_intent
            return await run_entity_lookup(company_id, term)

        return None
    except Exception as e:
        print(f"_dispatch_entity_intent error: {e}")
        import traceback
        traceback.print_exc()
        return None


# ── Risk/intel-aware intent detection (Task 2) ────────────────────────────────

NEEDS_REVIEW_PATTERNS = (
    "which files need review", "what files need review", "show needs review",
    "files that need review", "what should i review", "needs review files",
    "needs review", "need review", "flagged for review",
)

RISK_SUMMARY_PATTERNS = (
    "what risks do we have", "what issues do we have", "show risk flags",
    "show risks", "show issues", "what risk flags", "any risks",
    "safety issues", "missing po", "missing po issues", "expired cert",
    "expired certification", "spill incident", "what are our risks",
    "any issues", "open issues", "risk summary",
)

INTEL_CATEGORY_PATTERNS = (
    "which files are safety", "safety related files", "show financial files",
    "financial files", "what field data files", "field data files",
    "compliance docs", "compliance documents", "what communications files",
    "communications files", "files by category", "show category",
    "files are safety related", "show safety files", "show compliance",
    "what category", "which category",
)

FILE_INTEL_LOOKUP_PATTERNS = (
    "summarize what patchbrain knows about", "what kind of file is",
    "what are the key entities in", "what does patchbrain know about",
    "tell me about the file", "intelligence for", "file summary for",
    "what is in the file",
)


def detect_needs_review_intent(q: str) -> bool:
    ql = _normalize_question(q)
    return any(p in ql for p in NEEDS_REVIEW_PATTERNS)


def detect_risk_summary_intent(q: str) -> str | None:
    """Returns a specific risk flag keyword if a risk-lookup is detected, else 'all' for general summary."""
    ql = _normalize_question(q)
    if not any(p in ql for p in RISK_SUMMARY_PATTERNS):
        return None
    # Specific risk type requested?
    if any(w in ql for w in ("missing po", "missing purchase order")):
        return "missing_po"
    if any(w in ql for w in ("expired cert", "expired certification", "certification")):
        return "expired_cert"
    if any(w in ql for w in ("spill", "spill incident", "environmental")):
        return "spill_incident"
    if any(w in ql for w in ("safety",)):
        return "safety"
    return "all"


def detect_intel_category_intent(q: str) -> str | None:
    ql = _normalize_question(q)
    if not any(p in ql for p in INTEL_CATEGORY_PATTERNS):
        return None
    # Return the category keyword
    for cat in ("safety", "financial", "field_data", "compliance", "communications",
                "operational", "environmental", "legal", "hr"):
        if cat.replace("_", " ") in ql or cat in ql:
            return cat
    return "all"


def detect_file_intel_lookup_intent(q: str) -> str | None:
    """Returns the filename/search term if a file-intel lookup is detected."""
    ql = _normalize_question(q)
    for p in FILE_INTEL_LOOKUP_PATTERNS:
        if p in ql:
            idx = q.lower().find(p)
            after = q[idx + len(p):].strip(" ?.,:")
            if after:
                return after
    return None


def _extract_risk_flags(flags_raw) -> list[str]:
    """Normalize risk_flags from JSONB — handles list[str] and list[dict]."""
    if not flags_raw:
        return []
    if isinstance(flags_raw, str):
        try:
            flags_raw = json.loads(flags_raw)
        except Exception:
            return [flags_raw]
    if isinstance(flags_raw, dict):
        # Object wrapper: {"flags": [...]} or {"type": "..."}
        inner = flags_raw.get("flags") or flags_raw.get("items") or flags_raw.get("details") or []
        if isinstance(inner, list):
            flags_raw = inner
        else:
            val = flags_raw.get("flag") or flags_raw.get("type") or flags_raw.get("name")
            return [val] if val else []
    if not isinstance(flags_raw, list):
        return []
    out = []
    for f in flags_raw:
        if isinstance(f, str):
            out.append(f)
        elif isinstance(f, dict):
            out.append(f.get("flag") or f.get("type") or f.get("name") or str(f))
    return [x for x in out if x]


async def run_needs_review(company_id: uuid.UUID) -> str:
    cid_str = str(company_id)

    if DB_POOL:
        async with DB_POOL.acquire() as c:
            rows = await c.fetch(
                """
                select fi.short_summary, fi.category, fi.document_type, fi.risk_flags,
                       fv.original_filename
                from file_intelligence fi
                join file_versions fv on fv.id = fi.file_version_id
                join files f on f.id = fi.file_id
                where fi.company_id = $1
                  and fi.needs_review = true
                  and f.deleted_at is null
                order by fi.created_at desc
                limit 50
                """,
                company_id,
            )
            data = [dict(r) for r in rows]
    else:
        sb = _sb()
        rows = (
            sb.table("file_intelligence")
            .select("short_summary,category,document_type,risk_flags,file_version_id")
            .eq("company_id", cid_str)
            .eq("needs_review", True)
            .order("created_at", desc=True)
            .limit(50)
            .execute()
            .data or []
        )
        fv_ids = [str(r.get("file_version_id")) for r in rows if r.get("file_version_id")]
        fname_map: dict = {}
        if fv_ids:
            fv_rows = (
                sb.table("file_versions").select("id,original_filename")
                .in_("id", fv_ids).execute().data or []
            )
            fname_map = {r["id"]: r["original_filename"] for r in fv_rows}
        data = []
        for r in rows:
            r["original_filename"] = fname_map.get(str(r.get("file_version_id")), "unknown")
            data.append(r)

    if not data:
        return "[intel-v2] No files are currently flagged for review."

    lines = [f"## Files needing review ({len(data)} file{'s' if len(data) != 1 else ''})", ""]
    for i, r in enumerate(data[:25], 1):
        fname    = r.get("original_filename") or "unknown"
        cat      = r.get("category") or ""
        doc_type = r.get("document_type") or ""
        summary  = (r.get("short_summary") or "")[:120]
        flags    = _extract_risk_flags(r.get("risk_flags"))
        flag_str = f" ⚠ {', '.join(flags[:3])}" if flags else ""
        meta     = " · ".join(x for x in [cat, doc_type] if x)
        lines.append(f"**{i}. {fname}**" + (f" `[{meta}]`" if meta else "") + flag_str)
        if summary:
            lines.append(f"   > {summary}")
        lines.append("")
    return "\n".join(lines)


async def run_risk_summary(company_id: uuid.UUID, risk_filter: str) -> str:
    try:
        return await _run_risk_summary_inner(company_id, risk_filter)
    except Exception as e:
        import traceback as _tb
        print(f"run_risk_summary error (filter={risk_filter!r}): {e}")
        _tb.print_exc()
        return f"[intel-v2] Error loading risk summary: {str(e)[:200]}"


async def _run_risk_summary_inner(company_id: uuid.UUID, risk_filter: str) -> str:
    cid_str = str(company_id)

    if DB_POOL:
        async with DB_POOL.acquire() as c:
            rows = await c.fetch(
                """
                select fi.risk_flags, fv.original_filename
                from file_intelligence fi
                join file_versions fv on fv.id = fi.file_version_id
                join files f on f.id = fi.file_id
                where fi.company_id = $1
                  and fi.risk_flags is not null
                  and fi.risk_flags != 'null'::jsonb
                  and fi.risk_flags != '[]'::jsonb
                  and f.deleted_at is null
                limit 200
                """,
                company_id,
            )
            data = [dict(r) for r in rows]
    else:
        sb = _sb()
        rows = (
            sb.table("file_intelligence")
            .select("risk_flags,file_version_id")
            .eq("company_id", cid_str)
            .order("created_at", desc=True)
            .limit(200)
            .execute()
            .data or []
        )
        fv_ids = [str(r.get("file_version_id")) for r in rows if r.get("file_version_id")]
        fname_map: dict = {}
        if fv_ids:
            fv_rows = (
                sb.table("file_versions").select("id,original_filename")
                .in_("id", fv_ids).execute().data or []
            )
            fname_map = {r["id"]: r["original_filename"] for r in fv_rows}
        data = []
        for r in rows:
            r["original_filename"] = fname_map.get(str(r.get("file_version_id")), "unknown")
            data.append(r)

    if not data:
        return "[intel-v2] No risk flags found in your files."

    # Aggregate: flag -> list of filenames
    flag_files: dict[str, list[str]] = {}
    for r in data:
        flags = _extract_risk_flags(r.get("risk_flags"))
        fname = r.get("original_filename") or "unknown"
        for flag in flags:
            flag_files.setdefault(flag, []).append(fname)

    if not flag_files:
        return "[intel-v2] Files have risk_flags recorded but no parseable flag values were found.\n\nCheck that the intel pass ran successfully after file processing."

    # Filter if specific risk type requested
    if risk_filter != "all":
        flag_files = {k: v for k, v in flag_files.items() if risk_filter.lower() in k.lower()}
        if not flag_files:
            return f"[intel-v2] No **{risk_filter}** risk flags found in your files."

    # Reverse map: filename -> list of flags
    file_flags: dict[str, list[str]] = {}
    for flag, files in flag_files.items():
        for fn in files:
            file_flags.setdefault(fn, []).append(flag)

    lines = ["## Risk summary", ""]
    lines.append("| Risk flag | Files affected |")
    lines.append("|-----------|:--------------:|")
    for flag, files in sorted(flag_files.items(), key=lambda x: -len(x[1])):
        lines.append(f"| {flag.replace('_', ' ')} | {len(set(files))} |")

    lines.append("")
    lines.append("### Affected files")
    for fn, flags in sorted(file_flags.items()):
        flag_str = ", ".join(sorted(set(flags)))
        lines.append(f"- **{fn}** — {flag_str}")

    lines.append("")
    lines.append("*Ask: 'which files need review?' or 'explain the missing PO issue in \\<filename\\>'*")
    return "\n".join(lines)


async def run_intel_category_files(company_id: uuid.UUID, category: str) -> str:
    cid_str = str(company_id)

    if DB_POOL:
        async with DB_POOL.acquire() as c:
            if category == "all":
                rows = await c.fetch(
                    """
                    select fi.category, fi.document_type, fi.short_summary,
                           fi.risk_flags, fv.original_filename
                    from file_intelligence fi
                    join file_versions fv on fv.id = fi.file_version_id
                    join files f on f.id = fi.file_id
                    where fi.company_id = $1 and f.deleted_at is null
                    order by fi.category, fi.created_at desc
                    limit 100
                    """,
                    company_id,
                )
            else:
                rows = await c.fetch(
                    """
                    select fi.category, fi.document_type, fi.short_summary,
                           fi.risk_flags, fv.original_filename
                    from file_intelligence fi
                    join file_versions fv on fv.id = fi.file_version_id
                    join files f on f.id = fi.file_id
                    where fi.company_id = $1
                      and (lower(fi.category) ilike $2 or lower(fi.document_type) ilike $2)
                      and f.deleted_at is null
                    order by fi.created_at desc
                    limit 50
                    """,
                    company_id, f"%{category.replace('_', ' ')}%",
                )
            data = [dict(r) for r in rows]
    else:
        sb = _sb()
        q_builder = (
            sb.table("file_intelligence")
            .select("category,document_type,short_summary,risk_flags,file_version_id")
            .eq("company_id", cid_str)
        )
        if category != "all":
            q_builder = q_builder.ilike("category", f"%{category.replace('_', ' ')}%")
        rows = q_builder.order("created_at", desc=True).limit(100).execute().data or []
        fv_ids = [str(r.get("file_version_id")) for r in rows if r.get("file_version_id")]
        fname_map: dict = {}
        if fv_ids:
            fv_rows = (
                sb.table("file_versions").select("id,original_filename")
                .in_("id", fv_ids).execute().data or []
            )
            fname_map = {r["id"]: r["original_filename"] for r in fv_rows}
        data = []
        for r in rows:
            r["original_filename"] = fname_map.get(str(r.get("file_version_id")), "unknown")
            data.append(r)

    if not data:
        label = category.replace("_", " ") if category != "all" else ""
        return f"[intel-v2] No {('**' + label + '**') if label else ''} files found in the intel index."

    label = category.replace("_", " ").title() if category != "all" else "All categories"
    lines = [f"## {label} files ({len(data)} found)", ""]
    for i, r in enumerate(data[:25], 1):
        fname    = r.get("original_filename") or "unknown"
        cat      = r.get("category") or ""
        doc_type = r.get("document_type") or ""
        summary  = (r.get("short_summary") or "")[:100]
        flags    = _extract_risk_flags(r.get("risk_flags"))
        flag_str = f" ⚠ {', '.join(flags[:2])}" if flags else ""
        meta     = " · ".join(x for x in [cat, doc_type] if x)
        lines.append(f"**{i}. {fname}**" + (f" `[{meta}]`" if meta else "") + flag_str)
        if summary:
            lines.append(f"   > {summary}")
        lines.append("")
    return "\n".join(lines)


async def run_file_intel_lookup(company_id: uuid.UUID, search_term: str) -> str:
    cid_str = str(company_id)
    search_lower = search_term.lower().strip()

    if DB_POOL:
        async with DB_POOL.acquire() as c:
            rows = await c.fetch(
                """
                select fi.*, fv.original_filename
                from file_intelligence fi
                join file_versions fv on fv.id = fi.file_version_id
                join files f on f.id = fi.file_id
                where fi.company_id = $1
                  and (lower(fv.original_filename) ilike $2 or lower(fi.short_summary) ilike $2)
                  and f.deleted_at is null
                order by fi.created_at desc
                limit 5
                """,
                company_id, f"%{search_lower}%",
            )
            data = [dict(r) for r in rows]
    else:
        sb = _sb()
        rows = (
            sb.table("file_intelligence")
            .select("*,file_versions(original_filename)")
            .eq("company_id", cid_str)
            .ilike("short_summary", f"%{search_lower}%")
            .order("created_at", desc=True)
            .limit(5)
            .execute()
            .data or []
        )
        data = []
        for r in rows:
            fv = r.pop("file_versions", None) or {}
            r["original_filename"] = fv.get("original_filename") or "unknown"
            data.append(r)

    if not data:
        return f"[intel-v2] No intel record found for **\"{search_term}\"**.\n\nThe file may not have been processed yet, or try using part of the filename."

    lines = []
    for r in data[:3]:
        fname = r.get("original_filename") or "unknown"
        lines.append(f"## File intel: {fname}")
        lines.append("")
        if r.get("category"):
            lines.append(f"- **Category:** {r['category'].replace('_', ' ').title()}")
        if r.get("document_type"):
            lines.append(f"- **Document type:** {r['document_type'].replace('_', ' ').title()}")
        if r.get("short_summary"):
            lines.append(f"- **Summary:** {r['short_summary']}")
        if r.get("needs_review"):
            lines.append("- ⚠ **Flagged for review**")
        flags = _extract_risk_flags(r.get("risk_flags"))
        if flags:
            lines.append(f"- **Risk flags:** {', '.join(f.replace('_', ' ') for f in flags)}")
        key_entities = r.get("key_entities")
        if key_entities:
            if isinstance(key_entities, dict):
                parts = []
                for k, vals in key_entities.items():
                    if vals:
                        parts.append(f"{k}: {', '.join(str(v) for v in vals[:4])}")
                if parts:
                    lines.append(f"- **Key entities:** {'; '.join(parts[:5])}")
            elif isinstance(key_entities, list):
                lines.append(f"- **Key entities:** {', '.join(str(e) for e in key_entities[:10])}")
            elif isinstance(key_entities, str):
                lines.append(f"- **Key entities:** {key_entities[:200]}")
        suggested = r.get("suggested_questions")
        if suggested and isinstance(suggested, list):
            lines.append("")
            lines.append("**Suggested questions:**")
            for sq in suggested[:3]:
                lines.append(f"- {sq}")
        lines.append("")
    return "\n".join(lines).strip()


async def _dispatch_intel_intent(q: str, company_id: uuid.UUID) -> tuple[str, str] | None:
    """Returns (answer, intent_type) if question is an intel/risk intent, else None."""
    try:
        if detect_needs_review_intent(q):
            return (await run_needs_review(company_id), "needs_review_files")

        risk_filter = detect_risk_summary_intent(q)
        if risk_filter is not None:
            intent = "risk_lookup" if risk_filter != "all" else "risk_summary"
            return (await run_risk_summary(company_id, risk_filter), intent)

        cat = detect_intel_category_intent(q)
        if cat is not None:
            return (await run_intel_category_files(company_id, cat), "intel_category_files")

        file_term = detect_file_intel_lookup_intent(q)
        if file_term:
            return (await run_file_intel_lookup(company_id, file_term), "file_intel_lookup")

        return None
    except Exception as e:
        print(f"_dispatch_intel_intent error: {e}")
        import traceback
        traceback.print_exc()
        return None


def detect_operational_issues_intent(q: str) -> str | None:
    """Return intent subtype if question is about operational issues, else None."""
    ql = q.lower()
    issue_triggers = [
        "operational issue", "what issues", "any issues", "list issues",
        "show issues", "show me issues", "issues do we have", "issues need review",
        "what issue", "show candidate", "show confirmed", "show high severity",
        "show open issues", "open issues", "current issues",
    ]
    if not any(t in ql for t in issue_triggers):
        return None
    if any(t in ql for t in ["cert", "certification", "expired cert", "hazmat", "safety cert"]):
        return "expired_cert"
    if any(t in ql for t in ["missing po", "po reference", "purchase order", " po "]):
        return "missing_po"
    if any(t in ql for t in ["spill", "incident followup", "spill incident"]):
        return "spill"
    return "summary"


async def run_operational_issues_answer(company_id: uuid.UUID, intent_subtype: str) -> str:
    """Return a formatted markdown table of operational issues."""
    _RULE_KEY_MAP = {
        "expired_cert": "expired_hazmat_or_safety_cert_driver_used",
        "missing_po": "missing_po_reference",
        "spill": "spill_incident_followup",
    }
    rule_key_filter = _RULE_KEY_MAP.get(intent_subtype)
    issues = []
    try:
        if DB_POOL:
            async with DB_POOL.acquire() as c:
                if rule_key_filter:
                    rows = await c.fetch(
                        "SELECT severity, title, rule_key, status, created_at "
                        "FROM operational_issues WHERE company_id=$1 AND rule_key=$2 "
                        "ORDER BY created_at DESC LIMIT 20",
                        company_id, rule_key_filter,
                    )
                else:
                    rows = await c.fetch(
                        "SELECT severity, title, rule_key, status, created_at "
                        "FROM operational_issues WHERE company_id=$1 "
                        "ORDER BY created_at DESC LIMIT 20",
                        company_id,
                    )
                issues = [dict(r) for r in rows]
        else:
            sb = _sb()
            q2 = (sb.table("operational_issues")
                  .select("severity,title,rule_key,status,created_at")
                  .eq("company_id", str(company_id)))
            if rule_key_filter:
                q2 = q2.eq("rule_key", rule_key_filter)
            issues = q2.order("created_at", desc=True).limit(20).execute().data or []
    except Exception as e:
        return f"[issues-v1] Error retrieving operational issues: {e}"

    if not issues:
        return (
            "[issues-v1] No operational issues found.\n\n"
            "Use **POST /issues/run-detection** to generate candidates from uploaded files."
        )

    lines = [
        "[issues-v1] Operational Issues\n",
        "| Severity | Issue | Rule | Status |",
        "|---|---|---|---|",
    ]
    for issue in issues:
        sev   = issue.get("severity") or ""
        title = (issue.get("title") or "").replace("|", "-")
        rkey  = issue.get("rule_key") or ""
        st    = issue.get("status") or ""
        lines.append(f"| {sev} | {title} | {rkey} | {st} |")
    return "\n".join(lines)


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


def _count_substring(text: str, term: str) -> int:
    """Plain case-insensitive substring count. Used for files_mentioning."""
    if not text or not term:
        return 0
    return len(re.findall(re.escape(term), text, flags=re.I))


def _count_whole_word(text: str, term: str) -> int:
    """Whole-word count for pure-alpha terms (time, pump, load).
    Treats letters, digits, underscores, and hyphens as word-connected, so
    drive_time, timestamp, pump-off, and T-04 do NOT false-match 'time' or 'pump'.
    Non-alpha terms (T-04, Reeves 14H) fall through to plain substring count.
    """
    if not text or not term:
        return 0
    if re.fullmatch(r"[A-Za-z]+", term):
        return len(re.findall(
            rf"(?<![A-Za-z0-9_\-]){re.escape(term)}(?![A-Za-z0-9_\-])",
            text, flags=re.I
        ))
    return len(re.findall(re.escape(term), text, flags=re.I))


# Backward-compat alias
def _count_term_in_text(text: str, term: str) -> int:
    return _count_substring(text, term)


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
    MARKER = "[deterministic-v6]"

    if not rows:
        return f'{MARKER} I found 0 occurrences of "{term}" in ready uploaded files.'

    whole_by_file: dict[str, dict] = {}
    sub_by_file:   dict[str, dict] = {}
    whole_total = 0
    sub_total   = 0

    for row in rows:
        content  = row.get("content") or ""
        filename = row.get("original_filename") or "Unnamed file"

        wc = _count_whole_word(content, term)
        sc = _count_substring(content, term)

        if sc > 0:
            item = sub_by_file.setdefault(filename, {"count": 0, "chunks": 0})
            item["count"]  += sc
            item["chunks"] += 1
            sub_total      += sc

        if wc > 0:
            item = whole_by_file.setdefault(filename, {"count": 0, "chunks": 0})
            item["count"]  += wc
            item["chunks"] += 1
            whole_total    += wc

    # ── Whole-word hits ───────────────────────────────────────────────────────
    if whole_total > 0:
        lines = [
            f'{MARKER} I found {whole_total} standalone occurrence{"s" if whole_total != 1 else ""} of '
            f'"{term}" across {len(whole_by_file)} ready file{"s" if len(whole_by_file) != 1 else ""}.',
            "",
        ]
        for i, (fn, info) in enumerate(
            sorted(whole_by_file.items(), key=lambda x: x[1]["count"], reverse=True), 1
        ):
            lines.append(
                f"{i}. {fn} — {info['count']} standalone occurrence{'s' if info['count'] != 1 else ''} "
                f"in {info['chunks']} chunk{'s' if info['chunks'] != 1 else ''}"
            )
        extra = sub_total - whole_total
        if extra > 0:
            lines += [
                "",
                f'Note: "{term}" also appears {extra} additional time{"s" if extra != 1 else ""} as part of longer terms.',
            ]
        return "\n".join(lines)

    # ── Substring-only hits ───────────────────────────────────────────────────
    if sub_total > 0:
        lines = [
            f'{MARKER} I found 0 standalone occurrences of "{term}", but {sub_total} '
            f'substring match{"es" if sub_total != 1 else ""} across {len(sub_by_file)} '
            f'ready file{"s" if len(sub_by_file) != 1 else ""}.',
            "",
        ]
        for i, (fn, info) in enumerate(
            sorted(sub_by_file.items(), key=lambda x: x[1]["count"], reverse=True), 1
        ):
            lines.append(
                f"{i}. {fn} — {info['count']} substring match{'es' if info['count'] != 1 else ''} "
                f"in {info['chunks']} chunk{'s' if info['chunks'] != 1 else ''}"
            )
        return "\n".join(lines)

    # ── DB returned rows but Python found zero matches ────────────────────────
    # This means Postgres ilike matched but Python regex didn't. Log it and
    # report the raw chunk count so the user isn't silently lied to.
    print(f"[v6 DEBUG] ilike returned {len(rows)} rows but Python found 0 matches for {term!r}")
    return (
        f'{MARKER} Postgres found {len(rows)} chunk{"s" if len(rows) != 1 else ""} containing '
        f'"{term}" but the in-memory count came back 0 — likely an encoding mismatch. '
        f'Try rephrasing or ask me to summarize documents mentioning "{term}" instead.'
    )
def _build_files_mentioning_answer(term: str, rows: list[dict]) -> str:
    if not rows:
        return f'I found no ready uploaded files mentioning "{term}".'

    by_file: dict[str, dict] = {}

    for row in rows:
        filename = row.get("original_filename") or "Unnamed file"
        # ilike already confirmed the term is in this chunk — use substring count.
        # max(..., 1) ensures every matched chunk is counted even if Python
        # encoding differs slightly from what Postgres matched.
        count = _count_substring(row.get("content") or "", term)
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
    f'[deterministic-v6] I found {len(by_file)} ready file{"s" if len(by_file) != 1 else ""} mentioning "{term}".',
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
        if tool_name == "structured_calc":
            return await run_structured_calc(
                question=term,
                company_id=str(company_id),
                db_pool=DB_POOL,
                sb_client=_sb(),
            )

        rows = await _load_chunks_matching_term(company_id, term)

        if tool_name == "exact_count":
            base_answer = _build_exact_count_answer(term, rows)
            # If raw count is 0, check entity_mentions as fallback for entity-like tokens
            zero_result = (
                "I found 0 occurrences" in base_answer
                or "I found 0 standalone" in base_answer
                or "Postgres found" in base_answer
            )
            if zero_result and _looks_like_entity_token(term):
                entity_fallback = await _entity_mention_count_fallback(uuid.UUID(str(company_id)), term)
                if entity_fallback:
                    return entity_fallback
            return base_answer

        if tool_name == "files_mentioning":
            return _build_files_mentioning_answer(term, rows)

        return "I could not route that tool request."

    except Exception as e:
        import traceback
        print(f"Deterministic tool failed: tool={tool_name} term={term!r} company={company_id!r}")
        print(traceback.format_exc())

        if tool_name == "structured_calc":
            return (
                "[structured-calc-v4] I could not complete that calculation because the backend hit an internal error. "
                "The normal document search still works. Try rephrasing as 'total revenue from Reeves 14H' or "
                "'show all clients ranked by total amount'."
            )

        return (
            f'I could not complete the exact search for "{term}". '
            "Please try rephrasing your question, or ask me to summarize documents instead."
        )


# ── Entity-token helpers for exact_count fallback ─────────────────────────────

def _looks_like_entity_token(term: str) -> bool:
    """True if the term looks like a structured entity ID (T-04, FT-9001, Reeves 14H)
    rather than a plain English word like 'time' or 'pump'."""
    t = term.strip()
    return bool(
        re.search(r"[A-Za-z][-\s]?\d", t)          # letter then digit: T-04, Reeves 14H
        or re.search(r"\d+[A-Za-z]$", t)            # digit-letter suffix: 14H
        or re.fullmatch(r"[A-Z]{1,4}-?\d{2,}", t)  # pure ID: T-04, FT9001
    )


async def _entity_mention_count_fallback(company_id: uuid.UUID, term: str) -> str | None:
    """Query entity_mentions when raw text count is 0 for an entity-like token.
    Returns a formatted message, or None if no entity matches found."""
    search_lower = term.lower().strip()
    variants = [search_lower, search_lower.replace("-", ""), search_lower.replace("-", " ")]
    variants = list(dict.fromkeys(variants))  # deduplicate, preserve order

    try:
        if DB_POOL:
            async with DB_POOL.acquire() as c:
                conditions = " or ".join(
                    f"(lower(canonical_value) ilike ${2+i} or lower(value) ilike ${2+i})"
                    for i in range(len(variants))
                )
                params: list = [company_id] + [f"%{v}%" for v in variants]
                rows = await c.fetch(
                    f"""
                    select em.entity_type, em.canonical_value,
                           count(*) as mention_count,
                           count(distinct em.file_version_id) as file_count
                    from entity_mentions em
                    where em.company_id = $1
                      and ({conditions})
                    group by em.entity_type, em.canonical_value
                    order by mention_count desc
                    limit 20
                    """,
                    *params,
                )
                results = [dict(r) for r in rows]
        else:
            sb = _sb()
            results = []
            for v in variants:
                res = (
                    sb.table("entity_mentions")
                    .select("entity_type,canonical_value,file_version_id")
                    .eq("company_id", str(company_id))
                    .ilike("canonical_value", f"%{v}%")
                    .limit(200)
                    .execute()
                    .data or []
                )
                if res:
                    agg: dict = {}
                    for r in res:
                        key = (r["entity_type"], r["canonical_value"])
                        if key not in agg:
                            agg[key] = {"mention_count": 0, "file_count": set()}
                        agg[key]["mention_count"] += 1
                        agg[key]["file_count"].add(r.get("file_version_id"))
                    results = [
                        {"entity_type": k[0], "canonical_value": k[1],
                         "mention_count": v2["mention_count"], "file_count": len(v2["file_count"])}
                        for k, v2 in agg.items()
                    ]
                    break

        if not results:
            return None

        total_mentions = sum(r["mention_count"] for r in results)
        lines = [
            f"[deterministic-v7] Found **0 raw text occurrences** of \"{term}\" in document text.",
            "",
            f"However, the **entity index** contains **{total_mentions} mention{'s' if total_mentions != 1 else ''}**:",
            "",
        ]
        for r in results[:10]:
            lines.append(
                f"- `{r['canonical_value']}` ({r['entity_type']}) — "
                f"{r['mention_count']} mention{'s' if r['mention_count'] != 1 else ''} in {r['file_count']} file{'s' if r['file_count'] != 1 else ''}"
            )
        lines.append("")
        lines.append("*Entity data is extracted from structured CSV/Excel fields — the term may not appear as literal text in PDFs or emails.*")
        return "\n".join(lines)

    except Exception as e:
        print(f"_entity_mention_count_fallback error: {e}")
        return None


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

    # Entity-aware intents: answer from entity_mentions, not RAG.
    entity_answer = await _dispatch_entity_intent(q, cid)
    if entity_answer is not None:
        # Determine which sub-intent for logging
        if detect_entity_summary_intent(q):
            entity_intent_type = "entity_summary"
        elif detect_entity_files_intent(q):
            entity_intent_type = "entity_files"
        else:
            entity_intent_type = "entity_lookup"
        return StreamingResponse(
            _stream_direct_answer(
                sid, cid, uid, q, entity_answer, new_count, history, srow, started_at,
                intent_type=entity_intent_type,
            ),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    # Risk/intel-aware intents: answer from file_intelligence, not RAG.
    intel_result = await _dispatch_intel_intent(q, cid)
    if intel_result is not None:
        intel_answer, intel_intent_type = intel_result
        return StreamingResponse(
            _stream_direct_answer(
                sid, cid, uid, q, intel_answer, new_count, history, srow, started_at,
                intent_type=intel_intent_type,
            ),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    # Operational issues intents: answer from operational_issues table, not RAG.
    issues_intent = detect_operational_issues_intent(q)
    if issues_intent is not None:
        issues_answer = await run_operational_issues_answer(cid, issues_intent)
        return StreamingResponse(
            _stream_direct_answer(
                sid, cid, uid, q, issues_answer, new_count, history, srow, started_at,
                intent_type="operational_issues",
            ),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    # Deterministic tools: exact counts/searches should use SQL/Python, not Claude.
    deterministic_intent = _detect_deterministic_tool_intent(q)
    if deterministic_intent:
        tool_name, term = deterministic_intent
        # For structured_calc, pass the original question as the term
        effective_term = q if tool_name == "structured_calc" else term
        answer = await _run_deterministic_tool(cid, tool_name, effective_term)
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


# ── Operational Issues endpoints ─────────────────────────────────────────────

class IssueStatusBody(BaseModel):
    status: str

class RunDetectionBody(BaseModel):
    file_version_id: Optional[str] = None

@app.post("/issues/run-detection")
async def post_run_detection(body: RunDetectionBody = RunDetectionBody(), u=Depends(get_user)):
    cid_str = str(u["company_id"])
    sb = _sb()
    fvid = body.file_version_id or None
    result = await asyncio.get_event_loop().run_in_executor(
        None, run_issue_detection, sb, cid_str, fvid
    )
    return {"status": "ok", "result": result}

@app.get("/issues")
async def get_issues(
    status: Optional[str] = None,
    issue_type: Optional[str] = None,
    severity: Optional[str] = None,
    rule_key: Optional[str] = None,
    limit: int = 50,
    include_evidence: bool = False,
    u=Depends(get_user),
):
    cid = uuid.UUID(str(u["company_id"]))
    issues = []
    if DB_POOL:
        async with DB_POOL.acquire() as c:
            filters = ["company_id=$1"]
            params: list = [cid]
            idx = 2
            if status is not None:
                filters.append(f"status=${idx}"); params.append(status); idx += 1
            if severity is not None:
                filters.append(f"severity=${idx}"); params.append(severity); idx += 1
            if issue_type is not None:
                filters.append(f"issue_type=${idx}"); params.append(issue_type); idx += 1
            if rule_key is not None:
                filters.append(f"rule_key=${idx}"); params.append(rule_key); idx += 1
            params.append(min(limit, 200))
            where = " AND ".join(filters)
            rows = await c.fetch(
                f"SELECT * FROM operational_issues WHERE {where} ORDER BY created_at DESC LIMIT ${idx}",
                *params,
            )
            issues = [dict(r) for r in rows]
            if include_evidence and issues:
                issue_ids = [uuid.UUID(str(i["id"])) for i in issues]
                placeholders = ",".join(f"${j+1}" for j in range(len(issue_ids)))
                ev_rows = await c.fetch(
                    f"SELECT * FROM issue_evidence WHERE issue_id IN ({placeholders})",
                    *issue_ids,
                )
                ev_map: dict = {}
                for ev in ev_rows:
                    d = dict(ev)
                    ev_map.setdefault(str(d["issue_id"]), []).append(d)
                for issue in issues:
                    issue["evidence"] = ev_map.get(str(issue["id"]), [])
    else:
        sb = _sb()
        q = sb.table("operational_issues").select("*").eq("company_id", str(cid))
        if status is not None:
            q = q.eq("status", status)
        if severity is not None:
            q = q.eq("severity", severity)
        if issue_type is not None:
            q = q.eq("issue_type", issue_type)
        if rule_key is not None:
            q = q.eq("rule_key", rule_key)
        issues = q.order("created_at", desc=True).limit(min(limit, 200)).execute().data or []
        if include_evidence and issues:
            issue_ids = [str(i["id"]) for i in issues]
            ev_res = sb.table("issue_evidence").select("*").in_("issue_id", issue_ids).execute()
            ev_map2: dict = {}
            for ev in (ev_res.data or []):
                ev_map2.setdefault(str(ev["issue_id"]), []).append(ev)
            for issue in issues:
                issue["evidence"] = ev_map2.get(str(issue["id"]), [])
    return issues

@app.get("/issues/{issue_id}")
async def get_issue(issue_id: str, u=Depends(get_user)):
    cid = uuid.UUID(str(u["company_id"]))
    issue = None
    evidence = []
    if DB_POOL:
        async with DB_POOL.acquire() as c:
            row = await c.fetchrow(
                "SELECT * FROM operational_issues WHERE id=$1",
                uuid.UUID(issue_id),
            )
            if row is None or uuid.UUID(str(row["company_id"])) != cid:
                raise HTTPException(404, "Issue not found")
            issue = dict(row)
            ev_rows = await c.fetch(
                "SELECT * FROM issue_evidence WHERE issue_id=$1 ORDER BY created_at",
                uuid.UUID(issue_id),
            )
            evidence = [dict(r) for r in ev_rows]
    else:
        sb = _sb()
        res = sb.table("operational_issues").select("*").eq("id", issue_id).execute()
        if not res.data or uuid.UUID(str(res.data[0]["company_id"])) != cid:
            raise HTTPException(404, "Issue not found")
        issue = res.data[0]
        ev_res = sb.table("issue_evidence").select("*").eq("issue_id", issue_id).execute()
        evidence = ev_res.data or []
    issue["evidence"] = evidence
    return issue

@app.post("/issues/{issue_id}/status")
async def update_issue_status(issue_id: str, body: IssueStatusBody, u=Depends(get_user)):
    cid = uuid.UUID(str(u["company_id"]))
    valid_statuses = {"candidate", "confirmed", "dismissed", "resolved", "ignored"}
    if body.status not in valid_statuses:
        raise HTTPException(400, f"Invalid status. Must be one of: {', '.join(sorted(valid_statuses))}")
    now = datetime.utcnow()
    if DB_POOL:
        async with DB_POOL.acquire() as c:
            row = await c.fetchrow(
                "SELECT company_id FROM operational_issues WHERE id=$1",
                uuid.UUID(issue_id),
            )
            if row is None or uuid.UUID(str(row["company_id"])) != cid:
                raise HTTPException(404, "Issue not found")
            if body.status == "resolved":
                await c.execute(
                    "UPDATE operational_issues SET status=$1, updated_at=$2, resolved_at=$3 WHERE id=$4",
                    body.status, now, now, uuid.UUID(issue_id),
                )
            elif body.status in ("candidate", "confirmed"):
                await c.execute(
                    "UPDATE operational_issues SET status=$1, updated_at=$2, resolved_at=NULL WHERE id=$3",
                    body.status, now, uuid.UUID(issue_id),
                )
            else:
                await c.execute(
                    "UPDATE operational_issues SET status=$1, updated_at=$2 WHERE id=$3",
                    body.status, now, uuid.UUID(issue_id),
                )
    else:
        sb = _sb()
        check = sb.table("operational_issues").select("company_id").eq("id", issue_id).execute()
        if not check.data or uuid.UUID(str(check.data[0]["company_id"])) != cid:
            raise HTTPException(404, "Issue not found")
        payload: dict = {"status": body.status, "updated_at": now.isoformat()}
        if body.status == "resolved":
            payload["resolved_at"] = now.isoformat()
        elif body.status in ("candidate", "confirmed"):
            payload["resolved_at"] = None
        sb.table("operational_issues").update(payload).eq("id", issue_id).execute()
    return {"id": issue_id, "status": body.status}


# ── File intelligence endpoints ───────────────────────────────────────────────

@app.get("/files/intelligence")
async def list_file_intelligence(
    category:      Optional[str] = None,
    document_type: Optional[str] = None,
    needs_review:  Optional[bool] = None,
    search:        Optional[str] = None,
    u=Depends(get_user),
):
    """
    Returns file_intelligence rows for the authenticated user's company.
    Optional filters: category, document_type, needs_review, search (scans short_summary).
    """
    cid = uuid.UUID(str(u["company_id"]))

    if DB_POOL:
        async with DB_POOL.acquire() as c:
            # Build dynamic WHERE clauses
            conditions = ["fi.company_id = $1", "f.deleted_at is null"]
            params: list = [cid]
            idx = 2

            if category:
                conditions.append(f"fi.category = ${idx}")
                params.append(category)
                idx += 1
            if document_type:
                conditions.append(f"fi.document_type = ${idx}")
                params.append(document_type)
                idx += 1
            if needs_review is not None:
                conditions.append(f"fi.needs_review = ${idx}")
                params.append(needs_review)
                idx += 1
            if search:
                conditions.append(f"fi.short_summary ilike ${idx}")
                params.append(f"%{search}%")
                idx += 1

            where = " AND ".join(conditions)
            rows = await c.fetch(
                f"""
                select
                    fi.*,
                    f.logical_name,
                    fv.original_filename,
                    fv.status as version_status,
                    fv.created_at as version_created_at
                from file_intelligence fi
                join file_versions fv on fv.id = fi.file_version_id
                join files f on f.id = fi.file_id
                where {where}
                order by fi.created_at desc
                limit 200
                """,
                *params,
            )
            return [dict(r) for r in rows]

    # Supabase fallback
    sb = _sb()
    cid_str = str(cid)
    q = sb.table("file_intelligence").select(
        "*, file_versions(original_filename, status, created_at), files(logical_name, deleted_at)"
    ).eq("company_id", cid_str)

    if category:
        q = q.eq("category", category)
    if document_type:
        q = q.eq("document_type", document_type)
    if needs_review is not None:
        q = q.eq("needs_review", needs_review)
    if search:
        q = q.ilike("short_summary", f"%{search}%")

    rows = q.order("created_at", desc=True).limit(200).execute().data or []
    # Filter out deleted files
    return [r for r in rows if not (r.get("files") or {}).get("deleted_at")]


@app.get("/files/{file_id}/intelligence")
async def get_file_intelligence(file_id: str, u=Depends(get_user)):
    """
    Returns file_intelligence row + top entity_mentions for a single file.
    Company-scoped.
    """
    cid = uuid.UUID(str(u["company_id"]))
    try:
        fid = uuid.UUID(file_id)
    except ValueError:
        raise HTTPException(400, "Invalid file_id")

    if DB_POOL:
        async with DB_POOL.acquire() as c:
            intel = await c.fetchrow(
                """
                select fi.*, fv.original_filename, fv.status as version_status
                from file_intelligence fi
                join file_versions fv on fv.id = fi.file_version_id
                join files f on f.id = fi.file_id
                where fi.file_id = $1
                  and fi.company_id = $2
                  and f.deleted_at is null
                order by fi.created_at desc
                limit 1
                """,
                fid, cid,
            )
            if not intel:
                raise HTTPException(404, "No intelligence record found for this file")

            entities = await c.fetch(
                """
                select entity_type, value, canonical_value, source_field,
                       source_row, confidence_score, extraction_method
                from entity_mentions
                where file_id = $1
                  and company_id = $2
                order by entity_type, canonical_value
                limit 500
                """,
                fid, cid,
            )
            return {
                "intelligence": dict(intel),
                "entities": [dict(e) for e in entities],
            }

    sb = _sb()
    cid_str = str(cid)
    intel_rows = (
        sb.table("file_intelligence")
        .select("*")
        .eq("file_id", file_id)
        .eq("company_id", cid_str)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data or []
    )
    if not intel_rows:
        raise HTTPException(404, "No intelligence record found for this file")

    entities = (
        sb.table("entity_mentions")
        .select("entity_type,value,canonical_value,source_field,source_row,confidence_score,extraction_method")
        .eq("file_id", file_id)
        .eq("company_id", cid_str)
        .order("entity_type")
        .limit(500)
        .execute()
        .data or []
    )
    return {"intelligence": intel_rows[0], "entities": entities}


@app.get("/entities")
async def list_entities(
    entity_type: Optional[str] = None,
    search:      Optional[str] = None,
    limit:       int = 50,
    u=Depends(get_user),
):
    """
    Query entity_mentions for the authenticated user's company.
    Optional: entity_type filter, value/canonical_value search.
    """
    cid = uuid.UUID(str(u["company_id"]))
    limit = max(1, min(limit, 500))

    if DB_POOL:
        async with DB_POOL.acquire() as c:
            conditions = ["em.company_id = $1"]
            params: list = [cid]
            idx = 2

            if entity_type:
                conditions.append(f"em.entity_type = ${idx}")
                params.append(entity_type)
                idx += 1
            if search:
                conditions.append(f"em.canonical_value ilike ${idx}")
                params.append(f"%{search.lower()}%")
                idx += 1

            # limit is a separate clause, not part of WHERE
            params.append(limit)
            limit_ph = f"${idx}"
            where = " AND ".join(conditions)
            rows = await c.fetch(
                f"""
                select
                    em.entity_type,
                    em.canonical_value,
                    em.value,
                    em.source_field,
                    em.source_row,
                    em.confidence_score,
                    em.extraction_method,
                    fv.original_filename,
                    em.file_version_id
                from entity_mentions em
                join file_versions fv on fv.id = em.file_version_id
                where {where}
                order by em.entity_type, em.canonical_value
                limit {limit_ph}
                """,
                *params,
            )
            return [dict(r) for r in rows]

    sb = _sb()
    cid_str = str(cid)
    q = (
        sb.table("entity_mentions")
        .select("entity_type,canonical_value,value,source_field,source_row,confidence_score,extraction_method,file_version_id")
        .eq("company_id", cid_str)
    )
    if entity_type:
        q = q.eq("entity_type", entity_type)
    if search:
        q = q.ilike("canonical_value", f"%{search.lower()}%")

    rows = q.order("entity_type").order("canonical_value").limit(limit).execute().data or []
    return rows


@app.get("/entities/summary")
async def entities_summary(u=Depends(get_user)):
    """
    Returns grouped counts of unique canonical values per key entity type.
    E.g. {"trucks": 7, "drivers": 3, "clients": 4, "leases": 12, ...}
    """
    cid = uuid.UUID(str(u["company_id"]))
    types_to_count = ["truck", "driver", "client", "lease", "ticket_id", "invoice_id", "po_number", "job_id"]

    if DB_POOL:
        async with DB_POOL.acquire() as c:
            rows = await c.fetch(
                """
                select entity_type, count(distinct canonical_value) as distinct_count
                from entity_mentions
                where company_id = $1
                  and entity_type = any($2::text[])
                group by entity_type
                """,
                cid, types_to_count,
            )
            raw = {r["entity_type"]: r["distinct_count"] for r in rows}
    else:
        sb = _sb()
        cid_str = str(cid)
        raw: dict = {}
        for etype in types_to_count:
            try:
                res = (
                    sb.table("entity_mentions")
                    .select("canonical_value")
                    .eq("company_id", cid_str)
                    .eq("entity_type", etype)
                    .execute()
                )
                raw[etype] = len({r["canonical_value"] for r in (res.data or [])})
            except Exception:
                raw[etype] = 0

    label_map = {
        "truck":      "trucks",
        "driver":     "drivers",
        "client":     "clients",
        "lease":      "leases",
        "ticket_id":  "ticket_ids",
        "invoice_id": "invoice_ids",
        "po_number":  "po_numbers",
        "job_id":     "job_ids",
    }
    return {label_map[k]: int(raw.get(k, 0)) for k in types_to_count}


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
