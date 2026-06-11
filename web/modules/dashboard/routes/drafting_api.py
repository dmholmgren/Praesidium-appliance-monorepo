"""
modules/dashboard/routes/drafting_api.py — Drafting REST API

REST endpoints for drafting operations that the frontend calls directly
(as opposed to MCP tool calls via AI chat).

POST /api/v1/drafting/promote    — promote a staged draft to DMS

Patent Pending — Series 1/2/3 — D.M. Holmgren, Reg. No. 54,168
"""
import hashlib
import json
import logging
import mimetypes
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.drafting_api")

router = APIRouter(prefix="/api/v1/drafting", tags=["drafting"])


def _tid(request: Request) -> str:
    tid = getattr(request.state, "tenant_id", None)
    if not tid:
        raise HTTPException(401, "No tenant context")
    return tid.strip()


def _user(request: Request):
    return getattr(request.state, "current_user", None)


class PromoteRequest(BaseModel):
    draft_id: str
    subfolder: str = "11-Working Docs"


@router.post("/promote", response_class=JSONResponse)
async def promote_draft(request: Request, body: PromoteRequest):
    """Promote a staged draft from chats/ into the matter's DMS folder.
    Creates a documents DB record and moves the staging copy."""
    tid = _tid(request)

    async with AsyncSessionLocal() as db:
        row = (await db.execute(sa_text("""
            SELECT id::text, matter_id::text, filename, storage_path,
                   document_type, file_size
            FROM drafting_outputs
            WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
              AND promoted_at IS NULL AND deleted_at IS NULL
        """), {"did": body.draft_id, "tid": tid})).fetchone()

        if not row:
            raise HTTPException(404, "Draft not found or already promoted")

        src = Path(row.storage_path)
        if not src.is_file():
            raise HTTPException(404, "Staged file not found on disk")

        mf = (await db.execute(sa_text("""
            SELECT disk_root FROM matter_folders
            WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
              AND disk_root LIKE '/mnt/praesidium%' LIMIT 1
        """), {"mid": row.matter_id, "tid": tid})).fetchone()

        if not mf:
            raise HTTPException(404, "No Praesidium folder for this matter")

        root = Path(mf.disk_root)
        dest_dir = root / body.subfolder
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / row.filename

        if dest.exists():
            base_name, ext_part = dest.stem, dest.suffix
            counter = 1
            while dest.exists():
                dest = dest_dir / f"{base_name} ({counter}){ext_part}"
                counter += 1

        shutil.copy2(str(src), str(dest))
        content = dest.read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        mime = mimetypes.guess_type(str(dest))[0] or "application/octet-stream"

        doc_id = (await db.execute(sa_text("""
            INSERT INTO documents
                (id, tenant_id, matter_id, filename, original_filename, title,
                 mime_type, file_size, storage_path, document_type, status,
                 checksum, created_at, updated_at)
            VALUES (gen_random_uuid(), :tid, CAST(:mid AS uuid), :fname, :fname, :fname,
                    :mime, :fsize, :spath, :dtype, 'active', :checksum, NOW(), NOW())
            RETURNING id::text
        """), {
            "tid": tid, "mid": row.matter_id, "fname": dest.name,
            "mime": mime, "fsize": len(content), "spath": str(dest),
            "dtype": row.document_type or dest.suffix.lstrip(".").lower(),
            "checksum": checksum,
        })).scalar()

        await db.execute(sa_text("""
            UPDATE drafting_outputs
            SET promoted_at = NOW(), promoted_path = :path, promoted_doc_id = CAST(:did AS uuid)
            WHERE id = CAST(:oid AS uuid)
        """), {"path": str(dest), "did": doc_id, "oid": body.draft_id})

        await db.commit()

    try:
        src.unlink()
        session_dir = src.parent
        if session_dir.is_dir() and not any(session_dir.iterdir()):
            session_dir.rmdir()
    except OSError:
        pass

    return {
        "status": "ok",
        "doc_id": doc_id,
        "dms_path": str(dest),
        "filename": dest.name,
        "size": len(content),
        "promoted": True,
    }
