"""
modules/depositions/routes/alerts_api.py
Deposition ingestion alerts (§7): list active alerts, the "Ingest" action
(seeds the ingest ledger for the matter's pending transcripts, creating a
viaticum_sessions parent when none matches), and dismiss. The generator is
modules/depositions/jobs/deposition_alerts.py (deterministic, zero-LLM).
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant, _serialize
from modules.depositions.jobs.deposition_alerts import ALERT_TYPE

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/depositions", tags=["depositions-alerts-api"])


@router.get("/alerts")
async def list_alerts(request: Request, matter_id: str = Query(""),
                      user=Depends(get_current_user)):
    tid = _tenant(request)
    where = ["TRIM(tenant_id) = TRIM(:tid)", "alert_type = :atype", "status = 'active'"]
    params = {"tid": tid, "atype": ALERT_TYPE}
    if matter_id:
        where.append("matter_id = CAST(:mid AS uuid)")
        params["mid"] = matter_id
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT id::text, matter_id::text, alert_type, count, message, "
                "       status, payload, created_at "
                "FROM onboarding_alerts WHERE " + " AND ".join(where) +
                " ORDER BY created_at DESC"), params)
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse(_serialize(rows))
    except Exception as e:
        logger.exception("list_alerts failed")
        return JSONResponse({"error": str(e)}, 500)


def _ingest_alert_sync(tenant, alert_id):
    """Seed ingest for every pending transcript in the alert; create a depo
    session for any transcript that has none; resolve the alert; recompute."""
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    from modules.depositions.jobs.depo_dag import _seed, STAGE_INGEST
    from modules.depositions.jobs.deposition_alerts import recompute_deposition_alerts
    ten = (tenant or "").strip()
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute("SELECT matter_id::text FROM onboarding_alerts "
                    "WHERE id = CAST(%s AS uuid) AND TRIM(tenant_id) = %s "
                    "AND alert_type = %s", (alert_id, ten, ALERT_TYPE))
        row = cur.fetchone()
        if not row:
            return {"error": "alert not found"}
        matter_id = row[0]
        cur.execute(
            "SELECT id::text, session_id, deponent FROM deposition_transcripts "
            "WHERE TRIM(tenant_id) = %s AND matter_id = CAST(%s AS uuid) "
            "  AND status = 'pending'", (ten, matter_id))
        pend = cur.fetchall()
        seeded = 0
        for tid, sid, deponent in pend:
            if sid is None:
                # no session match -> create a deposition session parent
                cur.execute(
                    "INSERT INTO viaticum_sessions "
                    "  (tenant_id, matter_id, session_name, session_type, status) "
                    "VALUES (%s, CAST(%s AS uuid), %s, 'deposition', 'active') "
                    "RETURNING id",
                    (ten, matter_id, (deponent or "Deposition") + " transcript"))
                sid = cur.fetchone()[0]
                cur.execute("UPDATE deposition_transcripts SET session_id = %s, "
                            "updated_at = now() WHERE id = CAST(%s AS uuid)", (sid, tid))
            _seed(cur, ten, tid, sid, matter_id, STAGE_INGEST)
            seeded += 1
        # mark alert resolved, then recompute (which will clear it now that the
        # transcripts are no longer 'pending' once ingest flips their status;
        # ingest sets status='ingested' on success).
        cur.execute("UPDATE onboarding_alerts SET status = 'resolved', "
                    "reviewed_at = now() WHERE id = CAST(%s AS uuid)", (alert_id,))
        conn.commit()
        return {"seeded": seeded, "matter_id": matter_id}
    finally:
        conn.close()


@router.post("/alerts/{alert_id}/ingest")
async def ingest_alert(request: Request, alert_id: str,
                       user=Depends(get_current_user)):
    """Action: seed ingest for the alert's pending transcripts. Heavy parse/
    segment/embed then runs in the depo DAG drains."""
    tid = _tenant(request)
    try:
        out = await asyncio.get_event_loop().run_in_executor(
            None, _ingest_alert_sync, tid, alert_id)
        # fan the depo DAG out across the dedicated 'depositions' workers
        if not out.get("error") and out.get("matter_id"):
            try:
                from modules.depositions.jobs.depo_dag import enqueue_pipeline
                out["pipeline_job"] = enqueue_pipeline(tid.strip(), out["matter_id"])
            except Exception as e:
                logger.warning("enqueue_pipeline after alert ingest failed: %s", e)
        code = 404 if out.get("error") else 200
        return JSONResponse(out, code)
    except Exception as e:
        logger.exception("ingest_alert failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/alerts/{alert_id}/dismiss")
async def dismiss_alert(request: Request, alert_id: str,
                        user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(
                "UPDATE onboarding_alerts SET status = 'dismissed', reviewed_at = now() "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid) "
                "AND alert_type = :atype"),
                {"id": alert_id, "tid": tid, "atype": ALERT_TYPE})
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.exception("dismiss_alert failed")
        return JSONResponse({"error": str(e)}, 500)
