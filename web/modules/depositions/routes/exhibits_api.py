"""
modules/depositions/routes/exhibits_api.py
Deposition exhibit linking (no-copy, page:line-anchored) + cross-depo
prior-exhibit library. Backend for §4. Frontend panel wired later (U3 viewer
RightColumn currently a placeholder).
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant, _serialize

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/depositions", tags=["depositions-exhibits-api"])


class ExhibitLink(BaseModel):
    transcript_id: str
    document_id: Optional[str] = None
    document_source: Optional[str] = None       # 'dms' | 'ediscovery' | 'external'
    exhibit_number: Optional[str] = None
    exhibit_label: Optional[str] = None
    anchor_page: Optional[int] = None
    anchor_line: Optional[int] = None
    qa_unit_id: Optional[str] = None
    marked_by: Optional[str] = None
    notes: Optional[str] = None
    viaticum_exhibit_id: Optional[int] = None


class ExhibitPatch(BaseModel):
    exhibit_number: Optional[str] = None
    exhibit_label: Optional[str] = None
    anchor_page: Optional[int] = None
    anchor_line: Optional[int] = None
    notes: Optional[str] = None
    document_id: Optional[str] = None
    document_source: Optional[str] = None


@router.get("/transcripts/{transcript_id}/exhibits")
async def list_exhibits(request: Request, transcript_id: str,
                        user=Depends(get_current_user)):
    """Exhibits anchored in this transcript, in page:line order."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT id::text, document_id::text, document_source, exhibit_number, "
                "       exhibit_label, anchor_page, anchor_line, qa_unit_id::text, "
                "       marked_by, notes, viaticum_exhibit_id, updated_at "
                "FROM deposition_exhibit_links "
                "WHERE transcript_id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid) "
                "ORDER BY anchor_page NULLS LAST, anchor_line NULLS LAST, exhibit_number"),
                {"id": transcript_id, "tid": tid})
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse(_serialize(rows))
    except Exception as e:
        logger.exception("list_exhibits failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/exhibits")
async def create_exhibit(request: Request, body: ExhibitLink,
                         user=Depends(get_current_user)):
    """Link a DMS/eDiscovery document as an exhibit at a transcript page:line.
    No copy -- a metadata reference. matter_id/session_id inherited from the
    transcript."""
    tid = _tenant(request)
    uid = getattr(getattr(request.state, "current_user", None), "id", None)
    try:
        async with AsyncSessionLocal() as session:
            tr = await session.execute(sa_text(
                "SELECT matter_id::text, session_id FROM deposition_transcripts "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"id": body.transcript_id, "tid": tid})
            row = tr.mappings().fetchone()
            if not row:
                return JSONResponse({"error": "transcript not found"}, 404)
            r = await session.execute(sa_text(
                "INSERT INTO deposition_exhibit_links "
                "  (tenant_id, matter_id, transcript_id, session_id, viaticum_exhibit_id, "
                "   document_id, document_source, exhibit_number, exhibit_label, "
                "   anchor_page, anchor_line, qa_unit_id, marked_by, notes, created_by) "
                "VALUES (TRIM(:tid), CAST(:mid AS uuid), CAST(:trid AS uuid), :sid, :vex, "
                "        CAST(:doc AS uuid), :dsrc, :exnum, :exlabel, :pg, :ln, "
                "        CAST(:qa AS uuid), :marked, :notes, :uid) "
                "RETURNING id::text"),
                {"tid": tid, "mid": row["matter_id"], "trid": body.transcript_id,
                 "sid": row["session_id"], "vex": body.viaticum_exhibit_id,
                 "doc": body.document_id, "dsrc": body.document_source,
                 "exnum": body.exhibit_number, "exlabel": body.exhibit_label,
                 "pg": body.anchor_page, "ln": body.anchor_line,
                 "qa": body.qa_unit_id, "marked": body.marked_by,
                 "notes": body.notes, "uid": uid})
            new_id = r.mappings().fetchone()["id"]
            await session.commit()
        return JSONResponse({"id": new_id})
    except Exception as e:
        logger.exception("create_exhibit failed")
        return JSONResponse({"error": str(e)}, 500)


@router.patch("/exhibits/{exhibit_id}")
async def update_exhibit(request: Request, exhibit_id: str, body: ExhibitPatch,
                         user=Depends(get_current_user)):
    tid = _tenant(request)
    fields = {k: v for k, v in body.dict().items() if v is not None}
    if not fields:
        return JSONResponse({"error": "no fields"}, 400)
    sets, params = [], {"id": exhibit_id, "tid": tid}
    for k, v in fields.items():
        if k == "document_id":
            sets.append("document_id = CAST(:document_id AS uuid)")
        else:
            sets.append(f"{k} = :{k}")
        params[k] = v
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "UPDATE deposition_exhibit_links SET " + ", ".join(sets) +
                ", updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid) "
                "RETURNING id::text"), params)
            row = r.mappings().fetchone()
            await session.commit()
        if not row:
            return JSONResponse({"error": "not found"}, 404)
        return JSONResponse({"id": row["id"]})
    except Exception as e:
        logger.exception("update_exhibit failed")
        return JSONResponse({"error": str(e)}, 500)


@router.delete("/exhibits/{exhibit_id}")
async def delete_exhibit(request: Request, exhibit_id: str,
                         user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(
                "DELETE FROM deposition_exhibit_links "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"id": exhibit_id, "tid": tid})
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.exception("delete_exhibit failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/matters/{matter_id}/exhibit-library")
async def exhibit_library(request: Request, matter_id: str,
                          user=Depends(get_current_user)):
    """Cross-depo prior-exhibit library (§4): every exhibit used across all
    depositions in the matter, grouped by document, with the page:line usages so
    a prior exhibit can be reused in a new transcript without re-finding it."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT COALESCE(el.document_id::text, el.id::text) AS group_key, "
                "       el.document_id::text, el.document_source, "
                "       max(el.exhibit_label) AS exhibit_label, "
                "       count(*) AS usage_count, "
                "       array_agg(DISTINCT el.exhibit_number) "
                "         FILTER (WHERE el.exhibit_number IS NOT NULL) AS exhibit_numbers, "
                "       array_agg(DISTINCT t.deponent) AS deponents "
                "FROM deposition_exhibit_links el "
                "JOIN deposition_transcripts t ON t.id = el.transcript_id "
                "WHERE el.matter_id = CAST(:mid AS uuid) AND TRIM(el.tenant_id) = TRIM(:tid) "
                "GROUP BY group_key, el.document_id, el.document_source "
                "ORDER BY usage_count DESC, exhibit_label"),
                {"mid": matter_id, "tid": tid})
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse(_serialize({"library": rows}))
    except Exception as e:
        logger.exception("exhibit_library failed")
        return JSONResponse({"error": str(e)}, 500)
