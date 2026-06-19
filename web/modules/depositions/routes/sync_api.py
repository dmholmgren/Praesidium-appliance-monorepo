"""sync_api.py -- U9 sync lane: run forced alignment + manual timecode correction.

GET  /api/v1/depositions/transcripts/{id}/sync            sync status + coverage
POST /api/v1/depositions/transcripts/{id}/sync            run alignment (aeneas->whisper)
POST /api/v1/depositions/transcripts/{id}/timecodes/correct  set anchors + rescale

Alignment shells out to docker sidecars via modules.depositions.jobs.sync_align
(sync psycopg2), run off the event loop with asyncio.to_thread -- same shape as
the U7 clip-render path. Prerequisite failures (no video, no transcript lines,
aligner image absent + no whisper fallback) come back as 409 with an actionable
message for the viewer to surface.
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant, _serialize

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/depositions", tags=["depositions-sync"])


class Anchor(BaseModel):
    page: int
    line: int
    ms: int


class CorrectionBody(BaseModel):
    anchors: list[Anchor]


def _sync_run(tenant, transcript_id):
    from modules.depositions.jobs.sync_align import _connect, sync_transcript
    conn = _connect()
    try:
        return sync_transcript(conn, tenant, transcript_id)
    finally:
        conn.close()


def _correct_run(tenant, transcript_id, anchors):
    from modules.depositions.jobs.sync_align import _connect, apply_correction
    conn = _connect()
    try:
        return apply_correction(conn, tenant, transcript_id, anchors)
    finally:
        conn.close()


@router.get("/transcripts/{transcript_id}/sync")
async def sync_status(transcript_id: str, request: Request,
                      user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as s:
            r = await s.execute(sa_text(
                "SELECT t.has_video, (t.video_path IS NOT NULL) AS has_video_file, "
                "       t.has_timecodes, t.sync_engine, t.sync_coverage, t.synced_at, "
                "       t.video_duration_ms, "
                "       (SELECT count(*) FROM transcript_lines l "
                "          WHERE l.transcript_id = t.id AND l.timecode_ms IS NOT NULL) AS timecoded_lines, "
                "       (SELECT count(*) FROM transcript_lines l "
                "          WHERE l.transcript_id = t.id) AS total_lines, "
                "       (SELECT count(*) FROM transcript_lines l "
                "          WHERE l.transcript_id = t.id AND l.timecode_source = 'manual') AS manual_lines "
                "FROM deposition_transcripts t "
                "WHERE t.id = CAST(:id AS uuid) AND TRIM(t.tenant_id) = TRIM(:t)"),
                {"id": transcript_id, "t": tid})
            row = r.mappings().fetchone()
        if not row:
            return JSONResponse({"error": "transcript not found"}, status_code=404)
        return JSONResponse(_serialize(dict(row)))
    except Exception as e:
        logger.exception("sync_status failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/transcripts/{transcript_id}/sync")
async def run_sync(transcript_id: str, request: Request,
                   user=Depends(get_current_user)):
    """Forced-align this transcript to its video (aeneas, whisper fallback)."""
    tid = _tenant(request)
    try:
        res = await asyncio.to_thread(_sync_run, tid, transcript_id)
        return JSONResponse(_serialize(res))
    except Exception as e:
        logger.warning("run_sync failed for %s: %s", transcript_id, e)
        return JSONResponse({"error": str(e)}, status_code=409)


@router.post("/transcripts/{transcript_id}/timecodes/correct")
async def correct_timecodes(transcript_id: str, body: CorrectionBody,
                            request: Request, user=Depends(get_current_user)):
    """Pin true times for one or more lines (anchors) and rescale the rest."""
    tid = _tenant(request)
    anchors = [{"page": a.page, "line": a.line, "ms": a.ms} for a in body.anchors]
    if not anchors:
        return JSONResponse({"error": "no anchors supplied"}, status_code=400)
    try:
        res = await asyncio.to_thread(_correct_run, tid, transcript_id, anchors)
        return JSONResponse(_serialize(res))
    except Exception as e:
        logger.exception("correct_timecodes failed")
        return JSONResponse({"error": str(e)}, status_code=409)
