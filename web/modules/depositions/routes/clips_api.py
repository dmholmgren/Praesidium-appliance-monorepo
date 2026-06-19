"""clips_api.py -- U7 clip render + clip-object listing.

POST /api/v1/depositions/designations/{id}/render  ensure-clip + render
POST /api/v1/depositions/clips/{id}/render          (re-)render an existing clip
GET  /api/v1/depositions/clips                       clip objects for a matter/transcript

Render shells out to ffmpeg via modules.depositions.jobs.clip_render (sync
psycopg2), run off the event loop with asyncio.to_thread -- same shape as the
U6 report path. Prerequisite failures (no video / no timecodes) come back as
409 with an actionable message for the viewer to surface.
"""
import asyncio
import logging
import uuid as _uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant, _serialize

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/depositions", tags=["depositions-clips"])


def _render_sync(tenant, clip_id):
    from modules.depositions.jobs.clip_render import _connect, render_clip
    conn = _connect()
    try:
        return render_clip(conn, tenant, clip_id)
    finally:
        conn.close()


@router.get("/clips")
async def list_clips(request: Request, matter_id: str = Query(""),
                     transcript_id: str = Query(""),
                     user=Depends(get_current_user)):
    """Clip objects (rendered or pending) for a matter or transcript."""
    tid = _tenant(request)
    where = ["TRIM(c.tenant_id) = TRIM(:t)"]
    params = {"t": tid}
    if transcript_id:
        where.append("c.transcript_id = CAST(:tx AS uuid)")
        params["tx"] = transcript_id
    if matter_id:
        where.append("d.matter_id = CAST(:m AS uuid)")
        params["m"] = matter_id
    try:
        async with AsyncSessionLocal() as s:
            r = await s.execute(sa_text(
                "SELECT c.id::text, c.designation_id::text, c.in_ms, c.out_ms, "
                "       c.render_status, c.rendered_path, c.object_uuid::text, "
                "       d.start_page, d.start_line, d.end_page, d.end_line, "
                "       d.designating_party, d.designation_type, "
                "       doc.filename AS doc_filename "
                "FROM depo_clips c "
                "JOIN depo_designations d ON d.id = c.designation_id "
                "  AND TRIM(d.tenant_id) = TRIM(c.tenant_id) "
                "LEFT JOIN documents doc ON doc.id = c.object_uuid "
                "WHERE " + " AND ".join(where) +
                " ORDER BY d.start_page, d.start_line"), params)
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse(_serialize(rows))
    except Exception as e:
        logger.exception("list_clips failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/clips/{clip_id}/render")
async def render_existing_clip(clip_id: str, request: Request,
                               user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        res = await asyncio.to_thread(_render_sync, tid, clip_id)
        return JSONResponse(_serialize(res))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=409)


@router.post("/designations/{designation_id}/render")
async def render_designation(designation_id: str, request: Request,
                             user=Depends(get_current_user)):
    """Ensure a clip exists for the designation, then render it."""
    tid = _tenant(request)
    async with AsyncSessionLocal() as s:
        r = await s.execute(sa_text(
            "SELECT id::text FROM depo_clips WHERE designation_id = CAST(:d AS uuid) "
            "AND TRIM(tenant_id) = TRIM(:t) LIMIT 1"),
            {"d": designation_id, "t": tid})
        row = r.mappings().fetchone()
        if row:
            clip_id = row["id"]
        else:
            r2 = await s.execute(sa_text(
                "SELECT session_id, transcript_id::text FROM depo_designations "
                "WHERE id = CAST(:d AS uuid) AND TRIM(tenant_id) = TRIM(:t)"),
                {"d": designation_id, "t": tid})
            dd = r2.mappings().fetchone()
            if not dd:
                return JSONResponse({"error": "designation not found"}, status_code=404)
            clip_id = str(_uuid.uuid4())
            await s.execute(sa_text(
                "INSERT INTO depo_clips (id, tenant_id, designation_id, session_id, "
                "transcript_id, render_status, created_at, updated_at) "
                "VALUES (CAST(:id AS uuid), :t, CAST(:d AS uuid), :sid, "
                "CAST(:tx AS uuid), 'pending', NOW(), NOW())"),
                {"id": clip_id, "t": tid, "d": designation_id,
                 "sid": dd["session_id"], "tx": dd["transcript_id"]})
            await s.commit()
    try:
        res = await asyncio.to_thread(_render_sync, tid, clip_id)
        return JSONResponse(_serialize(res))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=409)
