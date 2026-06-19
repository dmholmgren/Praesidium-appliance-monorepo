"""
modules/depositions/routes/report_api.py
Designation report download (§6). Compiles depo_designations -> DOCX/PDF via the
deterministic builder and streams the file back. Frontend "Export report" button
wires here later.
"""
import asyncio
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel

from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant
from modules.depositions.jobs.designation_report import build_report, TEMPLATES, DEFAULT_TEMPLATE

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/depositions", tags=["depositions-report-api"])

_MEDIA = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


class ReportBody(BaseModel):
    transcript_id: Optional[str] = None
    session_id: Optional[int] = None
    format: str = "docx"
    template: str = DEFAULT_TEMPLATE
    report_date: str = ""


@router.get("/report-templates")
async def report_templates(request: Request, user=Depends(get_current_user)):
    return JSONResponse({"templates": list(TEMPLATES.keys()), "default": DEFAULT_TEMPLATE})


@router.post("/designation-report")
async def designation_report(request: Request, body: ReportBody,
                             user=Depends(get_current_user)):
    tid = _tenant(request)
    fmt = body.format if body.format in _MEDIA else "docx"
    if not body.transcript_id and body.session_id is None:
        return JSONResponse({"error": "transcript_id or session_id required"}, 400)
    try:
        out = await asyncio.get_event_loop().run_in_executor(
            None, build_report, tid, body.transcript_id, body.session_id,
            fmt, body.template, body.report_date)
        path = out.get("pdf_path") if fmt == "pdf" else out["path"]
        fname = "designation_report.%s" % fmt
        return FileResponse(path, media_type=_MEDIA[fmt], filename=fname)
    except Exception as e:
        logger.exception("designation_report failed")
        return JSONResponse({"error": str(e)}, 500)
