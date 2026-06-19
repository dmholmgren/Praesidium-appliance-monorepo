"""
modules/depositions/routes/designations_api.py
Deterministic page:line designations (§6) + clip auto-create when video (§5/§8).
Backend for U5. The viewer's "Designate" button wires to POST /designations later.

Designations stay deterministic page:line spans. On create/edit we re-derive
char_start/char_end + excerpt_text from transcript_lines (never copy-paste), and
project the span through transcript_lines.timecode_ms into a clip in/out for video
transcripts.
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
router = APIRouter(prefix="/api/v1/depositions", tags=["depositions-designations-api"])

VALID_TYPES = {"affirmative", "counter", "objection"}


class Designation(BaseModel):
    transcript_id: str
    start_page: int
    start_line: int
    end_page: int
    end_line: int
    designating_party: Optional[str] = None
    designation_type: str = "affirmative"
    issue_code: Optional[str] = None
    color: Optional[str] = None
    note: Optional[str] = None


class DesignationPatch(BaseModel):
    start_page: Optional[int] = None
    start_line: Optional[int] = None
    end_page: Optional[int] = None
    end_line: Optional[int] = None
    designating_party: Optional[str] = None
    designation_type: Optional[str] = None
    issue_code: Optional[str] = None
    color: Optional[str] = None
    note: Optional[str] = None


async def _transcript_meta(session, tid, transcript_id):
    r = await session.execute(sa_text(
        "SELECT matter_id::text, session_id, has_video, has_timecodes "
        "FROM deposition_transcripts "
        "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
        {"id": transcript_id, "tid": tid})
    return r.mappings().fetchone()


async def _resolve_span(session, transcript_id, sp, sl, ep, el):
    """Derive (char_start, char_end, excerpt, in_ms, out_ms) for a page:line span
    from the addressing layer. char offsets index into canonical_text (§0)."""
    lo = sp * 100000 + sl
    hi = ep * 100000 + el
    r = await session.execute(sa_text(
        "SELECT page, line, text, char_start, char_end, timecode_ms "
        "FROM transcript_lines WHERE transcript_id = CAST(:id AS uuid) "
        "  AND (page*100000 + line) BETWEEN :lo AND :hi "
        "ORDER BY page, line"),
        {"id": transcript_id, "lo": lo, "hi": hi})
    rows = [dict(x) for x in r.mappings().fetchall()]
    if not rows:
        return None
    char_start = rows[0]["char_start"]
    char_end = rows[-1]["char_end"]
    excerpt = "\n".join(x["text"] or "" for x in rows)
    tcs = [x["timecode_ms"] for x in rows if x["timecode_ms"] is not None]
    in_ms = min(tcs) if tcs else None
    out_ms = max(tcs) if tcs else None
    return char_start, char_end, excerpt, in_ms, out_ms


async def _upsert_clip(session, tid, desig_id, transcript_id, session_id,
                       in_ms, out_ms, has_video, has_timecodes):
    """Auto-create/refresh the clip for a designation on a video transcript."""
    if not has_video:
        return
    status = "pending" if (in_ms is not None and out_ms is not None) else "awaiting_sync"
    # one clip per designation: refresh in/out if it exists, else create
    r = await session.execute(sa_text(
        "SELECT id::text FROM depo_clips WHERE designation_id = CAST(:d AS uuid)"),
        {"d": desig_id})
    existing = r.mappings().fetchone()
    if existing:
        await session.execute(sa_text(
            "UPDATE depo_clips SET in_ms=:i, out_ms=:o, render_status=:s, "
            "  rendered_path=NULL, updated_at=now() "
            "WHERE designation_id = CAST(:d AS uuid)"),
            {"i": in_ms, "o": out_ms, "s": status, "d": desig_id})
    else:
        await session.execute(sa_text(
            "INSERT INTO depo_clips "
            "  (tenant_id, designation_id, session_id, transcript_id, in_ms, out_ms, render_status) "
            "VALUES (TRIM(:tid), CAST(:d AS uuid), :sid, CAST(:trid AS uuid), :i, :o, :s)"),
            {"tid": tid, "d": desig_id, "sid": session_id, "trid": transcript_id,
             "i": in_ms, "o": out_ms, "s": status})


@router.get("/transcripts/{transcript_id}/designations")
async def list_designations(request: Request, transcript_id: str,
                            user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT d.id::text, d.designating_party, d.designation_type, "
                "       d.start_page, d.start_line, d.end_page, d.end_line, "
                "       d.issue_code, d.color, d.note, d.excerpt_text, "
                "       c.id::text AS clip_id, c.render_status, c.in_ms, c.out_ms, "
                "       c.object_uuid::text "
                "FROM depo_designations d "
                "LEFT JOIN depo_clips c ON c.designation_id = d.id "
                "WHERE d.transcript_id = CAST(:id AS uuid) AND TRIM(d.tenant_id) = TRIM(:tid) "
                "ORDER BY d.start_page, d.start_line"),
                {"id": transcript_id, "tid": tid})
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse(_serialize(rows))
    except Exception as e:
        logger.exception("list_designations failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/designations")
async def create_designation(request: Request, body: Designation,
                             user=Depends(get_current_user)):
    tid = _tenant(request)
    uid = getattr(getattr(request.state, "current_user", None), "id", None)
    if body.designation_type not in VALID_TYPES:
        return JSONResponse({"error": "invalid designation_type"}, 400)
    try:
        async with AsyncSessionLocal() as session:
            meta = await _transcript_meta(session, tid, body.transcript_id)
            if not meta:
                return JSONResponse({"error": "transcript not found"}, 404)
            span = await _resolve_span(session, body.transcript_id,
                                       body.start_page, body.start_line,
                                       body.end_page, body.end_line)
            if not span:
                return JSONResponse({"error": "no lines in that page:line span"}, 400)
            char_start, char_end, excerpt, in_ms, out_ms = span
            r = await session.execute(sa_text(
                "INSERT INTO depo_designations "
                "  (tenant_id, matter_id, session_id, transcript_id, designating_party, "
                "   designation_type, start_page, start_line, end_page, end_line, "
                "   char_start, char_end, excerpt_text, issue_code, color, note, created_by) "
                "VALUES (TRIM(:tid), CAST(:mid AS uuid), :sid, CAST(:trid AS uuid), :party, "
                "        :dtype, :sp, :sl, :ep, :el, :cs, :ce, :excerpt, :ic, :color, :note, :uid) "
                "RETURNING id::text"),
                {"tid": tid, "mid": meta["matter_id"], "sid": meta["session_id"],
                 "trid": body.transcript_id, "party": body.designating_party,
                 "dtype": body.designation_type, "sp": body.start_page,
                 "sl": body.start_line, "ep": body.end_page, "el": body.end_line,
                 "cs": char_start, "ce": char_end, "excerpt": excerpt,
                 "ic": body.issue_code, "color": body.color, "note": body.note,
                 "uid": uid})
            did = r.mappings().fetchone()["id"]
            await _upsert_clip(session, tid, did, body.transcript_id, meta["session_id"],
                               in_ms, out_ms, meta["has_video"], meta["has_timecodes"])
            await session.commit()
        return JSONResponse({"id": did, "excerpt": excerpt,
                             "clip": bool(meta["has_video"])})
    except Exception as e:
        logger.exception("create_designation failed")
        return JSONResponse({"error": str(e)}, 500)


@router.patch("/designations/{designation_id}")
async def update_designation(request: Request, designation_id: str,
                             body: DesignationPatch, user=Depends(get_current_user)):
    tid = _tenant(request)
    if body.designation_type and body.designation_type not in VALID_TYPES:
        return JSONResponse({"error": "invalid designation_type"}, 400)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT transcript_id::text, start_page, start_line, end_page, end_line "
                "FROM depo_designations "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"id": designation_id, "tid": tid})
            cur = r.mappings().fetchone()
            if not cur:
                return JSONResponse({"error": "not found"}, 404)
            sp = body.start_page if body.start_page is not None else cur["start_page"]
            sl = body.start_line if body.start_line is not None else cur["start_line"]
            ep = body.end_page if body.end_page is not None else cur["end_page"]
            el = body.end_line if body.end_line is not None else cur["end_line"]
            span_changed = any(v is not None for v in
                               (body.start_page, body.start_line, body.end_page, body.end_line))

            sets = {"start_page": sp, "start_line": sl, "end_page": ep, "end_line": el}
            for k in ("designating_party", "designation_type", "issue_code", "color", "note"):
                v = getattr(body, k)
                if v is not None:
                    sets[k] = v

            in_ms = out_ms = None
            if span_changed:
                span = await _resolve_span(session, cur["transcript_id"], sp, sl, ep, el)
                if not span:
                    return JSONResponse({"error": "no lines in that page:line span"}, 400)
                cs, ce, excerpt, in_ms, out_ms = span
                sets.update({"char_start": cs, "char_end": ce, "excerpt_text": excerpt})

            cols = ", ".join(f"{k} = :{k}" for k in sets)
            params = dict(sets); params.update({"id": designation_id, "tid": tid})
            await session.execute(sa_text(
                "UPDATE depo_designations SET " + cols + ", updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"), params)

            if span_changed:
                meta = await _transcript_meta(session, tid, cur["transcript_id"])
                await _upsert_clip(session, tid, designation_id, cur["transcript_id"],
                                   meta["session_id"], in_ms, out_ms,
                                   meta["has_video"], meta["has_timecodes"])
            await session.commit()
        return JSONResponse({"id": designation_id, "span_changed": span_changed})
    except Exception as e:
        logger.exception("update_designation failed")
        return JSONResponse({"error": str(e)}, 500)


@router.delete("/designations/{designation_id}")
async def delete_designation(request: Request, designation_id: str,
                             user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(
                "DELETE FROM depo_designations "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"id": designation_id, "tid": tid})
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.exception("delete_designation failed")
        return JSONResponse({"error": str(e)}, 500)
