"""
Guided deposition-bundle ingest API.

  POST /api/v1/depositions/bundle/triage
      { matter_id, root }  (root = server folder; or paths[] of files/dirs)
      -> runs the LLM classifier over the folder and returns the proposal
         (units with parse_source / exhibits / video / flags). Echoes `root`.

  POST /api/v1/depositions/bundle/dispatch
      { matter_id, root, proposal }
      -> per confirmed unit: create depo session, register transcript (seeds the
         depo DAG), attach video (+forced-align), record exhibit links.
"""
import asyncio
import logging

from typing import List
from fastapi import APIRouter, Depends, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse

from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.services import bundle_ingest as bi

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/depositions/bundle", tags=["depositions-bundle"])


def _tenant(r: Request) -> str:
    return (getattr(r.state, "tenant_id", "") or "").strip()


def _uid(user):
    raw = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


@router.post("/triage")
async def triage(request: Request, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)
    matter_id = (body.get("matter_id") or "").strip()
    root = (body.get("root") or "").strip()
    if not matter_id or not root:
        return JSONResponse({"error": "matter_id and root required"}, status_code=400)
    import os
    if not os.path.isdir(root):
        return JSONResponse({"error": f"not a folder: {root}"}, status_code=400)
    try:
        manifest = await asyncio.get_event_loop().run_in_executor(
            None, bi.gather_signals, root)
        proposal = await bi.triage(manifest, tid, matter_id)
        await asyncio.get_event_loop().run_in_executor(
            None, bi.annotate_embedded, proposal, root)
        proposal["root"] = root
        return JSONResponse(proposal)
    except Exception as e:
        logger.exception("bundle triage failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/dispatch")
async def dispatch(request: Request, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)
    matter_id = (body.get("matter_id") or "").strip()
    root = (body.get("root") or "").strip()
    proposal = body.get("proposal") or {}
    if not matter_id or not root or not proposal.get("units"):
        return JSONResponse({"error": "matter_id, root, proposal.units required"},
                            status_code=400)
    try:
        out = await asyncio.get_event_loop().run_in_executor(
            None, bi.dispatch, tid, matter_id, root, proposal, _uid(user))
        return JSONResponse(out)
    except Exception as e:
        logger.exception("bundle dispatch failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/upload")
async def upload(request: Request, files: List[UploadFile] = File(...),
                 matter_id: str = Form(...), paths: List[str] = Form(default=[]),
                 user=Depends(get_current_user)):
    """Stream dropped files (incl. multi-GB video) into a staging folder under the
    matter's Depositions/_inbox/<batch>/ tree, preserving any relative subfolders.
    Returns {root} which the client then triages (guided ingest)."""
    import os, shutil, time, uuid, psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    tid = _tenant(request)
    if not matter_id or not files:
        return JSONResponse({"error": "matter_id and files required"}, status_code=400)
    conn = psycopg2.connect(**_db_kwargs())
    try:
        cur = conn.cursor()
        cur.execute("SELECT m.matter_name, c.client_name FROM matters m "
                    "LEFT JOIN clients c ON c.id=m.client_id AND TRIM(c.tenant_id)=TRIM(%s) "
                    "WHERE m.id=CAST(%s AS uuid) AND TRIM(m.tenant_id)=TRIM(%s)",
                    (tid, matter_id, tid))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return JSONResponse({"error": "matter not found"}, status_code=404)
    client = (row[1] or "_"); matter = (row[0] or "_")
    batch = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    base = os.path.join("/mnt/praesidium", tid.strip(), "matters", client, matter,
                        "Depositions", "_inbox", batch)
    saved = 0
    for i, f in enumerate(files):
        rel = (paths[i] if i < len(paths) else None) or f.filename or ("file%d" % i)
        parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".", "..")]
        dest = os.path.join(base, *parts) if parts else os.path.join(base, f.filename or "file")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out, length=8 * 1024 * 1024)
        saved += 1
    return JSONResponse({"root": base, "files": saved})


# ── Resumable chunked upload (cloud-anticipating) ──────────────────────────
# Session state in Redis (-> ElastiCache in cloud); bytes appended to a .part
# file at the requested offset (-> S3 multipart parts in cloud). A dropped
# connection resumes from the server's received-offset instead of restarting.
def _redis():
    import os, redis
    return redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))

def _safe_rel(rel):
    return "/".join(x for x in (rel or "").replace("\\", "/").split("/")
                    if x not in ("", "..", "."))

def _matter_inbox(tid, matter_id):
    import os, psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    try:
        cur = conn.cursor()
        cur.execute("SELECT m.matter_name, c.client_name FROM matters m "
                    "LEFT JOIN clients c ON c.id=m.client_id AND TRIM(c.tenant_id)=TRIM(%s) "
                    "WHERE m.id=CAST(%s AS uuid) AND TRIM(m.tenant_id)=TRIM(%s)",
                    (tid, matter_id, tid))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return os.path.join("/mnt/praesidium", tid.strip(), "matters",
                        (row[1] or "_"), (row[0] or "_"), "Depositions", "_inbox")


@router.post("/upload/init")
async def upload_init(request: Request, user=Depends(get_current_user)):
    import os, time, uuid, json
    tid = _tenant(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)
    matter_id = (body.get("matter_id") or "").strip()
    rel = _safe_rel(body.get("rel_path") or body.get("filename") or "")
    size = int(body.get("size") or 0)
    if not matter_id or not rel:
        return JSONResponse({"error": "matter_id and rel_path required"}, status_code=400)
    inbox = _matter_inbox(tid, matter_id)
    if not inbox:
        return JSONResponse({"error": "matter not found"}, status_code=404)
    batch = body.get("batch_id") or (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    root = os.path.join(inbox, batch)
    dest = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    upload_id = uuid.uuid4().hex
    r = _redis()
    r.hset("depo_upload:" + upload_id, mapping={"dest": dest, "size": str(size),
                                                "tid": tid, "root": root})
    r.expire("depo_upload:" + upload_id, 86400)
    received = os.path.getsize(dest + ".part") if os.path.exists(dest + ".part") else 0
    return JSONResponse({"upload_id": upload_id, "batch_id": batch, "root": root,
                         "received": received})


@router.get("/upload/state/{upload_id}")
async def upload_state(upload_id: str, request: Request, user=Depends(get_current_user)):
    import os
    m = _redis().hgetall("depo_upload:" + upload_id)
    if not m:
        return JSONResponse({"error": "no such upload"}, status_code=404)
    dest = m[b"dest"].decode()
    rec = os.path.getsize(dest + ".part") if os.path.exists(dest + ".part") else 0
    return JSONResponse({"received": rec, "size": int(m[b"size"])})


@router.put("/upload/chunk/{upload_id}")
async def upload_chunk(upload_id: str, request: Request, offset: int = 0,
                       user=Depends(get_current_user)):
    import os
    m = _redis().hgetall("depo_upload:" + upload_id)
    if not m:
        return JSONResponse({"error": "no such upload"}, status_code=404)
    dest = m[b"dest"].decode()
    part = dest + ".part"
    cur = os.path.getsize(part) if os.path.exists(part) else 0
    if offset > cur:
        return JSONResponse({"error": "gap", "received": cur}, status_code=409)
    chunk = await request.body()
    if offset < cur:                      # overlap from a retry — trim what we have
        skip = cur - offset
        chunk = chunk[skip:] if skip < len(chunk) else b""
    if chunk:
        with open(part, "ab") as fh:
            fh.write(chunk)
    return JSONResponse({"received": os.path.getsize(part)})


@router.post("/upload/finish/{upload_id}")
async def upload_finish(upload_id: str, request: Request, user=Depends(get_current_user)):
    import os
    key = "depo_upload:" + upload_id
    r = _redis()
    m = r.hgetall(key)
    if not m:
        return JSONResponse({"error": "no such upload"}, status_code=404)
    dest = m[b"dest"].decode()
    part = dest + ".part"
    if os.path.exists(part):
        os.replace(part, dest)
    r.delete(key)
    return JSONResponse({"ok": True, "path": dest, "root": m[b"root"].decode()})


@router.get("/inbox")
async def inbox_path(request: Request, matter_id: str = "", user=Depends(get_current_user)):
    """Return (creating if needed) the auto-ingest inbox drop path for a matter.
    Drop a deposition folder here (via the share) and the watcher ingests it."""
    tid = _tenant(request)
    if not matter_id:
        return JSONResponse({"error": "matter_id required"}, status_code=400)
    from modules.depositions.jobs.depo_inbox import ensure_inbox
    return JSONResponse({"path": ensure_inbox(tid, matter_id)})

