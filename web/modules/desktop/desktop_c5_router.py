"""
M-DESK C5 — Upload, Projects & Platform Checkout routes.

Seven endpoints, all behind require_desktop_user:

  POST /api/v1/desktop/upload
    Upload a document to a matter's DMS folder without prior checkout.
    Multipart form: file + matter_id + folder_path (optional) + title (optional).
    Creates a documents row and copies file to disk.

  GET  /api/v1/desktop/projects
    List projects for a given matter (?matter_id=UUID).
    Returns project id, title, template_type, status, exhibit count.

  GET  /api/v1/desktop/projects/{pid}/exhibits
    List exhibits in a project ordered by sort_order.
    Returns filename, exhibit_label, bates_start/end, storage_path.

  PUT  /api/v1/desktop/projects/{pid}/exhibits/reorder
    Reorder exhibits. Body: {"items": [{"id": "...", "sort_order": 0}, ...]}.

  POST /api/v1/desktop/projects/{pid}/exhibits/add
    Add a document as an exhibit. Body: {"document_id": "..."} or
    multipart upload of a file. Appends to end of exhibit list.

  GET  /api/v1/desktop/pending-checkouts
    Poll for documents checked out by the current user from the web platform
    that haven't been opened in the desktop client yet. Returns doc metadata
    + file download URL. The VSTO client calls this on a timer (every 10s).

  POST /api/v1/desktop/checkout/{doc_id}/mark-opened
    Mark a checked-out document as opened in the desktop client.

Patent Pending — Series 1/2/3/4 — D.M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException,
    Path as PathParam, Query, Request, UploadFile, status,
)
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.desktop import jwt_service
from modules.desktop.checkout_router import require_desktop_user

from modules.ediscovery.services.exhibit_sticker_engine import emboss_exhibit_sticker as _engine_emboss, STICKER_COLORS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/desktop", tags=["m-desk-c5"])


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _storage_root() -> Path:
    return Path(os.environ.get("PRAESIDIUM_STORAGE_ROOT", "/mnt/praesidium"))


def _jsonable(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    import uuid as _uuid
    from decimal import Decimal
    if isinstance(obj, _uuid.UUID):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    return obj


def _row_dict(row) -> dict:
    return {k: _jsonable(v) for k, v in dict(row).items()}


# ═══════════════════════════════════════════════════════════════════════
# POST /upload — Save document to Praesidium (no checkout required)
# ═══════════════════════════════════════════════════════════════════════

@router.post("/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    matter_id: str = Form(...),
    folder_path: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Upload a document directly to a matter's DMS.

    No checkout lock needed. For filing ad-hoc documents from Word
    into Praesidium — the 'Save to Praesidium' workflow.
    """
    tid = claims.tenant_id

    # Verify matter exists and belongs to tenant
    async with AsyncSessionLocal() as session:
        matter_row = await session.execute(
            sa_text("""
                SELECT id, matter_name, matter_number
                FROM matters
                WHERE id = CAST(:mid AS uuid)
                  AND TRIM(tenant_id) = :tid
                LIMIT 1
            """),
            {"mid": matter_id, "tid": tid},
        )
        matter = matter_row.mappings().first()
        if not matter:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "matter_not_found", "matter_id": matter_id},
            )

    # Read upload bytes
    upload_bytes = await file.read()
    if not upload_bytes:
        raise HTTPException(status_code=400, detail={"error": "empty_upload"})

    checksum = hashlib.sha256(upload_bytes).hexdigest()
    filename = file.filename or "document.docx"
    mime_type = file.content_type or "application/octet-stream"

    # Determine storage directory
    # Default: /mnt/praesidium/{tenant}/matters/{matter_id}/01-Drafts/
    # If folder_path provided: /mnt/praesidium/{tenant}/matters/{matter_id}/{folder_path}/
    base_dir = _storage_root() / tid.strip() / "matters" / matter_id
    if folder_path and folder_path.strip():
        dest_dir = base_dir / folder_path.strip("/")
    else:
        dest_dir = base_dir / "01-Drafts"

    dest_dir.mkdir(parents=True, exist_ok=True)

    # Avoid overwriting — append checksum prefix if file exists
    dest_path = dest_dir / filename
    if dest_path.exists():
        stem = dest_path.stem
        suffix = dest_path.suffix
        dest_path = dest_dir / f"{stem}_{checksum[:8]}{suffix}"

    dest_path.write_bytes(upload_bytes)

    storage_path = str(dest_path)
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa_text("""
                INSERT INTO documents (
                    id, tenant_id, matter_id, filename, original_filename,
                    mime_type, file_size, storage_path,
                    title, version_number, checksum, created_by,
                    status, metadata, created_at, updated_at
                )
                VALUES (
                    gen_random_uuid(), :tid, CAST(:mid AS uuid),
                    :filename, :filename,
                    :mime, :fsize, :spath,
                    :title, 1, :checksum, :uid,
                    'active', CAST(:meta AS jsonb), :now, :now
                )
                RETURNING id::text AS id
            """),
            {
                "tid": tid, "mid": matter_id,
                "filename": filename, "mime": mime_type,
                "fsize": len(upload_bytes), "spath": storage_path,
                "title": title or filename,
                "checksum": checksum, "uid": claims.user_id,
                "meta": '{"source":"m-desk-upload"}',
                "now": now,
            },
        )
        new_row = result.mappings().first()
        new_doc_id = new_row["id"]
        await session.commit()

    logger.info(
        "[m-desk] upload ok doc=%s matter=%s tenant=%s user=%s bytes=%d",
        new_doc_id, matter_id, tid, claims.user_id, len(upload_bytes),
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "doc_id": new_doc_id,
            "matter_id": matter_id,
            "filename": filename,
            "storage_path": storage_path,
            "file_size": len(upload_bytes),
            "checksum": checksum,
            "uploaded_at": now.isoformat(),
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# GET /projects — List projects for a matter
# ═══════════════════════════════════════════════════════════════════════

@router.get("/projects")
async def list_projects(
    matter_id: str = Query(..., description="Matter UUID"),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """List projects for a matter with exhibit counts."""
    tid = claims.tenant_id

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa_text("""
                SELECT
                    p.id::text          AS id,
                    p.title,
                    p.description,
                    p.template_type,
                    p.status,
                    p.priority,
                    p.due_date,
                    p.created_at,
                    p.updated_at,
                    COALESCE(ec.exhibit_count, 0) AS exhibit_count
                FROM projects p
                LEFT JOIN LATERAL (
                    SELECT COUNT(*)::int AS exhibit_count
                    FROM project_documents pd
                    WHERE pd.project_id = p.id
                      AND TRIM(pd.tenant_id) = :tid
                      AND pd.role = 'exhibit'
                ) ec ON true
                WHERE p.matter_id = CAST(:mid AS uuid)
                  AND TRIM(p.tenant_id) = :tid
                  AND p.status != 'deleted'
                ORDER BY p.sort_order, p.created_at DESC
            """),
            {"mid": matter_id, "tid": tid},
        )
        rows = [_row_dict(r) for r in result.mappings().all()]

    return {"matter_id": matter_id, "projects": rows, "total": len(rows)}


# ═══════════════════════════════════════════════════════════════════════
# POST /projects — Create a new project
# ═══════════════════════════════════════════════════════════════════════

@router.post("/projects")
async def create_project(
    request: Request,
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Create a new project for a matter.

    Body: {
        "matter_id": "uuid",
        "title": "Deposition of John Doe",
        "template_type": "document_assembly",  // or "deposition", "hearing", etc.
        "description": "optional"
    }
    """
    tid = claims.tenant_id
    body = await request.json()
    matter_id = body.get("matter_id")
    title = body.get("title", "").strip()
    template_type = body.get("template_type", "document_assembly")
    description = body.get("description", "")

    if not matter_id:
        raise HTTPException(400, {"error": "matter_id is required"})
    if not title:
        raise HTTPException(400, {"error": "title is required"})

    async with AsyncSessionLocal() as session:
        # Verify matter
        m = await session.execute(
            sa_text("""SELECT id FROM matters
                       WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid LIMIT 1"""),
            {"mid": matter_id, "tid": tid},
        )
        if not m.fetchone():
            raise HTTPException(404, {"error": "matter_not_found"})

        # Get next sort_order
        so = (await session.execute(
            sa_text("""SELECT COALESCE(MAX(sort_order), -1) + 1 AS n
                       FROM projects
                       WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"""),
            {"mid": matter_id, "tid": tid},
        )).fetchone()

        result = (await session.execute(
            sa_text("""
                INSERT INTO projects
                    (id, tenant_id, matter_id, title, description, template_type,
                     status, priority, sort_order, created_by, created_at, updated_at)
                VALUES
                    (gen_random_uuid(), :tid, CAST(:mid AS uuid), :title, :desc, :ttype,
                     'active', 'normal', :sort, :uid, NOW(), NOW())
                RETURNING id::text, title, template_type, status, sort_order, created_at
            """),
            {
                "tid": tid, "mid": matter_id, "title": title,
                "desc": description, "ttype": template_type,
                "sort": so.n if so else 0, "uid": claims.user_id,
            },
        )).mappings().first()
        await session.commit()

    return JSONResponse(status_code=201, content=_row_dict(result))


# ═══════════════════════════════════════════════════════════════════════
# GET /projects/{pid}/exhibits — Exhibit list for a project
# ═══════════════════════════════════════════════════════════════════════

@router.get("/projects/{project_id}/exhibits")
async def list_project_exhibits(
    project_id: str = PathParam(...),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """List exhibits in a project, ordered for the task pane."""
    tid = claims.tenant_id

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa_text("""
                SELECT
                    pd.id::text,
                    pd.document_id::text,
                    pd.storage_path,
                    pd.filename,
                    pd.mime_type,
                    pd.file_size,
                    pd.role,
                    pd.sort_order,
                    pd.exhibit_label,
                    pd.exhibit_number,
                    pd.bates_start,
                    pd.bates_end,
                    pd.bates_embossed,
                    pd.page_count,
                    pd.notes,
                    pd.created_at,
                    pd.updated_at
                FROM project_documents pd
                WHERE pd.project_id = CAST(:pid AS uuid)
                  AND TRIM(pd.tenant_id) = :tid
                ORDER BY pd.role, pd.sort_order, pd.created_at
            """),
            {"pid": project_id, "tid": tid},
        )
        rows = [_row_dict(r) for r in result.mappings().all()]

    exhibits = [r for r in rows if r.get("role") == "exhibit"]
    other = [r for r in rows if r.get("role") != "exhibit"]

    return {
        "project_id": project_id,
        "exhibits": exhibits,
        "other_documents": other,
        "total": len(rows),
    }


# ═══════════════════════════════════════════════════════════════════════
# PUT /projects/{pid}/exhibits/reorder — Reorder exhibits
# ═══════════════════════════════════════════════════════════════════════

@router.put("/projects/{project_id}/exhibits/reorder")
async def reorder_exhibits(
    request: Request,
    project_id: str = PathParam(...),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Reorder exhibits and optionally relabel them.

    Body: {
        "items": [{"id": "uuid", "sort_order": 0}, ...],
        "auto_relabel": true  // optional — regenerate Exhibit A, B, C...
    }
    """
    tid = claims.tenant_id
    body = await request.json()
    items = body.get("items", [])
    auto_relabel = body.get("auto_relabel", False)

    async with AsyncSessionLocal() as session:
        for item in items:
            await session.execute(
                sa_text("""
                    UPDATE project_documents
                    SET sort_order = :sort, updated_at = NOW()
                    WHERE id = CAST(:did AS uuid)
                      AND project_id = CAST(:pid AS uuid)
                      AND TRIM(tenant_id) = :tid
                """),
                {"sort": item["sort_order"], "did": item["id"],
                 "pid": project_id, "tid": tid},
            )

        if auto_relabel:
            # Re-read in new order, assign labels
            rows = (await session.execute(
                sa_text("""
                    SELECT id::text FROM project_documents
                    WHERE project_id = CAST(:pid AS uuid)
                      AND TRIM(tenant_id) = :tid
                      AND role = 'exhibit'
                    ORDER BY sort_order, created_at
                """),
                {"pid": project_id, "tid": tid},
            )).fetchall()

            for i, r in enumerate(rows):
                label = _alpha_label(i)
                await session.execute(
                    sa_text("""
                        UPDATE project_documents
                        SET exhibit_label = :label, exhibit_number = :num,
                            updated_at = NOW()
                        WHERE id = CAST(:did AS uuid)
                    """),
                    {"label": f"Exhibit {label}", "num": i + 1, "did": r.id},
                )

        await session.commit()

    return {"reordered": len(items), "auto_relabel": auto_relabel}


def _alpha_label(index: int) -> str:
    result = ""
    n = index
    while True:
        result = chr(65 + n % 26) + result
        n = n // 26 - 1
        if n < 0:
            break
    return result


# ═══════════════════════════════════════════════════════════════════════
# POST /projects/{pid}/exhibits/add — Add document as exhibit
# ═══════════════════════════════════════════════════════════════════════

@router.post("/projects/{project_id}/exhibits/add")
async def add_exhibit(
    request: Request,
    project_id: str = PathParam(...),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Add a document as an exhibit to a project.

    Body JSON: {"document_id": "uuid"} or {"document_id": "uuid", "filename": "..."}
    References an existing document or dms_document.
    """
    tid = claims.tenant_id
    body = await request.json()
    document_id = body.get("document_id")

    if not document_id:
        raise HTTPException(400, {"error": "document_id is required"})

    async with AsyncSessionLocal() as session:
        # Get next sort_order
        max_row = (await session.execute(
            sa_text("""
                SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order
                FROM project_documents
                WHERE project_id = CAST(:pid AS uuid)
                  AND TRIM(tenant_id) = :tid
                  AND role = 'exhibit'
            """),
            {"pid": project_id, "tid": tid},
        )).fetchone()
        next_order = max_row.next_order if max_row else 0

        storage_path = None
        filename = body.get("filename")
        mime_type = None
        file_size = None

        # Try documents table first
        doc_row = (await session.execute(
            sa_text("""
                SELECT filename, storage_path, mime_type, file_size
                FROM documents
                WHERE id = CAST(:did AS uuid)
                  AND TRIM(tenant_id) = :tid
                LIMIT 1
            """),
            {"did": document_id, "tid": tid},
        )).fetchone()

        if doc_row:
            storage_path = doc_row.storage_path
            filename = filename or doc_row.filename
            mime_type = doc_row.mime_type
            file_size = doc_row.file_size
        else:
            # Try dms_documents
            dms_row = (await session.execute(
                sa_text("""
                    SELECT file_path, file_size_bytes
                    FROM dms_documents
                    WHERE id = CAST(:did AS uuid)
                      AND TRIM(tenant_id) = :tid
                    LIMIT 1
                """),
                {"did": document_id, "tid": tid},
            )).fetchone()
            if dms_row:
                storage_path = dms_row.file_path
                filename = filename or os.path.basename(dms_row.file_path or "")
                file_size = dms_row.file_size_bytes
            else:
                raise HTTPException(404, {"error": "document_not_found"})

        result = (await session.execute(
            sa_text("""
                INSERT INTO project_documents
                    (tenant_id, project_id, document_id, storage_path, filename,
                     mime_type, file_size, role, sort_order, added_by,
                     created_at, updated_at)
                VALUES
                    (:tid, CAST(:pid AS uuid), CAST(:did AS uuid),
                     :spath, :fname, :mime, :fsize, 'exhibit', :sort,
                     :uid, NOW(), NOW())
                RETURNING id::text
            """),
            {
                "tid": tid, "pid": project_id, "did": document_id,
                "spath": storage_path, "fname": filename or "document",
                "mime": mime_type, "fsize": file_size,
                "sort": next_order, "uid": claims.user_id,
            },
        )).fetchone()
        await session.commit()

    return JSONResponse(
        status_code=201,
        content={
            "id": result.id,
            "filename": filename,
            "sort_order": next_order,
            "exhibit_number": next_order + 1,
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# GET /pending-checkouts — Poll for platform-initiated checkouts
# ═══════════════════════════════════════════════════════════════════════

@router.get("/pending-checkouts")
async def pending_checkouts(
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Return documents checked out by the current user that need opening.

    The VSTO client polls this every 10 seconds. When a checkout is found
    that isn't already open locally, the client downloads the file via
    GET /open/{doc_id} and opens it in Word.

    Returns only documents where metadata->>'checked_out_by' matches
    the current user's email AND metadata->>'desktop_opened' is not set.
    After the VSTO client opens the document, it calls
    POST /checkout/{doc_id}/mark-opened to suppress future polls.
    """
    tid = claims.tenant_id

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa_text("""
                SELECT
                    d.id::text           AS id,
                    d.filename,
                    d.original_filename,
                    d.mime_type,
                    d.file_size,
                    d.matter_id::text    AS matter_id,
                    d.storage_path,
                    d.metadata->>'checked_out_by'   AS checked_out_by,
                    d.metadata->>'checked_out_at'   AS checked_out_at,
                    m.matter_name,
                    m.matter_number
                FROM documents d
                LEFT JOIN matters m ON m.id = d.matter_id
                WHERE TRIM(d.tenant_id) = :tid
                  AND d.metadata->>'checked_out_by' = :email
                  AND COALESCE(d.metadata->>'desktop_opened', 'false') != 'true'
                  AND d.metadata->>'checked_out_at' IS NOT NULL
                ORDER BY d.metadata->>'checked_out_at' DESC
                LIMIT 10
            """),
            {"tid": tid, "email": claims.email},
        )
        rows = [_row_dict(r) for r in result.mappings().all()]

    return {"pending": rows, "count": len(rows)}


# ═══════════════════════════════════════════════════════════════════════
# POST /checkout/{doc_id}/mark-opened — Suppress future polls
# ═══════════════════════════════════════════════════════════════════════

@router.post("/checkout/{doc_id}/mark-opened")
async def mark_checkout_opened(
    doc_id: str = PathParam(...),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Mark a checked-out document as opened in the desktop client.

    Sets metadata->>'desktop_opened' = 'true' so the pending-checkouts
    endpoint stops returning it.
    """
    tid = claims.tenant_id

    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_text("""
                UPDATE documents
                SET metadata = COALESCE(metadata, '{}'::jsonb)
                               || '{"desktop_opened": "true"}'::jsonb,
                    updated_at = NOW()
                WHERE id = CAST(:doc AS uuid)
                  AND TRIM(tenant_id) = :tid
                  AND metadata->>'checked_out_by' = :email
            """),
            {"doc": doc_id, "tid": tid, "email": claims.email},
        )
        await session.commit()

    return {"marked": True, "doc_id": doc_id}


# ═══════════════════════════════════════════════════════════════════════
# POST /projects/{pid}/exhibits/upload — Upload file directly as exhibit
# ═══════════════════════════════════════════════════════════════════════

@router.post("/projects/{project_id}/exhibits/upload")
async def upload_exhibit(
    request: Request,
    project_id: str = PathParam(...),
    file: UploadFile = File(...),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Upload a file directly as an exhibit (multipart).

    Creates project_documents row with role='exhibit', copies file to
    /mnt/praesidium/{tenant}/projects/{project_id}/exhibits/{filename}.
    """
    tid = claims.tenant_id

    # Verify project exists
    async with AsyncSessionLocal() as session:
        proj = (await session.execute(
            sa_text("""SELECT id, matter_id::text AS matter_id
                       FROM projects
                       WHERE id = CAST(:pid AS uuid)
                         AND TRIM(tenant_id) = :tid LIMIT 1"""),
            {"pid": project_id, "tid": tid},
        )).fetchone()
        if not proj:
            raise HTTPException(404, {"error": "project_not_found"})

    upload_bytes = await file.read()
    if not upload_bytes:
        raise HTTPException(400, {"error": "empty_upload"})

    filename = file.filename or "exhibit.pdf"
    mime_type = file.content_type or "application/octet-stream"
    checksum = hashlib.sha256(upload_bytes).hexdigest()

    # Store in project exhibit directory
    exhibit_dir = _storage_root() / tid.strip() / "projects" / project_id / "exhibits"
    exhibit_dir.mkdir(parents=True, exist_ok=True)

    dest = exhibit_dir / filename
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        dest = exhibit_dir / f"{stem}_{checksum[:8]}{suffix}"
    dest.write_bytes(upload_bytes)

    async with AsyncSessionLocal() as session:
        # Get next sort_order
        so_row = (await session.execute(
            sa_text("""SELECT COALESCE(MAX(sort_order), -1) + 1 AS n
                       FROM project_documents
                       WHERE project_id = CAST(:pid AS uuid)
                         AND TRIM(tenant_id) = :tid AND role = 'exhibit'"""),
            {"pid": project_id, "tid": tid},
        )).fetchone()
        next_order = so_row.n if so_row else 0

        label = _alpha_label(next_order)

        result = (await session.execute(
            sa_text("""
                INSERT INTO project_documents
                    (tenant_id, project_id, document_id, storage_path, filename,
                     mime_type, file_size, role, sort_order, exhibit_label,
                     exhibit_number, added_by, created_at, updated_at)
                VALUES
                    (:tid, CAST(:pid AS uuid), NULL, :spath, :fname,
                     :mime, :fsize, 'exhibit', :sort, :label,
                     :num, :uid, NOW(), NOW())
                RETURNING id::text
            """),
            {
                "tid": tid, "pid": project_id,
                "spath": str(dest), "fname": filename,
                "mime": mime_type, "fsize": len(upload_bytes),
                "sort": next_order, "label": f"Exhibit {label}",
                "num": next_order + 1, "uid": claims.user_id,
            },
        )).fetchone()
        await session.commit()

    return JSONResponse(status_code=201, content={
        "id": result.id,
        "filename": filename,
        "storage_path": str(dest),
        "sort_order": next_order,
        "exhibit_label": f"Exhibit {label}",
        "exhibit_number": next_order + 1,
        "file_size": len(upload_bytes),
    })


# ═══════════════════════════════════════════════════════════════════════
# POST /projects/{pid}/exhibits/{eid}/emboss — Stamp exhibit sticker
# ═══════════════════════════════════════════════════════════════════════

def _resolve_exhibit_path(storage_path: str, tenant_id: str) -> Optional[str]:
    """Resolve the actual disk path for an exhibit, trying multiple strategies."""
    if not storage_path:
        return None

    candidates = [storage_path]

    # If relative path starting with praesidium/
    if storage_path.startswith("praesidium/"):
        rest = storage_path[len("praesidium/"):]
        candidates.append(f"/mnt/praesidium/{tenant_id.strip()}/{rest}")
        candidates.append(f"/mnt/{storage_path}")

    # If it doesn't start with /mnt, try prepending
    if not storage_path.startswith("/mnt"):
        candidates.append(f"/mnt/praesidium/{tenant_id.strip()}/{storage_path}")

    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


@router.post("/projects/{project_id}/exhibits/{exhibit_id}/emboss")
async def emboss_exhibit(
    request: Request,
    project_id: str = PathParam(...),
    exhibit_id: str = PathParam(...),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Stamp exhibit sticker on page 1 of a PDF.

    Optional JSON body: {"color": "transparent", "style": "3line",
    "position": "top-right", "margin": 36, "case_info": "..."}
    Colors: transparent (clear/default), white, blue
    """
    tid = claims.tenant_id
    try:
        body = await request.json()
    except Exception:
        body = {}

    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            sa_text("""
                SELECT pd.id::text, pd.storage_path, pd.filename, pd.mime_type,
                       pd.exhibit_label, pd.bates_embossed,
                       p.config AS project_config,
                       m.matter_name, m.matter_number
                FROM project_documents pd
                JOIN projects p ON p.id = pd.project_id AND TRIM(p.tenant_id) = :tid
                LEFT JOIN matters m ON m.id = p.matter_id
                WHERE pd.id = CAST(:eid AS uuid)
                  AND pd.project_id = CAST(:pid AS uuid)
                  AND TRIM(pd.tenant_id) = :tid
                LIMIT 1
            """),
            {"eid": exhibit_id, "pid": project_id, "tid": tid},
        )).mappings().first()

    if not row:
        raise HTTPException(404, {"error": "exhibit_not_found"})
    if row["bates_embossed"]:
        return {"already_embossed": True, "exhibit_id": exhibit_id,
                "exhibit_label": row["exhibit_label"]}

    filename = row["filename"] or ""
    ext = os.path.splitext(filename.lower())[1]
    is_pdf = ext == ".pdf" or (row["mime_type"] or "").startswith("application/pdf")
    if not is_pdf:
        raise HTTPException(400, {"error": "pdf_only",
            "detail": f"Exhibit embossing requires PDF. Got {ext or row['mime_type']}. Convert Word docs to PDF first."})

    disk_path = _resolve_exhibit_path(row["storage_path"], tid)
    if not disk_path:
        raise HTTPException(404, {"error": "file_not_found", "storage_path": row["storage_path"]})

    proj_cfg = {}
    if row["project_config"] and isinstance(row["project_config"], dict):
        proj_cfg = row["project_config"].get("exhibit_sticker_config", {})

    default_case_info = None
    if row.get("matter_number") and row.get("matter_name"):
        default_case_info = f"{row['matter_number']} | {row['matter_name']}"
    elif row.get("matter_name"):
        default_case_info = row["matter_name"]

    color = body.get("color") or proj_cfg.get("color", "transparent")
    style = body.get("style") or proj_cfg.get("style", "3line")
    position = body.get("position") or proj_cfg.get("position", "top-right")
    margin = body.get("margin") or proj_cfg.get("margin", 36)
    case_info = body.get("case_info") or proj_cfg.get("case_info") or default_case_info

    label = row["exhibit_label"] or "Exhibit"
    label_short = label[8:].strip() if label.lower().startswith("exhibit ") else label

    tmp_path = disk_path + ".emboss.tmp"
    ok = _engine_emboss(
        input_pdf_path=disk_path, output_pdf_path=tmp_path,
        exhibit_label=label_short, case_info=case_info,
        color=color, style=style, position=position,
        margin=int(margin), page_number=0,
    )
    if not ok:
        if os.path.exists(tmp_path): os.remove(tmp_path)
        raise HTTPException(500, {"error": "emboss_failed"})
    os.replace(tmp_path, disk_path)

    page_count = None
    try:
        import fitz; d = fitz.open(disk_path); page_count = len(d); d.close()
    except Exception: pass

    async with AsyncSessionLocal() as session:
        params = {"eid": exhibit_id, "tid": tid}
        sql = "UPDATE project_documents SET bates_embossed = true, updated_at = NOW()"
        if page_count is not None:
            sql += ", page_count = :pc"; params["pc"] = page_count
        sql += " WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid"
        await session.execute(sa_text(sql), params)
        await session.commit()

    return {"embossed": True, "exhibit_id": exhibit_id, "exhibit_label": label,
            "sticker": {"color": color, "style": style, "position": position, "case_info": case_info},
            "page_count": page_count}


@router.post("/projects/{project_id}/exhibits/emboss-all")
async def emboss_all_exhibits(
    request: Request,
    project_id: str = PathParam(...),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Batch-emboss all un-embossed PDF exhibits. Colors: transparent (default), white, blue."""
    tid = claims.tenant_id
    try:
        body = await request.json()
    except Exception:
        body = {}

    async with AsyncSessionLocal() as session:
        proj_row = (await session.execute(
            sa_text("""SELECT p.id::text, p.config, m.matter_name, m.matter_number
                FROM projects p LEFT JOIN matters m ON m.id = p.matter_id
                WHERE p.id = CAST(:pid AS uuid) AND TRIM(p.tenant_id) = :tid LIMIT 1"""),
            {"pid": project_id, "tid": tid},
        )).mappings().first()
        if not proj_row:
            raise HTTPException(404, {"error": "project_not_found"})

        exhibits = (await session.execute(
            sa_text("""SELECT id::text, storage_path, filename, mime_type, exhibit_label
                FROM project_documents
                WHERE project_id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid
                  AND role = 'exhibit' AND bates_embossed = false
                ORDER BY sort_order, created_at"""),
            {"pid": project_id, "tid": tid},
        )).mappings().all()

    if not exhibits:
        return {"embossed_count": 0, "skipped_count": 0, "message": "No un-embossed exhibits"}

    proj_cfg = {}
    if proj_row["config"] and isinstance(proj_row["config"], dict):
        proj_cfg = proj_row["config"].get("exhibit_sticker_config", {})
    default_case_info = None
    if proj_row.get("matter_number") and proj_row.get("matter_name"):
        default_case_info = f"{proj_row['matter_number']} | {proj_row['matter_name']}"
    elif proj_row.get("matter_name"):
        default_case_info = proj_row["matter_name"]

    color = body.get("color") or proj_cfg.get("color", "transparent")
    style = body.get("style") or proj_cfg.get("style", "3line")
    position = body.get("position") or proj_cfg.get("position", "top-right")
    margin = int(body.get("margin") or proj_cfg.get("margin", 36))
    case_info = body.get("case_info") or proj_cfg.get("case_info") or default_case_info

    embossed, skipped, errors = [], [], []
    for ex in exhibits:
        filename = ex["filename"] or ""
        ext_lower = os.path.splitext(filename.lower())[1]
        is_pdf = ext_lower == ".pdf" or (ex["mime_type"] or "").startswith("application/pdf")
        if not is_pdf:
            skipped.append({"id": ex["id"], "filename": filename, "reason": "not_pdf"}); continue
        disk_path = _resolve_exhibit_path(ex["storage_path"], tid)
        if not disk_path:
            skipped.append({"id": ex["id"], "filename": filename, "reason": "file_not_found"}); continue

        label = ex["exhibit_label"] or "Exhibit"
        label_short = label[8:].strip() if label.lower().startswith("exhibit ") else label
        tmp_path = disk_path + ".emboss.tmp"
        ok = _engine_emboss(input_pdf_path=disk_path, output_pdf_path=tmp_path,
            exhibit_label=label_short, case_info=case_info, color=color,
            style=style, position=position, margin=margin, page_number=0)
        if ok:
            os.replace(tmp_path, disk_path)
            pc = None
            try:
                import fitz; d = fitz.open(disk_path); pc = len(d); d.close()
            except Exception: pass
            async with AsyncSessionLocal() as session:
                params = {"eid": ex["id"], "tid": tid}
                sql = "UPDATE project_documents SET bates_embossed = true, updated_at = NOW()"
                if pc is not None: sql += ", page_count = :pc"; params["pc"] = pc
                sql += " WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid"
                await session.execute(sa_text(sql), params); await session.commit()
            embossed.append({"id": ex["id"], "filename": filename, "label": label})
        else:
            if os.path.exists(tmp_path): os.remove(tmp_path)
            errors.append({"id": ex["id"], "filename": filename})

    return {"embossed_count": len(embossed), "skipped_count": len(skipped),
            "error_count": len(errors), "embossed": embossed, "skipped": skipped, "errors": errors,
            "sticker": {"color": color, "style": style, "position": position, "case_info": case_info}}



# =======================================================================
# ═══════════════════════════════════════════════════════════════════════

@router.get("/projects/{project_id}/exhibits/{exhibit_id}/download")
async def download_exhibit(
    project_id: str = PathParam(...),
    exhibit_id: str = PathParam(...),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Download exhibit file bytes for the VSTO PDF viewer or local save."""
    tid = claims.tenant_id
    from fastapi.responses import FileResponse

    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            sa_text("""
                SELECT storage_path, filename, mime_type
                FROM project_documents
                WHERE id = CAST(:eid AS uuid)
                  AND project_id = CAST(:pid AS uuid)
                  AND TRIM(tenant_id) = :tid
                LIMIT 1
            """),
            {"eid": exhibit_id, "pid": project_id, "tid": tid},
        )).mappings().first()

    if not row:
        raise HTTPException(404, {"error": "exhibit_not_found"})

    disk_path = _resolve_exhibit_path(row["storage_path"], tid)
    if not disk_path:
        raise HTTPException(404, {"error": "file_not_found",
                                  "storage_path": row["storage_path"]})

    return FileResponse(
        path=disk_path,
        filename=row["filename"] or "exhibit",
        media_type=row["mime_type"] or "application/octet-stream",
    )


# ═══════════════════════════════════════════════════════════════════════
# POST /projects/{pid}/documents/{did}/save — Save edited file back
# ═══════════════════════════════════════════════════════════════════════

@router.post("/projects/{project_id}/documents/{doc_id}/save")
async def save_project_document(
    request: Request,
    project_id: str = PathParam(...),
    doc_id: str = PathParam(...),
    file: UploadFile = File(...),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Save edited file bytes back to a project document.

    Overwrites the file at storage_path. If the document was originally
    in chats/ staging, copies to the project's working directory first
    and updates storage_path.

    Used by the VSTO client when the user edits the base document and
    saves it back to the project.
    """
    tid = claims.tenant_id

    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            sa_text("""
                SELECT id::text, storage_path, filename, role
                FROM project_documents
                WHERE id = CAST(:did AS uuid)
                  AND project_id = CAST(:pid AS uuid)
                  AND TRIM(tenant_id) = :tid
                LIMIT 1
            """),
            {"did": doc_id, "pid": project_id, "tid": tid},
        )).mappings().first()

    if not row:
        raise HTTPException(404, {"error": "document_not_found"})

    upload_bytes = await file.read()
    if not upload_bytes:
        raise HTTPException(400, {"error": "empty_upload"})

    checksum = hashlib.sha256(upload_bytes).hexdigest()
    filename = row["filename"] or file.filename or "document.docx"

    storage_path = row["storage_path"]

    # If the file is in chats/ staging or has no real path, relocate to project working dir
    needs_relocate = (
        not storage_path
        or "/chats/" in (storage_path or "")
        or not storage_path.startswith("/")
    )

    if needs_relocate:
        project_dir = _storage_root() / tid.strip() / "projects" / project_id / "working"
        project_dir.mkdir(parents=True, exist_ok=True)
        dest = project_dir / filename
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            dest = project_dir / f"{stem}_{checksum[:8]}{suffix}"
        dest.write_bytes(upload_bytes)
        new_path = str(dest)
    else:
        # Overwrite in place
        disk_path = _resolve_exhibit_path(storage_path, tid)
        if disk_path:
            Path(disk_path).write_bytes(upload_bytes)
            new_path = disk_path
        else:
            # Path doesn't exist — write to project working dir
            project_dir = _storage_root() / tid.strip() / "projects" / project_id / "working"
            project_dir.mkdir(parents=True, exist_ok=True)
            dest = project_dir / filename
            dest.write_bytes(upload_bytes)
            new_path = str(dest)

    now = datetime.now(timezone.utc)

    # Update the project_documents row
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_text("""
                UPDATE project_documents
                SET storage_path = :spath,
                    file_size = :fsize,
                    updated_at = NOW()
                WHERE id = CAST(:did AS uuid)
                  AND TRIM(tenant_id) = :tid
            """),
            {"spath": new_path, "fsize": len(upload_bytes),
             "did": doc_id, "tid": tid},
        )
        await session.commit()

    logger.info(
        "[m-desk] save-back doc=%s project=%s bytes=%d tenant=%s",
        doc_id, project_id, len(upload_bytes), tid,
    )

    return JSONResponse(content={
        "saved": True,
        "doc_id": doc_id,
        "storage_path": new_path,
        "file_size": len(upload_bytes),
        "checksum": checksum,
        "relocated": needs_relocate,
        "saved_at": now.isoformat(),
    })


# ═══════════════════════════════════════════════════════════════════════
# POST /projects/{pid}/save-to-project — Smart save with mode support
# ═══════════════════════════════════════════════════════════════════════

@router.post("/projects/{project_id}/save-to-project")
async def save_to_project(
    request: Request,
    project_id: str = PathParam(...),
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    mode: Optional[str] = Form("auto"),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Save a file to a project with mode control.

    Modes:
      auto      - overwrite if same filename exists, else new exhibit
      overwrite - overwrite existing file bytes (404 if not found)
      version   - create new version: filename_v2.docx as new exhibit
      new       - always create a new exhibit

    Returns: {saved, mode_used, is_update, doc_id, filename, exhibit_label, ...}
    """
    tid = claims.tenant_id

    # Verify project
    async with AsyncSessionLocal() as session:
        proj = (await session.execute(
            sa_text("""SELECT id, matter_id::text AS matter_id
                       FROM projects
                       WHERE id = CAST(:pid AS uuid)
                         AND TRIM(tenant_id) = :tid LIMIT 1"""),
            {"pid": project_id, "tid": tid},
        )).fetchone()
        if not proj:
            raise HTTPException(404, {"error": "project_not_found"})

    upload_bytes = await file.read()
    if not upload_bytes:
        raise HTTPException(400, {"error": "empty_upload"})

    filename = title or file.filename or "document.docx"
    if not os.path.splitext(filename)[1]:
        orig_ext = os.path.splitext(file.filename or "")[1]
        if orig_ext:
            filename += orig_ext

    mime_type = file.content_type or "application/octet-stream"
    checksum = hashlib.sha256(upload_bytes).hexdigest()
    now = datetime.now(timezone.utc)

    # Check for existing document with same filename
    async with AsyncSessionLocal() as session:
        existing = (await session.execute(
            sa_text("""
                SELECT id::text, storage_path, filename, exhibit_label,
                       exhibit_number, sort_order
                FROM project_documents
                WHERE project_id = CAST(:pid AS uuid)
                  AND TRIM(tenant_id) = :tid
                  AND LOWER(filename) = LOWER(:fname)
                ORDER BY updated_at DESC
                LIMIT 1
            """),
            {"pid": project_id, "tid": tid, "fname": filename},
        )).mappings().first()

    effective_mode = mode or "auto"

    # ── AUTO: decide based on whether file exists ────────────
    if effective_mode == "auto":
        effective_mode = "overwrite" if existing else "new"

    # ── OVERWRITE ────────────────────────────────────────────
    if effective_mode == "overwrite":
        if not existing:
            raise HTTPException(404, {
                "error": "no_existing_document",
                "detail": f"No document named '{filename}' found in project"
            })

        doc_id = existing["id"]
        storage_path = existing["storage_path"]

        disk_path = _resolve_exhibit_path(storage_path, tid)
        if disk_path and os.path.isfile(disk_path):
            Path(disk_path).write_bytes(upload_bytes)
            new_path = disk_path
        else:
            exhibit_dir = _storage_root() / tid.strip() / "projects" / project_id / "exhibits"
            exhibit_dir.mkdir(parents=True, exist_ok=True)
            dest = exhibit_dir / filename
            dest.write_bytes(upload_bytes)
            new_path = str(dest)

        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_text("""
                    UPDATE project_documents
                    SET storage_path = :spath, file_size = :fsize,
                        mime_type = :mime, updated_at = NOW()
                    WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
                """),
                {"spath": new_path, "fsize": len(upload_bytes),
                 "mime": mime_type, "did": doc_id, "tid": tid},
            )
            await session.commit()

        logger.info("[m-desk] save-to-project OVERWRITE doc=%s project=%s", doc_id, project_id)

        return JSONResponse(content={
            "saved": True, "mode_used": "overwrite", "is_update": True,
            "doc_id": doc_id, "filename": filename,
            "storage_path": new_path, "file_size": len(upload_bytes),
            "checksum": checksum,
            "exhibit_label": existing.get("exhibit_label"),
        })

    # ── VERSION — new exhibit with _v2, _v3 suffix ──────────
    if effective_mode == "version":
        stem = os.path.splitext(filename)[0]
        ext = os.path.splitext(filename)[1]

        # Find next version number
        async with AsyncSessionLocal() as session:
            version_rows = (await session.execute(
                sa_text("""
                    SELECT filename FROM project_documents
                    WHERE project_id = CAST(:pid AS uuid)
                      AND TRIM(tenant_id) = :tid
                      AND LOWER(filename) LIKE LOWER(:pattern)
                """),
                {"pid": project_id, "tid": tid,
                 "pattern": stem + "%"},
            )).fetchall()

        max_ver = 1
        import re as _re
        for vr in version_rows:
            fn = vr.filename or ""
            m = _re.search(r'_v(\d+)', fn, _re.IGNORECASE)
            if m:
                max_ver = max(max_ver, int(m.group(1)))
            else:
                max_ver = max(max_ver, 1)
        next_ver = max_ver + 1
        versioned_name = f"{stem}_v{next_ver}{ext}"

        # Write file
        exhibit_dir = _storage_root() / tid.strip() / "projects" / project_id / "exhibits"
        exhibit_dir.mkdir(parents=True, exist_ok=True)
        dest = exhibit_dir / versioned_name
        dest.write_bytes(upload_bytes)

        # Create new exhibit row
        async with AsyncSessionLocal() as session:
            so_row = (await session.execute(
                sa_text("""SELECT COALESCE(MAX(sort_order), -1) + 1 AS n
                           FROM project_documents
                           WHERE project_id = CAST(:pid AS uuid)
                             AND TRIM(tenant_id) = :tid AND role = 'exhibit'"""),
                {"pid": project_id, "tid": tid},
            )).fetchone()
            next_order = so_row.n if so_row else 0
            label = _alpha_label(next_order)

            result = (await session.execute(
                sa_text("""
                    INSERT INTO project_documents
                        (tenant_id, project_id, document_id, storage_path, filename,
                         mime_type, file_size, role, sort_order, exhibit_label,
                         exhibit_number, added_by, created_at, updated_at)
                    VALUES
                        (:tid, CAST(:pid AS uuid), NULL, :spath, :fname,
                         :mime, :fsize, 'exhibit', :sort, :label,
                         :num, :uid, NOW(), NOW())
                    RETURNING id::text
                """),
                {
                    "tid": tid, "pid": project_id,
                    "spath": str(dest), "fname": versioned_name,
                    "mime": mime_type, "fsize": len(upload_bytes),
                    "sort": next_order, "label": f"Exhibit {label}",
                    "num": next_order + 1, "uid": claims.user_id,
                },
            )).fetchone()
            await session.commit()

        logger.info("[m-desk] save-to-project VERSION doc=%s v%d project=%s",
                    result.id, next_ver, project_id)

        return JSONResponse(status_code=201, content={
            "saved": True, "mode_used": "version", "is_update": False,
            "doc_id": result.id, "filename": versioned_name,
            "version_number": next_ver,
            "storage_path": str(dest), "file_size": len(upload_bytes),
            "checksum": checksum,
            "exhibit_label": f"Exhibit {label}",
        })

    # ── NEW — always create new exhibit ─────────────────────
    exhibit_dir = _storage_root() / tid.strip() / "projects" / project_id / "exhibits"
    exhibit_dir.mkdir(parents=True, exist_ok=True)
    dest = exhibit_dir / filename
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        dest = exhibit_dir / f"{stem}_{checksum[:8]}{suffix}"
    dest.write_bytes(upload_bytes)

    async with AsyncSessionLocal() as session:
        so_row = (await session.execute(
            sa_text("""SELECT COALESCE(MAX(sort_order), -1) + 1 AS n
                       FROM project_documents
                       WHERE project_id = CAST(:pid AS uuid)
                         AND TRIM(tenant_id) = :tid AND role = 'exhibit'"""),
            {"pid": project_id, "tid": tid},
        )).fetchone()
        next_order = so_row.n if so_row else 0
        label = _alpha_label(next_order)

        result = (await session.execute(
            sa_text("""
                INSERT INTO project_documents
                    (tenant_id, project_id, document_id, storage_path, filename,
                     mime_type, file_size, role, sort_order, exhibit_label,
                     exhibit_number, added_by, created_at, updated_at)
                VALUES
                    (:tid, CAST(:pid AS uuid), NULL, :spath, :fname,
                     :mime, :fsize, 'exhibit', :sort, :label,
                     :num, :uid, NOW(), NOW())
                RETURNING id::text
            """),
            {
                "tid": tid, "pid": project_id,
                "spath": str(dest), "fname": filename,
                "mime": mime_type, "fsize": len(upload_bytes),
                "sort": next_order, "label": f"Exhibit {label}",
                "num": next_order + 1, "uid": claims.user_id,
            },
        )).fetchone()
        await session.commit()

    logger.info("[m-desk] save-to-project NEW doc=%s project=%s", result.id, project_id)

    return JSONResponse(status_code=201, content={
        "saved": True, "mode_used": "new", "is_update": False,
        "doc_id": result.id, "filename": filename,
        "storage_path": str(dest), "file_size": len(upload_bytes),
        "checksum": checksum,
        "exhibit_label": f"Exhibit {label}",
    })
