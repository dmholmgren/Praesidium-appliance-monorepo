"""
adobe_convert_api.py — API endpoint for PDF-to-DOCX conversion via Adobe.
Deploy to: /app/modules/dms/services/adobe_convert_api.py

Registers:
  POST /api/v1/dms/convert-pdf-to-word
"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pathlib import Path

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/dms", tags=["dms-adobe-convert"])

PRAESIDIUM_ROOT = Path("/mnt/praesidium")


@router.post("/convert-pdf-to-word")
async def convert_pdf_to_word(request: Request):
    """Convert a PDF in the DMS to DOCX via Adobe PDF Services.

    Body JSON: { "matter_id": "uuid", "file_path": "relative/path/to/file.pdf" }
    The file_path is relative to the matter root (same as used in the file list).
    Output DOCX is saved alongside the source PDF in the same folder.
    """
    tid = (getattr(request.state, "tenant_id", "") or "").strip()
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)

    body = await request.json()
    matter_id = body.get("matter_id", "")
    file_path = body.get("file_path", "")

    if not matter_id or not file_path:
        return JSONResponse({"error": "matter_id and file_path required"}, status_code=400)

    if not file_path.lower().endswith('.pdf'):
        return JSONResponse({"error": "Only PDF files can be converted"}, status_code=400)

    # Resolve the absolute disk path
    # file_path comes from the frontend as relative (e.g. "02-Pleadings/Motion.pdf")
    # We need to find the matter's disk root
    from sqlalchemy import text as sa_text
    from core.db.base import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        row = await db.execute(sa_text("""
            SELECT m.matter_name, m.matter_number, c.client_name
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id AND trim(c.tenant_id) = trim(m.tenant_id)
            WHERE m.id = CAST(:mid AS uuid) AND trim(m.tenant_id) = :tid
        """), {"mid": matter_id, "tid": tid})
        matter = row.mappings().fetchone()

    if not matter:
        return JSONResponse({"error": "Matter not found"}, status_code=404)

    # Build the matter disk root using the same logic as folder_seeder
    from modules.dms.jobs.folder_seeder import _sanitize_fs_name, _matter_dest_path
    matter_root = _matter_dest_path(tid, matter["client_name"], matter["matter_name"], matter["matter_number"])

    abs_path = matter_root / file_path
    if not abs_path.exists():
        # Try alternate: the path might be absolute already
        abs_path = Path(file_path)
        if not abs_path.exists():
            return JSONResponse({"error": f"File not found: {file_path}"}, status_code=404)

    # Security: ensure the file is within praesidium mount
    try:
        abs_path.resolve().relative_to(PRAESIDIUM_ROOT.resolve())
    except ValueError:
        return JSONResponse({"error": "Access denied"}, status_code=403)

    # Run conversion
    from modules.dms.services.adobe_pdf_service import convert_pdf_to_docx
    result = convert_pdf_to_docx(str(abs_path))

    if result["success"]:
        return JSONResponse({
            "success": True,
            "filename": result["filename"],
            "output_path": result["output_path"],
            "message": f"Converted to {result['filename']}",
        })
    else:
        return JSONResponse({
            "success": False,
            "error": result["error"],
        }, status_code=500 if "SDK" in result.get("error", "") or "API" in result.get("error", "") else 400)


@router.get("/adobe-status")
async def adobe_status(request: Request):
    """Check Adobe PDF Services configuration status."""
    from modules.dms.services.adobe_pdf_service import get_status
    return JSONResponse(get_status())
