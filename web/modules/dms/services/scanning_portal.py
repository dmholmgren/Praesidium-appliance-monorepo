"""
COMP 15 — Scanning Portal
OCR intake pipeline, scan queue management, dictation queue UI.
Route prefix: /scan

DB pattern: AsyncSessionLocal — matches working platform pattern.
RQ jobs: _SyncSessionLocal for background processing on PROC-01.
"""

import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.dms.brand_helper import get_brand

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scan", tags=["scanning-portal"])
templates = Jinja2Templates(directory=["core/templates", "modules/dms/templates"])


# ── Scan Portal Home ──────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def scan_home(request: Request):
    """Scanning portal home — scan queue + dictation queue."""
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    brand = get_brand(request)
    user = getattr(request.state, "current_user", None)

    async with AsyncSessionLocal() as session:
        # Scan queue — pending/processing items
        scan_result = await session.execute(
            text("""
                SELECT sq.*, m.matter_name
                FROM scan_queue sq
                LEFT JOIN matters m ON sq.matter_id = m.id::text
                    AND trim(sq.tenant_id) = trim(m.tenant_id)
                WHERE trim(sq.tenant_id) = :tid AND sq.status != 'completed'
                ORDER BY sq.created_at DESC LIMIT 50
            """),
            {"tid": tenant_id},
        )
        scan_queue = [dict(r._mapping) for r in scan_result.fetchall()]

        # Dictation queue — pending/processing items
        dict_result = await session.execute(
            text("""
                SELECT dq.*, m.matter_name, u.full_name as user_name
                FROM dictation_queue dq
                LEFT JOIN matters m ON dq.matter_id = m.id::text
                    AND trim(dq.tenant_id) = trim(m.tenant_id)
                LEFT JOIN users u ON dq.user_id = u.id
                    AND trim(dq.tenant_id) = trim(u.tenant_id)
                WHERE trim(dq.tenant_id) = :tid AND dq.status != 'completed'
                ORDER BY dq.created_at DESC LIMIT 50
            """),
            {"tid": tenant_id},
        )
        dictation_queue = [dict(r._mapping) for r in dict_result.fetchall()]

        # Active matters for assignment dropdown
        matters_result = await session.execute(
            text("""
                SELECT m.id, m.matter_name, m.matter_number,
                       c.client_name
                FROM matters m
                LEFT JOIN clients c ON m.client_id = c.id
                    AND trim(m.tenant_id) = trim(c.tenant_id)
                WHERE trim(m.tenant_id) = :tid AND m.status = 'active'
                ORDER BY c.client_name ASC, m.matter_name ASC
            """),
            {"tid": tenant_id},
        )
        matters = [dict(r._mapping) for r in matters_result.fetchall()]

    return templates.TemplateResponse(
        "scan_portal.html",
        {
            "request": request,
            "brand": brand,
            "user": user,
            "current_user": user,
            "page": "scan",
            "scan_queue": scan_queue,
            "dictation_queue": dictation_queue,
            "matters": matters,
        },
    )


# ── Upload Scanned Document ───────────────────────────────────

@router.post("/upload")
async def scan_upload(
    request: Request,
    matter_id: str = Form(""),
    source: str = Form("scanner"),
    doc_type: str = Form(""),
    notes: str = Form(""),
    file: UploadFile = File(...),
):
    """Upload a scanned document to the scan queue."""
    tenant_id = request.state.tenant_id.strip()
    user = getattr(request.state, "current_user", None)
    user_id = str(getattr(user, "id", "")) if user else ""

    content = await file.read()
    scan_id = str(uuid.uuid4())

    # Save to /mnt/praesidium/{tenant_id}/_scan_queue/{scan_id}/{filename}
    # via LocalMountStorageAdapter — adapter handles tenant injection
    from modules.dms.adapters.local_mount_storage import get_storage_adapter
    storage = get_storage_adapter()
    temp_path = f"praesidium/_scan_queue/{scan_id}/{file.filename}"

    try:
        await storage.upload(
            tenant_id, temp_path, content,
            content_type=file.content_type or "application/octet-stream",
        )
    except Exception as e:
        logger.error("Storage upload failed for scan %s: %s", scan_id, e)
        raise HTTPException(status_code=502, detail="File storage unavailable")

    file_ext = os.path.splitext(file.filename)[1].lower().lstrip(".")

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO scan_queue
                    (id, tenant_id, matter_id, filename, file_size, file_type,
                     source, doc_type, notes, storage_path, status,
                     created_at, created_by)
                VALUES
                    (:id, :tid, :mid, :fname, :size, :ftype,
                     :source, :dtype, :notes, :path, 'pending',
                     NOW(), :by)
            """),
            {
                "id": scan_id,
                "tid": tenant_id,
                "mid": matter_id or None,
                "fname": file.filename,
                "size": len(content),
                "ftype": file_ext,
                "source": source,
                "dtype": doc_type,
                "notes": notes,
                "path": temp_path,
                "by": user_id,
            },
        )
        await session.commit()

    # Enqueue OCR processing on PROC-01
    try:
        import redis as redis_lib
        from rq import Queue
        redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
        q = Queue("ocr", connection=redis_lib.Redis.from_url(redis_url))
        q.enqueue(process_scan_item, tenant_id, scan_id)
    except Exception as e:
        logger.warning("Failed to enqueue OCR job for scan %s: %s", scan_id, e)

    return HTMLResponse(
        content=f'<div style="color:#065F46;padding:8px;background:#D1FAE5;border-radius:4px;font-size:13px;">✓ Uploaded: {file.filename}</div>',
        headers={"HX-Trigger": "scanUploaded"},
    )


# ── Assign Scan to Matter ─────────────────────────────────────

@router.post("/{scan_id}/assign")
async def assign_scan_to_matter(
    request: Request,
    scan_id: str,
    matter_id: str = Form(...),
    folder_path: str = Form(""),
):
    """Assign a scan queue item to a matter and file it."""
    tenant_id = request.state.tenant_id.strip()
    user = getattr(request.state, "current_user", None)
    user_id = str(getattr(user, "id", "")) if user else ""

    async with AsyncSessionLocal() as session:
        # Get scan item
        scan_result = await session.execute(
            text("""
                SELECT * FROM scan_queue
                WHERE id = :id AND trim(tenant_id) = :tid
            """),
            {"id": scan_id, "tid": tenant_id},
        )
        scan_item = scan_result.mappings().fetchone()
        if not scan_item:
            raise HTTPException(status_code=404, detail="Scan item not found")

        # Get matter for folder path construction
        matter_result = await session.execute(
            text("""
                SELECT matter_number, matter_name FROM matters
                WHERE id = :id AND trim(tenant_id) = :tid
            """),
            {"id": matter_id, "tid": tenant_id},
        )
        matter = matter_result.mappings().fetchone()
        if not matter:
            raise HTTPException(status_code=404, detail="Matter not found")

    # Build destination path — ID-based, under tenant's matter directory.
    # Paths use UUIDs to survive matter renames without filesystem moves.
    if folder_path:
        folder_clean = folder_path.strip("/")
        dst_path = f"praesidium/matters/{matter_id}/{folder_clean}/{scan_item['filename']}"
    else:
        dst_path = f"praesidium/matters/{matter_id}/{scan_item['filename']}"

    # Move file via LocalMountStorageAdapter (no bridge hop)
    from modules.dms.adapters.local_mount_storage import get_storage_adapter
    storage = get_storage_adapter()
    try:
        await storage.move(tenant_id, scan_item["storage_path"], dst_path)
    except Exception as e:
        logger.error("Storage move failed for scan %s: %s", scan_id, e)
        raise HTTPException(status_code=502, detail="File move failed")

    # Update scan queue record
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE scan_queue SET
                    status = 'filed',
                    matter_id = :mid,
                    filed_path = :path,
                    filed_at = NOW(),
                    filed_by = :by
                WHERE id = :id AND trim(tenant_id) = :tid
            """),
            {
                "mid": matter_id,
                "path": dst_path,
                "by": user_id,
                "id": scan_id,
                "tid": tenant_id,
            },
        )
        await session.commit()

    return HTMLResponse(
        content='<div style="color:#065F46;padding:8px;background:#D1FAE5;border-radius:4px;font-size:13px;">✓ Filed to matter</div>',
        headers={"HX-Trigger": "scanFiled"},
    )


# ── Delete / Dismiss Scan Item ────────────────────────────────

@router.delete("/{scan_id}")
async def dismiss_scan(request: Request, scan_id: str):
    """Mark a scan item as dismissed/cancelled."""
    tenant_id = request.state.tenant_id.strip()

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE scan_queue SET status = 'cancelled'
                WHERE id = :id AND trim(tenant_id) = :tid
            """),
            {"id": scan_id, "tid": tenant_id},
        )
        await session.commit()

    return JSONResponse({"status": "dismissed"})


# ── Dictation Upload ──────────────────────────────────────────

@router.post("/dictation/upload")
async def upload_dictation(
    request: Request,
    matter_id: str = Form(""),
    notes: str = Form(""),
    file: UploadFile = File(...),
):
    """Upload a dictation audio file for Whisper transcription."""
    tenant_id = request.state.tenant_id.strip()
    user = getattr(request.state, "current_user", None)
    user_id = getattr(user, "id", None) if user else None

    content = await file.read()
    dict_id = str(uuid.uuid4())

    # Save to /mnt/praesidium/{tenant_id}/_dictation_queue/{dict_id}/{filename}
    # via LocalMountStorageAdapter — adapter handles tenant injection
    from modules.dms.adapters.local_mount_storage import get_storage_adapter
    storage = get_storage_adapter()
    temp_path = f"praesidium/_dictation_queue/{dict_id}/{file.filename}"

    try:
        await storage.upload(
            tenant_id, temp_path, content,
            content_type=file.content_type or "audio/mpeg",
        )
    except Exception as e:
        logger.error("Storage upload failed for dictation %s: %s", dict_id, e)
        raise HTTPException(status_code=502, detail="File storage unavailable")

    audio_fmt = os.path.splitext(file.filename)[1].lower().lstrip(".")

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO dictation_queue
                    (id, tenant_id, user_id, matter_id, filename, file_size,
                     audio_format, storage_path, notes, status, created_at)
                VALUES
                    (:id, :tid, :uid, :mid, :fname, :size,
                     :fmt, :path, :notes, 'pending', NOW())
            """),
            {
                "id": dict_id,
                "tid": tenant_id,
                "uid": user_id,
                "mid": matter_id or None,
                "fname": file.filename,
                "size": len(content),
                "fmt": audio_fmt,
                "path": temp_path,
                "notes": notes,
            },
        )
        await session.commit()

    # Enqueue Whisper transcription
    try:
        import redis as redis_lib
        from rq import Queue
        redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
        q = Queue("transcription", connection=redis_lib.Redis.from_url(redis_url))
        q.enqueue(transcribe_dictation, tenant_id, dict_id)
    except Exception as e:
        logger.warning("Failed to enqueue transcription job for %s: %s", dict_id, e)

    return HTMLResponse(
        content=f'<div style="color:#065F46;padding:8px;background:#D1FAE5;border-radius:4px;font-size:13px;">✓ Dictation uploaded: {file.filename}</div>',
    )


# ── Scan Queue Status (HTMX refresh) ─────────────────────────

@router.get("/queue", response_class=HTMLResponse)
async def scan_queue_partial(request: Request):
    """HTMX partial — refreshes scan queue list."""
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    brand = get_brand(request)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT sq.*, m.matter_name
                FROM scan_queue sq
                LEFT JOIN matters m ON sq.matter_id = m.id::text
                    AND trim(sq.tenant_id) = trim(m.tenant_id)
                WHERE trim(sq.tenant_id) = :tid AND sq.status != 'completed'
                ORDER BY sq.created_at DESC LIMIT 50
            """),
            {"tid": tenant_id},
        )
        scan_queue = [dict(r._mapping) for r in result.fetchall()]

        matters_result = await session.execute(
            text("""
                SELECT id, matter_name, matter_number FROM matters
                WHERE trim(tenant_id) = :tid AND status = 'active'
                ORDER BY matter_name ASC
            """),
            {"tid": tenant_id},
        )
        matters = [dict(r._mapping) for r in matters_result.fetchall()]

    return templates.TemplateResponse(
        "scan_queue_partial.html",
        {
            "request": request,
            "brand": brand,
            "scan_queue": scan_queue,
            "matters": matters,
        },
    )


# ── RQ Background Jobs (sync — run on PROC-01) ───────────────

def process_scan_item(tenant_id: str, scan_id: str):
    """RQ job: OCR a scanned document. Runs synchronously on PROC-01.
    Reads file directly via LocalMountStorageAdapter — no bridge."""
    import asyncio
    from core.db.base import _SyncSessionLocal
    from sqlalchemy import text as sync_text
    from modules.dms.adapters.local_mount_storage import get_storage_adapter

    session = _SyncSessionLocal()
    try:
        row = session.execute(
            sync_text("""
                SELECT storage_path, file_type FROM scan_queue
                WHERE id = :id AND trim(tenant_id) = :tid
            """),
            {"id": scan_id, "tid": tenant_id.strip()},
        ).fetchone()

        if not row:
            return

        storage_path, file_type = row

        # Download file via adapter (sync context — bridge async call)
        storage = get_storage_adapter()
        content = asyncio.run(storage.download(tenant_id.strip(), storage_path))

        # OCR — reuses extraction helpers from ocr_pipeline
        from modules.dms.jobs.ocr_pipeline import _process_pdf, _process_image
        if file_type == "pdf":
            ocr_text = _process_pdf(content)
        elif file_type in ("jpg", "jpeg", "png", "tiff", "tif"):
            ocr_text = _process_image(content)
        else:
            ocr_text = ""

        session.execute(
            sync_text("""
                UPDATE scan_queue SET
                    ocr_text = :text,
                    status = 'ocr_complete'
                WHERE id = :id AND trim(tenant_id) = :tid
            """),
            {"text": ocr_text[:500000], "id": scan_id, "tid": tenant_id.strip()},
        )
        session.commit()

    except Exception as e:
        logger.error("OCR job failed for scan %s: %s", scan_id, e)
        try:
            session.execute(
                sync_text("""
                    UPDATE scan_queue SET status = 'ocr_failed'
                    WHERE id = :id AND trim(tenant_id) = :tid
                """),
                {"id": scan_id, "tid": tenant_id.strip()},
            )
            session.commit()
        except Exception:
            pass
    finally:
        session.close()


def transcribe_dictation(tenant_id: str, dictation_id: str):
    """RQ job: Transcribe dictation via Whisper on WSS-01. Sync on PROC-01.
    Reads audio file via adapter, sends to Whisper — still HTTP to WSS-01."""
    import asyncio
    import httpx
    from core.db.base import _SyncSessionLocal
    from sqlalchemy import text as sync_text
    from modules.dms.adapters.local_mount_storage import get_storage_adapter

    session = _SyncSessionLocal()
    try:
        row = session.execute(
            sync_text("""
                SELECT storage_path, audio_format FROM dictation_queue
                WHERE id = :id AND trim(tenant_id) = :tid
            """),
            {"id": dictation_id, "tid": tenant_id.strip()},
        ).fetchone()

        if not row:
            return

        storage_path, audio_format = row

        # Download audio via adapter
        storage = get_storage_adapter()
        audio_content = asyncio.run(storage.download(tenant_id.strip(), storage_path))

        # Send to Whisper on WSS-01 (still HTTP — Whisper is a real external service)
        whisper_url = os.environ.get("WHISPER_URL", "http://10.10.60.14:8000")
        try:
            whisper_resp = httpx.post(
                f"{whisper_url}/v1/audio/transcriptions",
                files={"file": (f"audio.{audio_format}", audio_content)},
                data={"model": "whisper-1", "language": "en"},
                timeout=300,
            )
            whisper_resp.raise_for_status()
            transcript = whisper_resp.json().get("text", "")
            status = "completed"
        except Exception as e:
            logger.error("Whisper transcription failed for %s: %s", dictation_id, e)
            transcript = ""
            status = "failed"

        session.execute(
            sync_text("""
                UPDATE dictation_queue SET
                    transcript = :text,
                    status = :status,
                    completed_at = NOW()
                WHERE id = :id AND trim(tenant_id) = :tid
            """),
            {
                "text": transcript,
                "status": status,
                "id": dictation_id,
                "tid": tenant_id.strip(),
            },
        )
        session.commit()

    except Exception as e:
        logger.error("Transcription job failed for %s: %s", dictation_id, e)
    finally:
        session.close()
