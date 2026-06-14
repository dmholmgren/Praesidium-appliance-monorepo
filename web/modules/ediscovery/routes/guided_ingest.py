"""
modules/ediscovery/routes/guided_ingest.py

Guided-ingestion API (observe -> propose -> confirm -> execute -> account).

  POST /api/v1/ediscovery/guided/propose
      { matter_id, paths: [..server paths..], origin? }
      -> creates a pending collection_proposals row, enqueues the propose job,
         returns { proposal_id, status: "pending" }. Poll the GET below.

  GET  /api/v1/ediscovery/guided/proposal/{id}
      -> { id, status, inventory, proposal }  (status: pending|ready|error|
         confirmed|executed)

  POST /api/v1/ediscovery/guided/confirm/{id}
      { proposal: {..edited contract..} }
      -> creates one collection per confirmed unit and enqueues run_collection_full
         per unit (client-files first, eDiscovery second). Returns created ids.

This is the C3 guided session: the same flow the onboarding *_PLAN_NEEDED
alerts' "execute" button launches, and the flow drag-drop / picker enters.
"""
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ediscovery/guided", tags=["ediscovery-guided"])


def _uid(user):
    return user.get("id") if isinstance(user, dict) else getattr(user, "id", None)


def _enqueue_propose(proposal_id, tenant_id, matter_id, paths, user_id):
    import os
    import redis as redis_lib
    from rq import Queue
    redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
    q = Queue("ediscovery", connection=redis_lib.Redis.from_url(redis_url))
    q.enqueue(
        "modules.ediscovery.jobs.guided_propose.run_proposal",
        proposal_id, tenant_id, matter_id, paths, user_id,
        job_timeout="6h", result_ttl=3600,
    )


@router.post("/propose")
async def propose(request: Request, user=Depends(get_current_user)):
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user_id = _uid(user)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)

    matter_id = (body.get("matter_id") or "").strip()
    paths = body.get("paths") or []
    origin = body.get("origin") or "picker"
    if not matter_id or not paths:
        return JSONResponse({"error": "matter_id and paths required"},
                            status_code=400)

    async with AsyncSessionLocal() as s:
        row = (await s.execute(text("""
            INSERT INTO collection_proposals
                (tenant_id, matter_id, source_paths, status, origin, created_by)
            VALUES
                (:tid, CAST(:mid AS uuid), CAST(:paths AS jsonb), 'pending',
                 :origin, CAST(:uid AS uuid))
            RETURNING id::text
        """), {"tid": tenant_id, "mid": matter_id,
               "paths": __import__("json").dumps(paths), "origin": origin,
               "uid": str(user_id) if user_id else None})).first()
        proposal_id = row[0]
        await s.commit()

    _enqueue_propose(proposal_id, tenant_id, matter_id, paths, user_id)
    return JSONResponse({"proposal_id": proposal_id, "status": "pending"})


@router.get("/proposal/{proposal_id}")
async def get_proposal(proposal_id: str, request: Request,
                       user=Depends(get_current_user)):
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    async with AsyncSessionLocal() as s:
        row = (await s.execute(text("""
            SELECT id::text, matter_id::text, status, source_paths,
                   inventory, proposal, origin, created_at
              FROM collection_proposals
             WHERE id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"pid": proposal_id, "tid": tenant_id})).mappings().first()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    d = dict(row)
    d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
    return JSONResponse(d)


@router.post("/confirm/{proposal_id}")
async def confirm(proposal_id: str, request: Request,
                  user=Depends(get_current_user)):
    from modules.ediscovery.guided_ingest.pipeline import confirm_proposal
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user_id = _uid(user)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)

    async with AsyncSessionLocal() as s:
        row = (await s.execute(text("""
            SELECT matter_id::text, status FROM collection_proposals
             WHERE id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"pid": proposal_id, "tid": tenant_id})).first()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    if row[1] == "executed":
        return JSONResponse({"error": "already executed"}, status_code=409)

    matter_id = row[0]
    edited = body.get("proposal") or {}
    edited["matter_id"] = matter_id   # so pipeline.matter_id_of resolves
    try:
        created = await confirm_proposal(tenant_id, proposal_id, edited, user_id)
    except Exception as e:
        logger.exception("guided confirm %s failed", proposal_id)
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"proposal_id": proposal_id, "status": "executed",
                         "created": created})
