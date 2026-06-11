"""
modules/ediscovery/routes/wiam_api.py

WIAM JSON API for React frontend.
Replaces the HTMX/template routes in intelligence_layer.py for WIAM.

Endpoints:
  GET  /api/v1/wiam/matters/{id}/sessions      — list sessions + finding counts
  GET  /api/v1/wiam/matters/{id}/sessions/{sid} — session detail + all findings
  POST /api/v1/wiam/matters/{id}/sessions       — create session + enqueue engine
  POST /api/v1/wiam/matters/{id}/sessions/{sid}/findings/{fid}/dispose
  GET  /api/v1/wiam/matters/{id}/summary        — aggregate stats

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/wiam", tags=["wiam-api"])


def _tid(request: Request) -> str:
    tid = getattr(request.state, 'tenant_id', None)
    if not tid:
        raise HTTPException(401, "No tenant context")
    return tid.strip()


def _uid(request: Request) -> int:
    uid = getattr(request.state, 'user_id', None)
    if not uid:
        raise HTTPException(401, "Not authenticated")
    return int(uid)


# ================================================================== #
# GET /api/v1/wiam/matters/{matter_id}/sessions                      #
# ================================================================== #

@router.get("/matters/{matter_id}/sessions")
async def list_wiam_sessions(request: Request, matter_id: str):
    """List WIAM sessions for a matter with finding counts per dimension."""
    tenant_id = _tid(request)

    async with AsyncSessionLocal() as session:
        # Verify matter access
        row = await session.execute(
            text("SELECT id FROM matters WHERE id = :mid AND TRIM(tenant_id) = :tid"),
            {"mid": matter_id, "tid": tenant_id}
        )
        if not row.first():
            raise HTTPException(404, "Matter not found")

        # Sessions with aggregated finding counts
        rows = await session.execute(
            text("""
                SELECT s.id, s.triggered_by, s.status, s.created_at,
                       s.completed_at, s.total_findings,
                       s.dimensions_requested, s.error_log,
                       s.created_by,
                       COALESCE(
                         (SELECT json_object_agg(dimension, cnt)
                          FROM (
                            SELECT dimension, count(*) as cnt
                            FROM wiam_findings
                            WHERE session_id = s.id
                            GROUP BY dimension
                          ) sub), '{}'::json
                       ) AS dim_counts,
                       COALESCE(
                         (SELECT count(*) FROM wiam_findings
                          WHERE session_id = s.id
                          AND priority IN ('critical', 'high')
                          AND disposition IS NULL), 0
                       ) AS open_critical
                FROM wiam_sessions s
                WHERE s.matter_id = :mid AND TRIM(s.tenant_id) = :tid
                ORDER BY s.created_at DESC
                LIMIT 50
            """),
            {"mid": matter_id, "tid": tenant_id}
        )
        sessions = []
        for r in rows.mappings():
            d = dict(r)
            # Ensure JSON fields are dicts
            if isinstance(d.get("dim_counts"), str):
                try: d["dim_counts"] = json.loads(d["dim_counts"])
                except: d["dim_counts"] = {}
            if isinstance(d.get("dimensions_requested"), str):
                try: d["dimensions_requested"] = json.loads(d["dimensions_requested"])
                except: d["dimensions_requested"] = []
            # Serialize timestamps
            for k in ("created_at", "completed_at"):
                if d.get(k):
                    d[k] = d[k].isoformat()
            sessions.append(d)

    return JSONResponse({"matter_id": matter_id, "sessions": sessions})


# ================================================================== #
# GET /api/v1/wiam/matters/{matter_id}/sessions/{session_id}         #
# ================================================================== #

@router.get("/matters/{matter_id}/sessions/{session_id}")
async def get_wiam_session_detail(request: Request, matter_id: str, session_id: str):
    """Session detail with all findings grouped by dimension."""
    tenant_id = _tid(request)

    async with AsyncSessionLocal() as session:
        # Session
        sess_row = await session.execute(
            text("""
                SELECT id, triggered_by, status, created_at, completed_at,
                       total_findings, dimensions_requested, error_log,
                       context_summary, created_by
                FROM wiam_sessions
                WHERE id = :sid AND matter_id = :mid AND TRIM(tenant_id) = :tid
            """),
            {"sid": session_id, "mid": matter_id, "tid": tenant_id}
        )
        sess = sess_row.mappings().first()
        if not sess:
            raise HTTPException(404, "Session not found")
        sess_dict = dict(sess)
        for k in ("created_at", "completed_at"):
            if sess_dict.get(k):
                sess_dict[k] = sess_dict[k].isoformat()
        if isinstance(sess_dict.get("dimensions_requested"), str):
            try: sess_dict["dimensions_requested"] = json.loads(sess_dict["dimensions_requested"])
            except: pass

        # Findings
        find_rows = await session.execute(
            text("""
                SELECT id, finding_type, dimension, claim_element,
                       description, citations, confidence, priority,
                       suggested_action, disposition, disposed_by,
                       disposed_at, created_at
                FROM wiam_findings
                WHERE session_id = :sid AND TRIM(tenant_id) = :tid
                ORDER BY
                    CASE priority
                        WHEN 'critical' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        ELSE 3
                    END,
                    confidence DESC,
                    created_at
            """),
            {"sid": session_id, "tid": tenant_id}
        )
        findings = []
        for r in find_rows.mappings():
            f = dict(r)
            if isinstance(f.get("citations"), str):
                try: f["citations"] = json.loads(f["citations"])
                except: f["citations"] = []
            for k in ("created_at", "disposed_at"):
                if f.get(k):
                    f[k] = f[k].isoformat()
            findings.append(f)

        # Group by dimension
        by_dimension = {}
        for f in findings:
            dim = f.get("dimension", "your_case")
            by_dimension.setdefault(dim, []).append(f)

    return JSONResponse({
        "session": sess_dict,
        "findings": findings,
        "by_dimension": by_dimension,
        "stats": {
            "total": len(findings),
            "critical": sum(1 for f in findings if f["priority"] == "critical"),
            "high": sum(1 for f in findings if f["priority"] == "high"),
            "open": sum(1 for f in findings if not f.get("disposition")),
            "accepted": sum(1 for f in findings if f.get("disposition") == "accepted"),
            "dismissed": sum(1 for f in findings if f.get("disposition") == "dismissed"),
        },
    })


# ================================================================== #
# POST /api/v1/wiam/matters/{matter_id}/sessions — create + enqueue  #
# ================================================================== #

@router.post("/matters/{matter_id}/sessions")
async def create_wiam_session(request: Request, matter_id: str):
    """
    Create a new WIAM session and enqueue the engine job.
    
    Body (optional):
        triggered_by: manual | pre_filing | post_depo | scheduled
        dimensions: ["opp_gap", "own_gap", "external_gap", "drift_gap"]
    """
    tenant_id = _tid(request)
    user_id = _uid(request)

    # Verify matter
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            text("SELECT id, matter_name FROM matters WHERE id = :mid AND TRIM(tenant_id) = :tid"),
            {"mid": matter_id, "tid": tenant_id}
        )
        matter = row.mappings().first()
        if not matter:
            raise HTTPException(404, "Matter not found")

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    triggered_by = body.get("triggered_by", "manual")
    if triggered_by not in {"manual", "pre_filing", "post_depo", "scheduled"}:
        triggered_by = "manual"

    dimensions = body.get("dimensions")
    if not dimensions or not isinstance(dimensions, list):
        dimensions = ["opp_gap", "own_gap", "external_gap", "drift_gap"]

    session_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO wiam_sessions
                    (id, matter_id, tenant_id, triggered_by, status,
                     created_by, dimensions_requested)
                VALUES
                    (:id, :mid, :tid, :trig, 'running', :uid,
                     CAST(:dims AS jsonb))
            """),
            {
                "id": session_id,
                "mid": matter_id,
                "tid": tenant_id,
                "trig": triggered_by,
                "uid": user_id,
                "dims": json.dumps(dimensions),
            }
        )
        await session.commit()

    # Enqueue RQ job
    job_id = None
    try:
        import redis
        from rq import Queue
        import os
        redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
        r = redis.Redis.from_url(redis_url)
        q = Queue("ediscovery", connection=r)
        job = q.enqueue(
            "jobs.wiam_engine.run",
            session_id=session_id,
            matter_id=matter_id,
            tenant_id=tenant_id,
            triggered_by=triggered_by,
            dimensions=dimensions,
            job_timeout=900,
        )
        job_id = job.id
        log.info("wiam_api: enqueued job %s for session %s", job_id, session_id)
    except Exception as exc:
        log.error("wiam_api: failed to enqueue engine job: %s", exc)
        # Session exists with status='running' — engine didn't start
        # Mark it as failed so UI doesn't show perpetual spinner
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    UPDATE wiam_sessions
                    SET status = 'failed', error_log = :err,
                        completed_at = NOW()
                    WHERE id = :sid
                """),
                {"sid": session_id, "err": f"Failed to enqueue: {exc}"}
            )
            await session.commit()

    return JSONResponse({
        "session_id": session_id,
        "status": "running",
        "job_id": job_id,
        "matter_id": matter_id,
        "triggered_by": triggered_by,
        "dimensions": dimensions,
    })


# ================================================================== #
# POST .../findings/{finding_id}/dispose                             #
# ================================================================== #

@router.post("/matters/{matter_id}/sessions/{session_id}/findings/{finding_id}/dispose")
async def dispose_wiam_finding(
    request: Request, matter_id: str, session_id: str, finding_id: str
):
    """Accept or dismiss a WIAM finding. Immutable — cannot re-dispose."""
    tenant_id = _tid(request)
    user_id = _uid(request)

    body = await request.json()
    disposition = body.get("disposition")
    if disposition not in ("accepted", "dismissed"):
        raise HTTPException(422, "disposition must be 'accepted' or 'dismissed'")

    async with AsyncSessionLocal() as session:
        row = await session.execute(
            text("""
                SELECT disposition FROM wiam_findings
                WHERE id = :fid AND session_id = :sid AND TRIM(tenant_id) = :tid
            """),
            {"fid": finding_id, "sid": session_id, "tid": tenant_id}
        )
        finding = row.mappings().first()
        if not finding:
            raise HTTPException(404, "Finding not found")
        if finding["disposition"] is not None:
            raise HTTPException(409, "Finding already disposed")

        await session.execute(
            text("""
                UPDATE wiam_findings
                SET disposition = :disp, disposed_by = :uid, disposed_at = NOW()
                WHERE id = :fid
            """),
            {"disp": disposition, "uid": user_id, "fid": finding_id}
        )
        await session.commit()

    return JSONResponse({
        "finding_id": finding_id,
        "disposition": disposition,
    })


# ================================================================== #
# GET /api/v1/wiam/matters/{matter_id}/summary                      #
# ================================================================== #

@router.get("/matters/{matter_id}/summary")
async def wiam_matter_summary(request: Request, matter_id: str):
    """
    Aggregate WIAM stats for a matter:
    - Total sessions, latest session status
    - Open findings by dimension and priority
    - Trend (findings over time)
    """
    tenant_id = _tid(request)

    async with AsyncSessionLocal() as session:
        # Latest session
        latest = await session.execute(
            text("""
                SELECT id, status, created_at, completed_at, total_findings
                FROM wiam_sessions
                WHERE matter_id = :mid AND TRIM(tenant_id) = :tid
                ORDER BY created_at DESC LIMIT 1
            """),
            {"mid": matter_id, "tid": tenant_id}
        )
        latest_row = latest.mappings().first()

        # Session count
        count_row = await session.execute(
            text("""
                SELECT count(*) as total_sessions
                FROM wiam_sessions
                WHERE matter_id = :mid AND TRIM(tenant_id) = :tid
            """),
            {"mid": matter_id, "tid": tenant_id}
        )
        total_sessions = count_row.scalar() or 0

        # Open findings by dimension
        dim_rows = await session.execute(
            text("""
                SELECT dimension,
                       count(*) as total,
                       count(*) FILTER (WHERE priority IN ('critical', 'high')) as urgent,
                       count(*) FILTER (WHERE disposition IS NULL) as open
                FROM wiam_findings
                WHERE matter_id = :mid AND TRIM(tenant_id) = :tid
                GROUP BY dimension
            """),
            {"mid": matter_id, "tid": tenant_id}
        )
        by_dimension = {}
        for r in dim_rows.mappings():
            by_dimension[r["dimension"] or "unknown"] = {
                "total": r["total"],
                "urgent": r["urgent"],
                "open": r["open"],
            }

        # Global open count
        open_row = await session.execute(
            text("""
                SELECT count(*) as open_findings
                FROM wiam_findings
                WHERE matter_id = :mid AND TRIM(tenant_id) = :tid
                  AND disposition IS NULL
            """),
            {"mid": matter_id, "tid": tenant_id}
        )
        open_findings = open_row.scalar() or 0

    latest_dict = None
    if latest_row:
        latest_dict = dict(latest_row)
        for k in ("created_at", "completed_at"):
            if latest_dict.get(k):
                latest_dict[k] = latest_dict[k].isoformat()

    return JSONResponse({
        "matter_id": matter_id,
        "total_sessions": total_sessions,
        "latest_session": latest_dict,
        "open_findings": open_findings,
        "by_dimension": by_dimension,
    })
