"""
modules/depositions/routes/depo_api.py
JSON API for the React deposition transcript viewer + semantic Q&A search.

Mirrors modules/ediscovery/routes/search_api.py conventions: async
AsyncSessionLocal + sa_text, tenant from request.state, get_current_user,
_serialize. Read-only over the U1/U2 substrate (deposition_transcripts,
transcript_lines, transcript_qa_units, transcript_qa_embeddings).
"""
import asyncio
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/depositions", tags=["depositions-api"])


def _tenant(r: Request) -> str:
    return (getattr(r.state, "tenant_id", "") or "").strip()


def _serialize(obj):
    import uuid
    from datetime import datetime, date
    from decimal import Decimal
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(v) for v in obj]
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return obj


# --- transcripts ------------------------------------------------------------

@router.get("/transcripts")
async def list_transcripts(request: Request, matter_id: str = Query(""),
                           session_id: Optional[int] = Query(None),
                           user=Depends(get_current_user)):
    """Transcripts for a matter (or session) — the viewer's picker/list."""
    tid = _tenant(request)
    where = ["TRIM(t.tenant_id) = TRIM(:tid)"]
    params = {"tid": tid}
    if matter_id:
        where.append("t.matter_id = CAST(:mid AS uuid)")
        params["mid"] = matter_id
    if session_id is not None:
        where.append("t.session_id = :sid")
        params["sid"] = session_id
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT t.id::text, t.deponent, t.session_id, t.source_format, "
                "       t.status, t.page_first, t.page_last, t.line_count, "
                "       t.qa_count, t.has_video, t.has_timecodes, "
                "       t.needs_conversion, t.imported_at "
                "FROM deposition_transcripts t WHERE " + " AND ".join(where) +
                " ORDER BY t.imported_at DESC"), params)
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse(_serialize(rows))
    except Exception as e:
        logger.exception("list_transcripts failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/transcripts/{transcript_id}")
async def get_transcript(request: Request, transcript_id: str,
                         user=Depends(get_current_user)):
    """Transcript meta + the page:line addressing layer, grouped by page."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT id::text, deponent, session_id, matter_id::text, "
                "       source_format, status, page_first, page_last, line_count, "
                "       qa_count, has_video, has_timecodes, needs_conversion, "
                "       video_status, (video_path LIKE '%.mp4') AS video_ready "
                "FROM deposition_transcripts "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"id": transcript_id, "tid": tid})
            meta = r.mappings().fetchone()
            if not meta:
                return JSONResponse({"error": "not found"}, 404)
            lr = await session.execute(sa_text(
                "SELECT page, line, text, qa_role, timecode_ms, char_start, char_end "
                "FROM transcript_lines WHERE transcript_id = CAST(:id AS uuid) "
                "ORDER BY page, line"), {"id": transcript_id})
            lines = [dict(x) for x in lr.mappings().fetchall()]
        pages = []
        cur_page, bucket = None, None
        for ln in lines:
            if ln["page"] != cur_page:
                cur_page = ln["page"]
                bucket = {"page": cur_page, "lines": []}
                pages.append(bucket)
            bucket["lines"].append(ln)
        return JSONResponse(_serialize({"transcript": dict(meta), "pages": pages}))
    except Exception as e:
        logger.exception("get_transcript failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/transcripts/{transcript_id}/qa")
async def get_qa_units(request: Request, transcript_id: str,
                       user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT q.id::text, q.seq, q.examiner, q.witness, q.q_start_page, "
                "       q.q_start_line, q.a_end_page, q.a_end_line, q.question_text, "
                "       q.answer_text, q.is_colloquy, q.char_start, q.char_end "
                "FROM transcript_qa_units q "
                "JOIN deposition_transcripts t ON t.id = q.transcript_id "
                "WHERE q.transcript_id = CAST(:id AS uuid) "
                "  AND TRIM(t.tenant_id) = TRIM(:tid) ORDER BY q.seq"),
                {"id": transcript_id, "tid": tid})
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse(_serialize(rows))
    except Exception as e:
        logger.exception("get_qa_units failed")
        return JSONResponse({"error": str(e)}, 500)


# --- semantic Q&A search ----------------------------------------------------

class SearchBody(BaseModel):
    query: str
    transcript_id: Optional[str] = None
    matter_id: Optional[str] = None
    limit: int = 20


def _embed_query_sync(text: str):
    from modules.depositions.jobs.embed_qa import _embed, EMBED_URL_DEFAULT, _vec_literal
    vecs, _m, _r = _embed(EMBED_URL_DEFAULT, [text])
    return _vec_literal(vecs[0])


@router.post("/search")
async def semantic_search(request: Request, body: SearchBody,
                          user=Depends(get_current_user)):
    """Semantic Q&A search over transcript_qa_embeddings (pgvector kNN in the
    same 768-d ModernBERT space). Scoped to one transcript or a whole matter.
    Best chunk per qa_unit; returns page:line-ready hits."""
    tid = _tenant(request)
    q = (body.query or "").strip()
    if not q:
        return JSONResponse({"results": []})
    try:
        # embed the query off the event loop (sync urllib to praesidium-embed)
        qv = await asyncio.get_event_loop().run_in_executor(None, _embed_query_sync, q)
        where = ["TRIM(e.tenant_id) = TRIM(:tid)"]
        params = {"tid": tid, "qv": qv, "lim": max(1, min(body.limit, 100))}
        if body.transcript_id:
            where.append("e.transcript_id = CAST(:trid AS uuid)")
            params["trid"] = body.transcript_id
        if body.matter_id:
            where.append("t.matter_id = CAST(:mid AS uuid)")
            params["mid"] = body.matter_id
        sql = (
            "WITH ranked AS ("
            "  SELECT e.qa_unit_id AS qa_id, "
            "         min(e.embedding <=> CAST(:qv AS vector)) AS dist "
            "  FROM transcript_qa_embeddings e "
            "  JOIN deposition_transcripts t ON t.id = e.transcript_id "
            "  WHERE " + " AND ".join(where) +
            "  GROUP BY e.qa_unit_id) "
            "SELECT q.id::text, q.transcript_id::text, q.seq, q.examiner, q.witness, "
            "       q.q_start_page, q.q_start_line, q.a_end_page, q.a_end_line, "
            "       q.question_text, q.answer_text, q.is_colloquy, t.deponent, "
            "       (1 - r.dist) AS sim "
            "FROM ranked r "
            "JOIN transcript_qa_units q ON q.id = r.qa_id "
            "JOIN deposition_transcripts t ON t.id = q.transcript_id "
            "ORDER BY r.dist LIMIT :lim")
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(sql), params)
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse(_serialize({"results": rows, "query": q}))
    except Exception as e:
        logger.exception("semantic_search failed")
        return JSONResponse({"error": str(e)}, 500)


# --- inline video stream (for the viewer's video pane) ----------------------

@router.get("/transcripts/{transcript_id}/video")
async def stream_video(request: Request, transcript_id: str,
                       user=Depends(get_current_user)):
    """Serve the transcript's source video for inline playback. FileResponse
    handles HTTP Range so the <video> element can seek/scrub."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT video_path FROM deposition_transcripts "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"id": transcript_id, "tid": tid})
            row = r.mappings().fetchone()
        if not row or not row["video_path"]:
            return JSONResponse({"error": "no video on record"}, 404)
        path = row["video_path"]
        if not os.path.exists(path):
            return JSONResponse({"error": "video file missing on disk"}, 404)
        import mimetypes
        media = mimetypes.guess_type(path)[0] or "video/mp4"
        return FileResponse(path, media_type=media)
    except Exception as e:
        logger.exception("stream_video failed")
        return JSONResponse({"error": str(e)}, 500)


# --- upload + register + ingest a transcript (homepage Ingest panel) --------

@router.post("/ingest-upload")
async def ingest_upload(request: Request, matter_id: str = Form(...),
                        file: UploadFile = File(...), deponent: str = Form(""),
                        user=Depends(get_current_user)):
    """Save an uploaded transcript under the matter tree, register it as a
    pending deposition transcript, and seed ingest (parse/segment/embed runs in
    the depo DAG drains). One-click for the homepage drop zone."""
    tid = _tenant(request)
    if not matter_id:
        return JSONResponse({"error": "matter_id required"}, 400)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT m.matter_name, c.client_name FROM matters m "
                "LEFT JOIN clients c ON c.id = m.client_id AND TRIM(c.tenant_id) = TRIM(:tid) "
                "WHERE m.id = CAST(:mid AS uuid) AND TRIM(m.tenant_id) = TRIM(:tid)"),
                {"tid": tid, "mid": matter_id})
            row = r.mappings().fetchone()
        if not row:
            return JSONResponse({"error": "matter not found"}, 404)

        base = os.path.join("/mnt/praesidium", tid.strip(), "matters",
                            row["client_name"] or "_", row["matter_name"] or "_",
                            "Depositions")
        os.makedirs(base, exist_ok=True)
        safe = os.path.basename(file.filename or "transcript")
        dest = os.path.join(base, safe)
        if os.path.exists(dest):
            stem, ext = os.path.splitext(safe)
            n = 2
            while os.path.exists(os.path.join(base, "%s (%d)%s" % (stem, n, ext))):
                n += 1
            dest = os.path.join(base, "%s (%d)%s" % (stem, n, ext))

        import shutil
        with open(dest, "wb") as out:
            shutil.copyfileobj(file.file, out)

        dep = (deponent or "").strip() or os.path.splitext(os.path.basename(dest))[0]

        def _register_and_seed():
            from modules.depositions.jobs.deposition_alerts import register_pending
            from modules.depositions.routes.alerts_api import _ingest_alert_sync
            from modules.depositions.jobs.depo_dag import enqueue_pipeline
            res = register_pending(tid.strip(), dest, matter_id, None, dep, False)
            alert_id = ((res or {}).get("alert") or {}).get("alert")
            if alert_id:
                res["ingest"] = _ingest_alert_sync(tid.strip(), alert_id)
            # fan the depo DAG out across the dedicated 'depositions' workers
            res["pipeline_job"] = enqueue_pipeline(tid.strip(), matter_id)
            return res

        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, _register_and_seed)
        return JSONResponse(_serialize({"ok": True, "file": os.path.basename(dest), "result": res}))
    except Exception as e:
        logger.exception("ingest_upload failed")
        return JSONResponse({"error": str(e)}, 500)


# --- landing feed (Transcripts module 6-panel home) -------------------------

@router.get("/landing")
async def landing(request: Request, matter_id: str = Query(""),
                  user=Depends(get_current_user)):
    """Aggregate feed for the Transcripts module landing page.

    Cross-matter (tenant-scoped) by default; pass matter_id to scope to one
    matter. Powers the 6-panel home: Processing status, My Recent Transcripts,
    New Depo Transcripts, New Hearing/Trial Transcripts. (Alerts come from the
    separate /alerts endpoint; the drop zone posts to /ingest-upload.)
    """
    tid = _tenant(request)
    base_where = ["TRIM(t.tenant_id) = TRIM(:tid)"]
    params = {"tid": tid}
    if matter_id:
        base_where.append("t.matter_id = CAST(:mid AS uuid)")
        params["mid"] = matter_id
    cols = (
        "t.id::text, t.deponent, t.title, t.matter_id::text AS matter_id, "
        "m.matter_name, m.matter_number, t.transcript_kind, t.source_format, "
        "t.status, t.qa_count, t.page_first, t.page_last, t.has_video, "
        "t.volume, t.trial_day, t.created_at, t.updated_at, t.imported_at"
    )
    frm = ("FROM deposition_transcripts t "
           "LEFT JOIN matters m ON m.id = t.matter_id")
    # statuses that mean the ingest pipeline has finished for the row
    DONE = "('segmented','synced','ready','complete','done')"
    try:
        async with AsyncSessionLocal() as session:
            async def q(extra, order, limit):
                where = base_where + (extra or [])
                sql = (f"SELECT {cols} {frm} WHERE " + " AND ".join(where) +
                       f" ORDER BY {order} LIMIT {limit}")
                r = await session.execute(sa_text(sql), params)
                return [dict(x) for x in r.mappings().fetchall()]

            processing = await q(
                [f"COALESCE(t.status,'') NOT IN {DONE}"],
                "COALESCE(t.updated_at, t.imported_at, t.created_at) DESC", 12)
            # My Recent — per-user, ordered by last viewed (migration 0111)
            uid = getattr(user, "id", None)
            if uid is not None:
                rparams = {"tid": tid, "uid": uid}
                rsql = (
                    f"SELECT {cols}, v.viewed_at FROM transcript_views v "
                    "JOIN deposition_transcripts t ON t.id = v.transcript_id "
                    "LEFT JOIN matters m ON m.id = t.matter_id "
                    "WHERE TRIM(v.tenant_id) = TRIM(:tid) AND v.user_id = :uid")
                if matter_id:
                    rsql += " AND t.matter_id = CAST(:mid AS uuid)"
                    rparams["mid"] = matter_id
                rsql += " ORDER BY v.viewed_at DESC LIMIT 8"
                rr = await session.execute(sa_text(rsql), rparams)
                recent = [dict(x) for x in rr.mappings().fetchall()]
            else:
                recent = []
            new_depo = await q(
                ["t.transcript_kind = 'deposition'"],
                "COALESCE(t.imported_at, t.created_at) DESC", 8)
            new_trial = await q(
                ["t.transcript_kind IN ('trial','hearing')"],
                "COALESCE(t.imported_at, t.created_at) DESC", 8)
        return JSONResponse(_serialize({
            "processing": processing,
            "recent": recent,
            "new_depo": new_depo,
            "new_trial": new_trial,
        }))
    except Exception as e:
        logger.exception("landing failed")
        return JSONResponse({"error": str(e)}, 500)
