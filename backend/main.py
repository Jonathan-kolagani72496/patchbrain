"""
PatchBrain FastAPI backend - v3
Performance: async/await everywhere, asyncpg connection pool,
streaming Claude responses, smart context window, parallel DB calls.
"""

import os
import uuid
import json
import asyncio
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

load_dotenv()

app = FastAPI(title="PatchBrain API", version="3.0.0")

FRONTEND_URL = os.environ.get("FRONTEND_URL", "")
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

# ── /ask  (streaming) ──────────────────────────────────────────────────────────

class AskBody(BaseModel):
    question:   str
    session_id: Optional[str] = None

@app.post("/ask")
async def ask(b: AskBody, u=Depends(get_user)):
    cid  = uuid.UUID(str(u["company_id"]))
    uid  = uuid.UUID(str(u["id"]))
    q    = b.question.strip()
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
    # Embed
    try:
        emb = await ai.embeddings.create(model="text-embedding-3-small", input=q)
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
            "p_query_text": q,
            "p_match_count": 10,
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
            "score":chunk.get("combined_score")})

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
            async with ant.messages.stream(
                model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
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

@app.get("/health")
async def health():
    return {"status":"ok","version":"3.0.0","pool": DB_POOL is not None}
@app.get("/conflicts")
async def list_conflicts(u=Depends(get_user)):
    cid = str(u["company_id"])
    sb = _sb()
    rows = sb.table("data_conflicts") \
        .select("*") \
        .eq("company_id", cid) \
        .order("created_at", desc=False) \
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