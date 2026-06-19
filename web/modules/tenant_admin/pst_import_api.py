"""
modules/tenant_admin/pst_import_api.py
JSON API for the tenant-admin Firm Email Archive (PST import) — B1.

A SEPARATE collection point from eDiscovery: an admin points at a PST already on
a mounted share (PSTs are GB-scale, so server-side path beats HTTP upload),
picks a routing tag (firm_archive=global dedup / custodian=custodian-scoped),
and the collector (jobs/pst_collector.run_batch) extracts via pffexport, parses,
dedups, and lands messages in pst_messages + the shared email search vector base.

Runs the collector in a background thread in THIS container (praesidium-web has
pff-tools). Once praesidium-platform:latest is rebuilt (Dockerfile already
carries pff-tools), this can move to the ediscovery RQ queue for the worker pool.
"""
import logging
import os
import threading

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/pst-import", tags=["pst-import-api"])

ALLOWED_ROOTS = ("/mnt/legacy", "/mnt/praesidium", "/mnt/clients", "/mnt/docsend")


def _tid(request):
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _uid(request):
    u = getattr(request.state, "current_user", None)
    return getattr(u, "id", None) or getattr(u, "user_id", None)


def _safe_pst_path(p):
    p = os.path.realpath(p or "")
    if not p.lower().endswith(".pst"):
        return None
    if not any(p == r or p.startswith(r + "/") for r in ALLOWED_ROOTS):
        return None
    return p if os.path.isfile(p) else None


def _jsonable(row):
    d = dict(row)
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


@router.get("/batches")
async def list_batches(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT id::text, source_filename, routing_tag, custodian_label, status, "
            "       total_messages, imported_messages, duplicate_messages, error, "
            "       created_at, completed_at "
            "FROM pst_import_batches WHERE TRIM(tenant_id)=:t "
            "ORDER BY created_at DESC LIMIT 200"), {"t": tid})).mappings().all()
    return JSONResponse({"batches": [_jsonable(r) for r in rows]})


@router.get("/browse")
async def browse_psts(request: Request, prefix: str = "/mnt/legacy",
                      user=Depends(get_current_user)):
    """List subfolders + .pst files under an allowed root (non-recursive)."""
    base = os.path.realpath(prefix or "/mnt/legacy")
    if not any(base == r or base.startswith(r + "/") or r.startswith(base) for r in ALLOWED_ROOTS):
        return JSONResponse({"error": "path not allowed"}, status_code=400)
    folders, psts = [], []
    try:
        with os.scandir(base) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    folders.append({"name": e.name, "path": e.path})
                elif e.name.lower().endswith(".pst"):
                    try:
                        sz = e.stat().st_size
                    except OSError:
                        sz = 0
                    psts.append({"name": e.name, "path": e.path, "size": sz})
    except OSError as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)
    folders.sort(key=lambda x: x["name"].lower())
    psts.sort(key=lambda x: x["name"].lower())
    return JSONResponse({"path": base, "folders": folders, "psts": psts})


def _run_batch_thread(batch_id):
    try:
        from jobs.pst_collector import run_batch
        run_batch(batch_id)
    except Exception:
        logger.exception("pst run_batch thread failed for %s", batch_id)


@router.post("/start")
async def start_import(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    uid = _uid(request)
    body = await request.json()
    storage_path = _safe_pst_path(body.get("storage_path"))
    if not storage_path:
        return JSONResponse(
            {"error": "storage_path must be an existing .pst under an allowed mount"},
            status_code=400)
    routing_tag = body.get("routing_tag") or "firm_archive"
    if routing_tag not in ("firm_archive", "custodian"):
        return JSONResponse({"error": "routing_tag must be firm_archive or custodian"},
                            status_code=400)
    custodian_label = (body.get("custodian_label") or "").strip() or None
    if routing_tag == "custodian" and not custodian_label:
        return JSONResponse({"error": "custodian routing requires custodian_label"},
                            status_code=400)
    fname = body.get("source_filename") or os.path.basename(storage_path)
    try:
        size = os.path.getsize(storage_path)
    except OSError:
        size = None
    async with AsyncSessionLocal() as db:
        bid = (await db.execute(text(
            "INSERT INTO pst_import_batches (tenant_id, uploaded_by, source_filename, "
            " storage_path, file_size_bytes, routing_tag, custodian_label, status) "
            "VALUES (:t, :u, :f, :p, :sz, :rt, :cl, 'pending') RETURNING id::text"),
            {"t": tid, "u": uid, "f": fname, "p": storage_path, "sz": size,
             "rt": routing_tag, "cl": custodian_label})).scalar()
        await db.commit()
    threading.Thread(target=_run_batch_thread, args=(bid,), daemon=True).start()
    logger.info("pst-import started batch=%s path=%s tag=%s", bid, storage_path, routing_tag)
    return JSONResponse({"ok": True, "batch_id": bid, "status": "pending"})
