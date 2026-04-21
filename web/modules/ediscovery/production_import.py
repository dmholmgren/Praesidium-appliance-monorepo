"""
modules/ediscovery/production_import.py
Component 8 — Production Import
Patent Pending — 64/020,027

Endpoints:
  GET  /ediscovery/matters/{matter_id}/productions          → production_list.html
  POST /ediscovery/matters/{matter_id}/productions          → create production record + upload load file
  GET  /ediscovery/productions/{production_id}              → production_detail.html
  POST /ediscovery/productions/{production_id}/start        → enqueue import_production RQ job
  GET  /ediscovery/productions/{production_id}/status       → JSON status for HTMX polling
  GET  /ediscovery/productions/{production_id}/field-mapping → field_mapping.html
  POST /ediscovery/productions/{production_id}/field-mapping → save confirmed field map
  POST /ediscovery/productions/{production_id}/suggest-mapping → enqueue AI mapping job
  GET  /ediscovery/matters/{matter_id}/evidence-workspace   → evidence_workspace.html
  POST /ediscovery/matters/{matter_id}/evidence-workspace   → add item to workspace
  DELETE /ediscovery/evidence-workspace/{item_id}           → remove item
  POST /ediscovery/evidence-workspace/{item_id}/reorder     → update sort_order

Route ordering: static segments registered before path-param segments.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import uuid
from typing import Optional

import aiofiles
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

log = logging.getLogger("praesidium.ediscovery.production_import")

router = APIRouter(prefix="/ediscovery", tags=["ediscovery-productions"])

templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates/ediscovery", "templates/ediscovery"])

# ── Config ────────────────────────────────────────────────────────────────────
UPLOAD_TMP_DIR = os.environ.get("UPLOAD_TMP_DIR", "/tmp/praesidium_uploads")
REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
MAX_LOAD_FILE_BYTES = 200 * 1024 * 1024  # 200 MB

LOAD_FILE_EXTENSIONS = {".dat", ".csv", ".opt", ".lfp", ".dii", ".txt"}

os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)


# ── Pydantic models ───────────────────────────────────────────────────────────
class CreateProductionRequest(BaseModel):
    production_name: str
    producing_party: Optional[str] = None
    load_file_format: str = "dat"


class ConfirmFieldMapRequest(BaseModel):
    field_map: dict


class AddEvidenceItemRequest(BaseModel):
    document_id: str
    workspace_section: Optional[str] = None
    display_label: Optional[str] = None
    notes: Optional[str] = None
    sort_order: int = 0


class ReorderItemRequest(BaseModel):
    sort_order: int


# ── Helpers ───────────────────────────────────────────────────────────────────
async def _get_tenant_id(request: Request) -> str:
    user = await get_current_user(request)
    return user["tenant_id"].strip()


async def _get_user_id(request: Request) -> Optional[int]:
    try:
        user = await get_current_user(request)
        return user.get("id")
    except Exception:
        return None


def _detect_format(filename: str) -> str:
    """Infer load file format from extension."""
    ext = os.path.splitext(filename.lower())[1]
    mapping = {
        ".dat": "dat",
        ".csv": "csv",
        ".opt": "opt",
        ".lfp": "lfp",
        ".dii": "dii",
    }
    return mapping.get(ext, "csv")


def _sniff_columns(raw_bytes: bytes, fmt: str) -> list[str]:
    """
    Sniff column headers from the first line of a load file.
    DAT files use Concordance delimiters (þ field sep, ÿ quote char).
    All others treated as CSV.
    """
    try:
        text_data = raw_bytes.decode("utf-8", errors="replace")
        first_line = text_data.split("\n")[0]
        if fmt == "dat":
            # Concordance DAT: þ (0xFE) delimiter, ÿ (0xFF) quote
            cols = first_line.replace("\xfe", "\x01").replace("\xff", "").split("\x01")
            return [c.strip() for c in cols if c.strip()]
        else:
            reader = csv.reader(io.StringIO(first_line))
            for row in reader:
                return [c.strip() for c in row if c.strip()]
    except Exception as exc:
        log.warning("Column sniff failed: %s", exc)
    return []


# ══════════════════════════════════════════════════════════════════════════════
# Productions — list & create
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/matters/{matter_id}/productions", response_class=HTMLResponse)
async def list_productions(matter_id: str, request: Request):
    """List all inbound productions for a matter."""
    tenant_id = await _get_tenant_id(request)
    branding = getattr(request.state, "branding", None)

    async with AsyncSessionLocal() as session:
        matter_r = await session.execute(
            text("SELECT id, matter_name AS name FROM matters WHERE id = :mid AND tenant_id = :tid"),
            {"mid": matter_id, "tid": tenant_id},
        )
        matter = matter_r.mappings().fetchone()
        if not matter:
            raise HTTPException(status_code=404, detail="Matter not found")

        prod_r = await session.execute(
            text("""
                SELECT p.id, p.production_name, p.producing_party,
                       p.load_file_format, p.status,
                       p.row_count_total, p.row_count_imported,
                       p.row_count_failed, p.row_count_skipped,
                       p.created_at, p.completed_at,
                       u.full_name AS imported_by_name
                FROM productions p
                LEFT JOIN users u ON u.id = p.imported_by
                WHERE p.tenant_id = :tid AND p.matter_id = :mid
                ORDER BY p.created_at DESC
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        productions = [dict(r) for r in prod_r.mappings().fetchall()]

    return templates.TemplateResponse("production_list.html", {
        "request": request,
        "matter": dict(matter),
        "productions": productions,
        "branding": branding,
    })


@router.post("/matters/{matter_id}/productions")
async def create_production(
    matter_id: str,
    request: Request,
    production_name: str,
    producing_party: Optional[str] = None,
    load_file_format: str = "dat",
    file: Optional[UploadFile] = File(None),
):
    """
    Create a production record and optionally upload the load file.
    If a file is provided, sniff its column headers for field mapping.
    Returns JSON with production_id and detected columns.
    """
    tenant_id = await _get_tenant_id(request)
    user_id = await _get_user_id(request)
    production_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text("SELECT id FROM matters WHERE id = :mid AND tenant_id = :tid"),
            {"mid": matter_id, "tid": tenant_id},
        )
        if not r.fetchone():
            raise HTTPException(status_code=404, detail="Matter not found")

    # Handle load file upload
    load_file_path = None
    load_file_name = None
    load_file_size = None
    detected_columns: list[str] = []
    detected_format = load_file_format

    if file and file.filename:
        ext = os.path.splitext(file.filename.lower())[1]
        if ext not in LOAD_FILE_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported load file type: {ext}. Expected: {', '.join(LOAD_FILE_EXTENSIONS)}",
            )
        raw = await file.read()
        if len(raw) > MAX_LOAD_FILE_BYTES:
            raise HTTPException(status_code=413, detail="Load file exceeds 200 MB limit")

        detected_format = _detect_format(file.filename)
        detected_columns = _sniff_columns(raw[:4096], detected_format)

        load_file_name = file.filename
        load_file_size = len(raw)
        safe_name = f"{production_id}_{file.filename}"
        load_file_path = os.path.join(UPLOAD_TMP_DIR, safe_name)
        async with aiofiles.open(load_file_path, "wb") as f:
            await f.write(raw)

    # Persist production record
    async with session_factory() as session:
        await session.execute(
            text("""
                INSERT INTO productions (
                    id, tenant_id, matter_id, production_name, producing_party,
                    load_file_format, status, load_file_path, load_file_name,
                    load_file_size_bytes, imported_by
                ) VALUES (
                    :id, :tid, :mid, :name, :party,
                    :fmt, 'pending', :fpath, :fname,
                    :fsize, :uid
                )
            """),
            {
                "id": production_id,
                "tid": tenant_id,
                "mid": matter_id,
                "name": production_name,
                "party": producing_party,
                "fmt": detected_format,
                "fpath": load_file_path,
                "fname": load_file_name,
                "fsize": load_file_size,
                "uid": user_id,
            },
        )
        await session.commit()

    return JSONResponse({
        "production_id": production_id,
        "status": "pending",
        "load_file_format": detected_format,
        "detected_columns": detected_columns,
    })


# ══════════════════════════════════════════════════════════════════════════════
# Production detail & status
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/productions/{production_id}", response_class=HTMLResponse)
async def production_detail(production_id: str, request: Request):
    """Production detail page — progress, row status table."""
    tenant_id = await _get_tenant_id(request)
    branding = getattr(request.state, "branding", None)

    async with AsyncSessionLocal() as session:
        prod_r = await session.execute(
            text("""
                SELECT p.*, m.matter_name AS matter_name
                FROM productions p
                JOIN matters m ON m.id = p.matter_id
                WHERE p.id = :pid AND p.tenant_id = :tid
            """),
            {"pid": production_id, "tid": tenant_id},
        )
        production = prod_r.mappings().fetchone()
        if not production:
            raise HTTPException(status_code=404, detail="Production not found")

        rows_r = await session.execute(
            text("""
                SELECT id, row_number, bates_begin, bates_end,
                       status, error_message, processed_at
                FROM production_rows
                WHERE production_id = :pid AND tenant_id = :tid
                ORDER BY row_number ASC
                LIMIT 500
            """),
            {"pid": production_id, "tid": tenant_id},
        )
        rows = [dict(r) for r in rows_r.mappings().fetchall()]

    return templates.TemplateResponse("production_detail.html", {
        "request": request,
        "production": dict(production),
        "rows": rows,
        "branding": branding,
    })


@router.get("/productions/{production_id}/status")
async def production_status(production_id: str, request: Request):
    """JSON status for HTMX polling during import."""
    tenant_id = await _get_tenant_id(request)

    async with AsyncSessionLocal() as session:
        prod_r = await session.execute(
            text("""
                SELECT status, row_count_total, row_count_imported,
                       row_count_failed, row_count_skipped, rq_job_id
                FROM productions
                WHERE id = :pid AND tenant_id = :tid
            """),
            {"pid": production_id, "tid": tenant_id},
        )
        prod = prod_r.mappings().fetchone()
        if not prod:
            raise HTTPException(status_code=404, detail="Production not found")

    total = prod["row_count_total"] or 0
    imported = prod["row_count_imported"] or 0
    pct = round((imported / total * 100) if total > 0 else 0, 1)

    return JSONResponse({
        "status": prod["status"],
        "row_count_total": total,
        "row_count_imported": imported,
        "row_count_failed": prod["row_count_failed"] or 0,
        "row_count_skipped": prod["row_count_skipped"] or 0,
        "percent_complete": pct,
        "rq_job_id": prod["rq_job_id"],
        "done": prod["status"] in ("complete", "partial", "failed"),
    })


@router.post("/productions/{production_id}/start")
async def start_import(production_id: str, request: Request):
    """Enqueue the import_production RQ job."""
    from redis import Redis
    from rq import Queue

    tenant_id = await _get_tenant_id(request)

    async with AsyncSessionLocal() as session:
        prod_r = await session.execute(
            text("""
                SELECT id, status, load_file_path, load_file_format, field_map
                FROM productions
                WHERE id = :pid AND tenant_id = :tid
            """),
            {"pid": production_id, "tid": tenant_id},
        )
        prod = prod_r.mappings().fetchone()
        if not prod:
            raise HTTPException(status_code=404, detail="Production not found")
        if prod["status"] not in ("pending",):
            raise HTTPException(
                status_code=409,
                detail=f"Production is '{prod['status']}' — can only start from 'pending'",
            )
        if not prod["load_file_path"]:
            raise HTTPException(status_code=400, detail="No load file uploaded")
        if not prod["field_map"]:
            raise HTTPException(
                status_code=400,
                detail="Field mapping required before import. Use /field-mapping to confirm.",
            )

        await session.execute(
            text("UPDATE productions SET status = 'running', started_at = NOW() WHERE id = :pid"),
            {"pid": production_id},
        )
        await session.commit()

    redis_conn = Redis.from_url(REDIS_URL)
    q = Queue("ediscovery_proc", connection=redis_conn)
    job = q.enqueue(
        "jobs.import_production.run",
        production_id,
        tenant_id,
        job_timeout=7200,
    )

    async with session_factory() as session:
        await session.execute(
            text("UPDATE productions SET rq_job_id = :jid WHERE id = :pid"),
            {"jid": job.id, "pid": production_id},
        )
        await session.commit()

    log.info("Enqueued import job %s for production %s", job.id, production_id)
    return JSONResponse({"status": "running", "rq_job_id": job.id})


# ══════════════════════════════════════════════════════════════════════════════
# Field mapping
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/productions/{production_id}/field-mapping", response_class=HTMLResponse)
async def field_mapping_page(production_id: str, request: Request):
    """Field mapping review and confirmation UI."""
    tenant_id = await _get_tenant_id(request)
    branding = getattr(request.state, "branding", None)

    async with AsyncSessionLocal() as session:
        prod_r = await session.execute(
            text("""
                SELECT p.id, p.production_name, p.load_file_format,
                       p.field_map, p.status, p.matter_id,
                       m.matter_name AS matter_name
                FROM productions p
                JOIN matters m ON m.id = p.matter_id
                WHERE p.id = :pid AND p.tenant_id = :tid
            """),
            {"pid": production_id, "tid": tenant_id},
        )
        production = prod_r.mappings().fetchone()
        if not production:
            raise HTTPException(status_code=404, detail="Production not found")

    # Standard Praesidium target fields for mapping
    target_fields = [
        {"key": "bates_begin", "label": "Bates Begin", "required": True},
        {"key": "bates_end", "label": "Bates End", "required": False},
        {"key": "doc_date", "label": "Document Date", "required": False},
        {"key": "author", "label": "Author / From", "required": False},
        {"key": "recipients", "label": "Recipients / To", "required": False},
        {"key": "subject", "label": "Subject", "required": False},
        {"key": "custodian", "label": "Custodian", "required": False},
        {"key": "doc_type", "label": "Document Type", "required": False},
        {"key": "file_path", "label": "Native File Path", "required": False},
        {"key": "text_path", "label": "Extracted Text Path", "required": False},
        {"key": "confidentiality", "label": "Confidentiality / Privilege", "required": False},
        {"key": "md5_hash", "label": "MD5 Hash", "required": False},
    ]

    prod_dict = dict(production)
    existing_map = prod_dict.get("field_map") or {}

    return templates.TemplateResponse("field_mapping.html", {
        "request": request,
        "production": prod_dict,
        "target_fields": target_fields,
        "existing_map": existing_map,
        "branding": branding,
    })


@router.post("/productions/{production_id}/field-mapping")
async def save_field_mapping(
    production_id: str,
    request: Request,
    body: ConfirmFieldMapRequest,
):
    """Save confirmed field mapping. Validates bates_begin is mapped."""
    tenant_id = await _get_tenant_id(request)

    if "bates_begin" not in body.field_map or not body.field_map["bates_begin"]:
        raise HTTPException(
            status_code=400,
            detail="bates_begin is required in field map",
        )

    import json
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text("SELECT id, status FROM productions WHERE id = :pid AND tenant_id = :tid"),
            {"pid": production_id, "tid": tenant_id},
        )
        prod = r.mappings().fetchone()
        if not prod:
            raise HTTPException(status_code=404, detail="Production not found")
        if prod["status"] not in ("pending",):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot edit field map when status is '{prod['status']}'",
            )

        await session.execute(
            text("UPDATE productions SET field_map = :fm WHERE id = :pid"),
            {"fm": json.dumps(body.field_map), "pid": production_id},
        )
        await session.commit()

    return JSONResponse({"status": "saved", "field_map": body.field_map})


@router.post("/productions/{production_id}/suggest-mapping")
async def suggest_field_mapping(production_id: str, request: Request):
    """Enqueue AI field mapping job. Returns immediately — result polled via status."""
    from redis import Redis
    from rq import Queue

    tenant_id = await _get_tenant_id(request)

    async with AsyncSessionLocal() as session:
        prod_r = await session.execute(
            text("""
                SELECT id, load_file_path, load_file_format, field_map
                FROM productions WHERE id = :pid AND tenant_id = :tid
            """),
            {"pid": production_id, "tid": tenant_id},
        )
        prod = prod_r.mappings().fetchone()
        if not prod:
            raise HTTPException(status_code=404, detail="Production not found")
        if not prod["load_file_path"]:
            raise HTTPException(status_code=400, detail="No load file uploaded")

    redis_conn = Redis.from_url(REDIS_URL)
    q = Queue("ediscovery_proc", connection=redis_conn)
    job = q.enqueue(
        "jobs.suggest_field_mapping.run",
        production_id,
        tenant_id,
        job_timeout=300,
    )

    log.info("Enqueued field mapping job %s for production %s", job.id, production_id)
    return JSONResponse({"status": "queued", "rq_job_id": job.id})


# ══════════════════════════════════════════════════════════════════════════════
# Evidence Assembly Workspace
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/matters/{matter_id}/evidence-workspace", response_class=HTMLResponse)
async def evidence_workspace(matter_id: str, request: Request):
    """Evidence Assembly Workspace — attorney curated document set."""
    tenant_id = await _get_tenant_id(request)
    branding = getattr(request.state, "branding", None)

    async with AsyncSessionLocal() as session:
        matter_r = await session.execute(
            text("SELECT id, matter_name AS name FROM matters WHERE id = :mid AND tenant_id = :tid"),
            {"mid": matter_id, "tid": tenant_id},
        )
        matter = matter_r.mappings().fetchone()
        if not matter:
            raise HTTPException(status_code=404, detail="Matter not found")

        items_r = await session.execute(
            text("""
                SELECT ea.id, ea.document_id, ea.workspace_section,
                       ea.display_label, ea.notes, ea.sort_order, ea.created_at,
                       d.original_filename, d.bates_begin, d.bates_end,
                       d.doc_type, d.source
                FROM evidence_assembly_items ea
                LEFT JOIN ediscovery_documents d ON d.id = ea.document_id::uuid
                WHERE ea.tenant_id = :tid AND ea.matter_id = :mid
                ORDER BY ea.workspace_section NULLS LAST, ea.sort_order ASC
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        items = [dict(r) for r in items_r.mappings().fetchall()]

        # Group by workspace_section
        sections: dict[str, list] = {}
        for item in items:
            sec = item["workspace_section"] or "General"
            sections.setdefault(sec, []).append(item)

    return templates.TemplateResponse("evidence_workspace.html", {
        "request": request,
        "matter": dict(matter),
        "sections": sections,
        "item_count": len(items),
        "branding": branding,
    })


@router.post("/matters/{matter_id}/evidence-workspace")
async def add_evidence_item(
    matter_id: str,
    request: Request,
    body: AddEvidenceItemRequest,
):
    """Add a document to the evidence workspace."""
    tenant_id = await _get_tenant_id(request)
    user_id = await _get_user_id(request)
    item_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as session:
        # Verify matter
        r = await session.execute(
            text("SELECT id FROM matters WHERE id = :mid AND tenant_id = :tid"),
            {"mid": matter_id, "tid": tenant_id},
        )
        if not r.fetchone():
            raise HTTPException(status_code=404, detail="Matter not found")

        # Verify document exists in tenant corpus
        doc_r = await session.execute(
            text("SELECT id FROM ediscovery_documents WHERE id = :did AND tenant_id = :tid"),
            {"did": body.document_id, "tid": tenant_id},
        )
        if not doc_r.fetchone():
            raise HTTPException(status_code=404, detail="Document not found in corpus")

        await session.execute(
            text("""
                INSERT INTO evidence_assembly_items (
                    id, tenant_id, matter_id, document_id,
                    workspace_section, display_label, notes, sort_order, added_by
                ) VALUES (
                    :id, :tid, :mid, :did,
                    :section, :label, :notes, :sort, :uid
                )
            """),
            {
                "id": item_id,
                "tid": tenant_id,
                "mid": matter_id,
                "did": body.document_id,
                "section": body.workspace_section,
                "label": body.display_label,
                "notes": body.notes,
                "sort": body.sort_order,
                "uid": user_id,
            },
        )
        await session.commit()

    return JSONResponse({"id": item_id, "status": "added"})


@router.delete("/evidence-workspace/{item_id}")
async def remove_evidence_item(item_id: str, request: Request):
    """Remove a document from the evidence workspace."""
    tenant_id = await _get_tenant_id(request)

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text("SELECT id FROM evidence_assembly_items WHERE id = :iid AND tenant_id = :tid"),
            {"iid": item_id, "tid": tenant_id},
        )
        if not r.fetchone():
            raise HTTPException(status_code=404, detail="Item not found")

        await session.execute(
            text("DELETE FROM evidence_assembly_items WHERE id = :iid AND tenant_id = :tid"),
            {"iid": item_id, "tid": tenant_id},
        )
        await session.commit()

    return JSONResponse({"status": "removed"})


@router.post("/evidence-workspace/{item_id}/reorder")
async def reorder_evidence_item(
    item_id: str,
    request: Request,
    body: ReorderItemRequest,
):
    """Update sort_order for drag-and-drop reordering."""
    tenant_id = await _get_tenant_id(request)

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text("SELECT id FROM evidence_assembly_items WHERE id = :iid AND tenant_id = :tid"),
            {"iid": item_id, "tid": tenant_id},
        )
        if not r.fetchone():
            raise HTTPException(status_code=404, detail="Item not found")

        await session.execute(
            text("""
                UPDATE evidence_assembly_items
                SET sort_order = :sort, updated_at = NOW()
                WHERE id = :iid AND tenant_id = :tid
            """),
            {"sort": body.sort_order, "iid": item_id, "tid": tenant_id},
        )
        await session.commit()

    return JSONResponse({"status": "updated", "sort_order": body.sort_order})
