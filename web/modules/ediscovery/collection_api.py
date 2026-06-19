"""
modules/ediscovery/collection_api.py
Component 7 — Collection Drop Zone
Patent Pending — 64/020,027
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from typing import Optional

import aiofiles
import httpx
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user
from modules.dashboard.services.nav_context import get_nav_context

log = logging.getLogger("praesidium.ediscovery.collection_api")

router = APIRouter(prefix="/ediscovery", tags=["ediscovery-collections"])

templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates/ediscovery", "templates/ediscovery"])

# ── Config ────────────────────────────────────────────────────────────────────
CIFS_URL = os.environ.get("CIFS_URL", "http://10.10.60.13:8080")
UPLOAD_TMP_DIR = os.environ.get("UPLOAD_TMP_DIR", "/tmp/praesidium_uploads")
COLLECTION_STAGING_ROOT = os.environ.get("COLLECTION_STAGING_ROOT", "/mnt/praesidium/staging")
MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB
ALLOWED_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".eml", ".msg",
    ".xlsx", ".xls", ".tiff", ".tif", ".txt",
    ".csv", ".pptx", ".ppt", ".rtf", ".htm", ".html",
}

os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)


# ── Pydantic models ───────────────────────────────────────────────────────────
class CreateCollectionRequest(BaseModel):
    name: str
    description: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────
def _ext_ok(filename: str) -> bool:
    _, ext = os.path.splitext(filename.lower())
    return ext in ALLOWED_EXTENSIONS


async def _check_dms_hash(file_hash: str) -> tuple[Optional[str], Optional[float]]:
    """Read-only DMS cross-reference. Returns (match_id, confidence) or (None, None).
    DISCOVERY/DMS SEPARATION: we never write to DMS from here."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{CIFS_URL}/api/documents/hash/{file_hash}")
            if resp.status_code == 200:
                data = resp.json()
                return data.get("id"), data.get("confidence", 1.0)
    except Exception as exc:
        log.warning("DMS hash check failed (non-fatal): %s", exc)
    return None, None


async def _get_tenant_id(request: Request) -> str:
    # get_current_user is sync — no await
    try:
        return request.state.tenant_id.strip()
    except AttributeError:
        user = get_current_user(request)
        tid = user.tenant_id if hasattr(user, 'tenant_id') else user['tenant_id']
        return tid.strip()


async def _get_user_id(request: Request) -> Optional[str]:
    try:
        user = get_current_user(request)
        if hasattr(user, 'id'):
            return str(user.id)
        return user.get('id')
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Collections CRUD
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/collections")
async def all_collections(request: Request):
    """Redirect old collections page to new React imports page."""
    from fastapi.responses import RedirectResponse
    matter_id = request.query_params.get("matter_id", "")
    url = "/ediscovery/imports"
    if matter_id:
        url += "?matter_id=" + matter_id
    return RedirectResponse(url=url, status_code=302)


@router.get("/review", response_class=HTMLResponse)
async def review_landing(request: Request):
    """
    Review landing page — matters that have at least one ediscovery collection.
    Click a matter to go to its collection list.
    If no matters have collections, prompt to create one.
    """
    tenant_id = request.state.tenant_id.strip()
    branding = getattr(request.state, "branding", None)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT
                    m.id::text AS matter_id,
                    m.matter_name,
                    m.matter_number,
                    m.status AS matter_status,
                    COUNT(c.id) AS collection_count,
                    MAX(c.created_at) AS last_collection_at,
                    SUM(c.total_docs) AS total_docs,
                    SUM(c.reviewed_docs) AS reviewed_docs,
                    BOOL_OR(c.status = 'review_ready') AS has_ready
                FROM matters m
                JOIN ediscovery_collections c
                    ON c.matter_id = m.id
                    AND c.tenant_id = m.tenant_id
                WHERE m.tenant_id = :tid
                GROUP BY m.id, m.matter_name, m.matter_number, m.status
                ORDER BY last_collection_at DESC
            """),
            {"tid": tenant_id},
        )
        matters = [dict(r) for r in result.mappings().fetchall()]

    nav = await get_nav_context(request, page="ediscovery")
    return templates.TemplateResponse(request, "ediscovery_review.html", {
        "nav": nav,
        "matters": matters,
        "branding": branding,
        "edisco_tab": "review",
    })


@router.get("/matters/{matter_id}/collections", response_class=HTMLResponse)
async def list_collections(matter_id: str, request: Request):
    """List all collections for a matter — uses ediscovery_collections."""
    tenant_id = request.state.tenant_id.strip()
    branding = getattr(request.state, "branding", None)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT
                    c.id, c.collection_name, c.source_party, c.source_type,
                    c.received_date, c.received_method, c.status,
                    c.total_docs, c.processed_docs, c.reviewed_docs,
                    c.created_at
                FROM ediscovery_collections c
                WHERE c.tenant_id = :tid AND c.matter_id = CAST(:mid AS uuid)
                ORDER BY c.created_at DESC
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        collections = [dict(r) for r in result.mappings().fetchall()]

        matter_result = await session.execute(
            text("SELECT id, matter_name FROM matters WHERE id = CAST(:mid AS uuid) AND tenant_id = :tid"),
            {"mid": matter_id, "tid": tenant_id},
        )
        matter = matter_result.mappings().fetchone()

    if not matter:
        raise HTTPException(status_code=404, detail="Matter not found")

    nav = await get_nav_context(request, page="ediscovery")
    return templates.TemplateResponse(request, "collection_list.html", {
        "nav": nav,
        "collections": collections,
        "matter": dict(matter),
        "branding": branding,
        "edisco_tab": "collections",
    })


@router.delete("/collections/{collection_id}")
async def remove_collection(collection_id: str, request: Request):
    """Remove a FAILED collection: tear down its documents + derived data and
    drop the collection row. Guarded to status='failed' so the UI action can
    never nuke a live collection. The heavy psycopg2 teardown runs off the
    event loop."""
    tenant_id = request.state.tenant_id.strip()
    from starlette.concurrency import run_in_threadpool
    from modules.ediscovery.jobs.teardown_collection import delete_ediscovery_collection
    result = await run_in_threadpool(
        delete_ediscovery_collection, tenant_id, collection_id,
        drop_collection=True, require_failed=True)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    log.info("removed failed collection %s: %s", collection_id, result)
    return JSONResponse(
        {"ok": True, "removed": result},
        headers={"HX-Redirect": request.headers.get("referer") or "/ediscovery"})


@router.post("/matters/{matter_id}/collections")
async def create_collection(matter_id: str, request: Request, body: CreateCollectionRequest):
    """Create a new named collection for a matter."""
    tenant_id = await _get_tenant_id(request)
    user_id = await _get_user_id(request)
    collection_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as session:
        # Verify matter belongs to tenant
        r = await session.execute(
            text("SELECT id FROM matters WHERE id = :mid AND tenant_id = :tid"),
            {"mid": matter_id, "tid": tenant_id},
        )
        if not r.fetchone():
            raise HTTPException(status_code=404, detail="Matter not found")

        await session.execute(
            text("""
                INSERT INTO collections (id, tenant_id, matter_id, name, description, created_by)
                VALUES (:id, :tid, :mid, :name, :desc, :uid)
            """),
            {
                "id": collection_id,
                "tid": tenant_id,
                "mid": matter_id,
                "name": body.name,
                "desc": body.description,
                "uid": user_id,
            },
        )
        await session.commit()

    return JSONResponse({"id": collection_id, "status": "active", "name": body.name})


@router.get("/collections/new", response_class=HTMLResponse)
async def collection_new(request: Request):
    """
    New collection form — select matter, source type, and metadata.
    /collections/new must be registered BEFORE /collections/{collection_id}
    to prevent 'new' being matched as a collection_id.
    """
    tenant_id = await _get_tenant_id(request)
    branding = getattr(request.state, "branding", None)

    async with AsyncSessionLocal() as session:
        matters_result = await session.execute(
            text("""
                SELECT id::text, matter_name
                FROM matters
                WHERE tenant_id = :tid
                  AND status = 'active'
                ORDER BY matter_name ASC
            """),
            {"tid": tenant_id},
        )
        matters = [dict(r) for r in matters_result.mappings().fetchall()]

    nav = await get_nav_context(request, page="collections")
    return templates.TemplateResponse(request, "collection_create.html", {
        "nav": nav,
        "matters": matters,
        "branding": branding,
        "edisco_tab": "review",
    })


@router.get("/collections/{collection_id}", response_class=HTMLResponse)
async def collection_detail(collection_id: str, request: Request):
    """Three-panel drop zone UI for a collection."""
    tenant_id = await _get_tenant_id(request)
    branding = getattr(request.state, "branding", None)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT c.id, c.name, c.description, c.status,
                       c.matter_id, c.created_at,
                       m.matter_name
                FROM collections c
                JOIN matters m ON m.id = c.matter_id
                WHERE c.id = :cid AND c.tenant_id = :tid
            """),
            {"cid": collection_id, "tid": tenant_id},
        )
        collection = result.mappings().fetchone()

    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    nav = await get_nav_context(request, page="ediscovery", matter_name=collection["matter_name"])
    return templates.TemplateResponse(request, "collection_detail.html", {
        "nav": nav,
        "collection": dict(collection),
        "branding": getattr(request.state, "branding", None),
    })


# ══════════════════════════════════════════════════════════════════════════════
# Upload
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/collections/{collection_id}/upload")
async def upload_document(
    collection_id: str,
    request: Request,
    file: UploadFile = File(...),
):
    """
    Chunked file upload with SHA-256 dedup, DMS cross-reference, and RQ enqueue.
    File is saved to shared staging mount (/mnt/praesidium/staging) so RQ workers
    on PROC-01 can read it. Returns immediately — ingestion happens async via RQ.
    """
    from redis import Redis
    from rq import Queue

    tenant_id = await _get_tenant_id(request)
    user_id = await _get_user_id(request)

    # ── Extension check ───────────────────────────────────────────────────────
    if not _ext_ok(file.filename or ""):
        raise HTTPException(status_code=400, detail="File type not permitted")

    # ── Read + size check + hash — write to local tmp first ───────────────────
    tmp_path = os.path.join(UPLOAD_TMP_DIR, f"{uuid.uuid4()}_{file.filename}")
    sha256 = hashlib.sha256()
    size = 0

    try:
        async with aiofiles.open(tmp_path, "wb") as f_out:
            while chunk := await file.read(1024 * 1024):  # 1 MB chunks
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="File exceeds 500 MB limit")
                sha256.update(chunk)
                await f_out.write(chunk)
    except HTTPException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    file_hash = sha256.hexdigest()

    async with AsyncSessionLocal() as session:
        # ── Verify collection ─────────────────────────────────────────────────
        r = await session.execute(
            text("SELECT id, status FROM collections WHERE id = :cid AND tenant_id = :tid"),
            {"cid": collection_id, "tid": tenant_id},
        )
        collection = r.mappings().fetchone()
        if not collection:
            os.unlink(tmp_path)
            raise HTTPException(status_code=404, detail="Collection not found")
        if collection["status"] != "active":
            os.unlink(tmp_path)
            raise HTTPException(status_code=409, detail="Collection is not active")

        # ── SHA-256 dedup ─────────────────────────────────────────────────────
        dup = await session.execute(
            text("""
                SELECT id FROM collection_documents
                WHERE collection_id = :cid AND file_hash = :hash
            """),
            {"cid": collection_id, "hash": file_hash},
        )
        if dup.fetchone():
            os.unlink(tmp_path)
            raise HTTPException(status_code=409, detail="Duplicate file — already in collection")

        # ── DMS cross-reference (read-only) ───────────────────────────────────
        dms_match_id, dms_confidence = await _check_dms_hash(file_hash)
        upload_status = "dms_duplicate" if dms_match_id else "pending"

        # ── Insert collection_documents row ───────────────────────────────────
        doc_id = str(uuid.uuid4())
        await session.execute(
            text("""
                INSERT INTO collection_documents
                  (id, tenant_id, collection_id, original_filename, file_hash,
                   file_size_bytes, upload_status, dms_match_id,
                   dms_match_confidence, uploaded_by)
                VALUES
                  (:id, :tid, :cid, :fname, :hash,
                   :fsize, :status, :dms_id,
                   :dms_conf, :uid)
            """),
            {
                "id": doc_id,
                "tid": tenant_id,
                "cid": collection_id,
                "fname": file.filename,
                "hash": file_hash,
                "fsize": size,
                "status": upload_status,
                "dms_id": dms_match_id,
                "dms_conf": dms_confidence,
                "uid": user_id,
            },
        )
        await session.commit()

    # ── Copy file to shared staging mount so workers can read it ─────────────
    staging_path = None
    if upload_status == "pending":
        try:
            staging_dir = os.path.join(COLLECTION_STAGING_ROOT, tenant_id.strip())
            os.makedirs(staging_dir, exist_ok=True)
            staging_path = os.path.join(staging_dir, doc_id)
            shutil.copy2(tmp_path, staging_path)
            async with AsyncSessionLocal() as s_stage:
                await s_stage.execute(
                    text("UPDATE collection_documents SET staging_path = :sp WHERE id = :id"),
                    {"sp": staging_path, "id": doc_id},
                )
                await s_stage.commit()
            log.info("Staged file for doc %s at %s", doc_id, staging_path)
        except Exception as exc:
            log.error("Failed to stage file for doc %s: %s", doc_id, exc)
            staging_path = None

    # Clean up local tmp — worker reads from staging mount
    if os.path.exists(tmp_path):
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    # ── Enqueue RQ job (only if not flagged as DMS duplicate) ─────────────────
    rq_job_id = None
    if upload_status == "pending" and staging_path:
        try:
            REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
            redis_conn = Redis.from_url(REDIS_URL)
            q = Queue("ediscovery", connection=redis_conn)
            job = q.enqueue(
                "jobs.ingest_collection_document.run",
                doc_id,
                job_timeout=3600,
            )
            rq_job_id = job.id
            # Store job id for polling
            async with AsyncSessionLocal() as session2:
                await session2.execute(
                    text("UPDATE collection_documents SET rq_job_id = :jid WHERE id = :id"),
                    {"jid": rq_job_id, "id": doc_id},
                )
                await session2.commit()
            log.info("Enqueued ingest job %s for collection_doc %s", rq_job_id, doc_id)
        except Exception as exc:
            log.error("Failed to enqueue ingest job: %s", exc)

    return JSONResponse({
        "collection_doc_id": doc_id,
        "status": upload_status,
        "dms_match": dms_match_id is not None,
        "dms_match_confidence": dms_confidence,
        "rq_job_id": rq_job_id,
    })


# ══════════════════════════════════════════════════════════════════════════════
# HTMX partials & status
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/collections/{collection_id}/documents", response_class=HTMLResponse)
async def collection_documents_partial(collection_id: str, request: Request):
    """HTMX partial — document list with status badges."""
    tenant_id = await _get_tenant_id(request)
    branding = getattr(request.state, "branding", None)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, original_filename, file_size_bytes, upload_status,
                       dms_match_id, dms_match_confidence, rejection_reason,
                       uploaded_at, processed_at
                FROM collection_documents
                WHERE collection_id = :cid AND tenant_id = :tid
                ORDER BY uploaded_at DESC
            """),
            {"cid": collection_id, "tid": tenant_id},
        )
        docs = [dict(r) for r in result.mappings().fetchall()]

    return templates.TemplateResponse(request, "partials/collection_documents.html", {
        "docs": docs,
        "collection_id": collection_id,
        "branding": getattr(request.state, "branding", None),
    })


@router.get("/collections/{collection_id}/status")
async def collection_status(collection_id: str, request: Request):
    """JSON status counts for polling."""
    tenant_id = await _get_tenant_id(request)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT upload_status, COUNT(*) AS cnt
                FROM collection_documents
                WHERE collection_id = :cid AND tenant_id = :tid
                GROUP BY upload_status
            """),
            {"cid": collection_id, "tid": tenant_id},
        )
        counts = {r["upload_status"]: r["cnt"] for r in result.mappings().fetchall()}

    total = sum(counts.values())
    still_processing = counts.get("pending", 0) + counts.get("processing", 0)

    return JSONResponse({
        "total": total,
        "counts": counts,
        "still_processing": still_processing,
    })


# ══════════════════════════════════════════════════════════════════════════════
# DMS override + discard
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/collections/{collection_id}/documents/{doc_id}/confirm-dms-override")
async def confirm_dms_override(collection_id: str, doc_id: str, request: Request):
    """Attorney confirms: proceed with ingestion despite DMS match."""
    from redis import Redis
    from rq import Queue

    tenant_id = await _get_tenant_id(request)
    user_id = await _get_user_id(request)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, upload_status, original_filename, file_hash, staging_path
                FROM collection_documents
                WHERE id = :did AND collection_id = :cid AND tenant_id = :tid
            """),
            {"did": doc_id, "cid": collection_id, "tid": tenant_id},
        )
        doc = result.mappings().fetchone()

        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        if doc["upload_status"] != "dms_duplicate":
            raise HTTPException(status_code=409,
                                detail=f"Document is '{doc['upload_status']}', not 'dms_duplicate'")

        # Use stored staging_path — already on shared mount from initial upload
        actual_staging = doc["staging_path"]

        # Fallback: if staging_path missing, try to find by hash in staging dir
        if not actual_staging or not os.path.exists(actual_staging):
            import glob
            staging_dir = os.path.join(COLLECTION_STAGING_ROOT, tenant_id.strip())
            actual_staging = None
            for m in glob.glob(os.path.join(staging_dir, "*")):
                if os.path.isfile(m):
                    h = hashlib.sha256()
                    with open(m, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            h.update(chunk)
                    if h.hexdigest() == doc["file_hash"]:
                        actual_staging = m
                        break

        await session.execute(
            text("""
                UPDATE collection_documents
                SET upload_status = 'pending',
                    override_confirmed_by = :uid,
                    override_confirmed_at = NOW()
                WHERE id = :did
            """),
            {"uid": user_id, "did": doc_id},
        )
        await session.commit()

    # Enqueue ingestion
    rq_job_id = None
    if actual_staging:
        try:
            REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
            redis_conn = Redis.from_url(REDIS_URL)
            q = Queue("ediscovery", connection=redis_conn)
            job = q.enqueue(
                "jobs.ingest_collection_document.run",
                doc_id,
                job_timeout=3600,
            )
            rq_job_id = job.id
            async with AsyncSessionLocal() as session2:
                await session2.execute(
                    text("UPDATE collection_documents SET rq_job_id = :jid WHERE id = :id"),
                    {"jid": rq_job_id, "id": doc_id},
                )
                await session2.commit()
        except Exception as exc:
            log.error("Failed to enqueue override ingestion: %s", exc)

    return JSONResponse({"status": "pending", "rq_job_id": rq_job_id})


@router.post("/collections/{collection_id}/documents/{doc_id}/discard")
async def discard_document(collection_id: str, doc_id: str, request: Request):
    """Attorney discards a DMS-flagged or rejected document."""
    tenant_id = await _get_tenant_id(request)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, upload_status, file_hash, original_filename, staging_path
                FROM collection_documents
                WHERE id = :did AND collection_id = :cid AND tenant_id = :tid
            """),
            {"did": doc_id, "cid": collection_id, "tid": tenant_id},
        )
        doc = result.mappings().fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")

        await session.execute(
            text("""
                UPDATE collection_documents
                SET upload_status = 'rejected',
                    rejection_reason = 'Discarded by attorney',
                    processed_at = NOW(),
                    staging_path = NULL
                WHERE id = :did
            """),
            {"did": doc_id},
        )
        await session.commit()

    # Clean up staging file
    staging_path = doc["staging_path"]
    if staging_path and os.path.exists(staging_path):
        try:
            os.unlink(staging_path)
        except Exception:
            pass
    else:
        # Fallback: find by hash in staging dir
        import glob
        staging_dir = os.path.join(COLLECTION_STAGING_ROOT, tenant_id.strip())
        for m in glob.glob(os.path.join(staging_dir, "*")):
            if os.path.isfile(m):
                h = hashlib.sha256()
                try:
                    with open(m, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            h.update(chunk)
                    if h.hexdigest() == doc["file_hash"]:
                        os.unlink(m)
                        break
                except Exception:
                    pass

    return JSONResponse({"status": "rejected"})
