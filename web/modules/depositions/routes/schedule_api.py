"""
modules/depositions/routes/schedule_api.py
Deposition scheduling primitive + homepage summary.

Backs the Depositions homepage Schedule panel and the lifecycle bridge:
    depo_schedule (planned) -> viaticum_sessions (conducted) -> deposition_transcripts (ingested)

Phase 1 = CRUD over depo_schedule. The Phase 2 proposal engine (scope-and-hold)
writes candidate slots into proposed_dates with status='proposing' — no schema change.

Mirrors depo_api.py conventions: AsyncSessionLocal + sa_text, tenant from
request.state (TRIM both sides), get_current_user, _serialize.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/depositions", tags=["depositions-schedule-api"])

_STATUSES = {"requested", "proposing", "noticed", "confirmed",
             "rescheduled", "completed", "cancelled"}

# columns a client may set on create/update (everything except identity/audit cols)
_WRITABLE = [
    "deponent", "depo_role", "status", "scheduled_start", "scheduled_end",
    "duration_est_minutes", "location", "is_remote", "remote_url",
    "noticing_party", "defending_party", "court_reporter", "videographer",
    "discovery_cutoff", "linked_session_id", "linked_transcript_id", "notes",
]


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


_SELECT = (
    "SELECT id::text, tenant_id, matter_id::text, deponent, depo_role, status, "
    "proposed_dates, scheduled_start, scheduled_end, duration_est_minutes, location, "
    "is_remote, remote_url, noticing_party, defending_party, court_reporter, videographer, "
    "discovery_cutoff, linked_session_id, linked_transcript_id::text, notes, "
    "created_by, created_at, updated_at FROM depo_schedule"
)


# --- schedule CRUD ----------------------------------------------------------

@router.get("/schedule")
async def list_schedule(request: Request, matter_id: str = Query(""),
                        status: Optional[str] = Query(None),
                        user=Depends(get_current_user)):
    """Scheduled depositions for a matter — the homepage Schedule panel."""
    tid = _tenant(request)
    where = ["TRIM(tenant_id) = TRIM(:tid)"]
    params = {"tid": tid}
    if matter_id:
        where.append("matter_id = CAST(:mid AS uuid)")
        params["mid"] = matter_id
    if status:
        where.append("status = :status")
        params["status"] = status
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                _SELECT + " WHERE " + " AND ".join(where) +
                " ORDER BY COALESCE(scheduled_start, created_at) ASC"), params)
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse(_serialize(rows))
    except Exception as e:
        logger.exception("list_schedule failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/schedule")
async def create_schedule(request: Request, user=Depends(get_current_user)):
    tid = _tenant(request)
    body = await request.json()
    matter_id = (body.get("matter_id") or "").strip()
    if not matter_id:
        return JSONResponse({"error": "matter_id required"}, 400)
    status = (body.get("status") or "requested").strip()
    if status not in _STATUSES:
        return JSONResponse({"error": f"invalid status '{status}'",
                             "valid": sorted(_STATUSES)}, 400)
    uid = getattr(user, "id", None)

    cols = ["tenant_id", "matter_id", "status", "created_by"]
    vals = ["TRIM(:tid)", "CAST(:mid AS uuid)", ":status", ":uid"]
    params = {"tid": tid, "mid": matter_id, "status": status, "uid": uid}
    for c in _WRITABLE:
        if c == "status" or c not in body:
            continue
        if c == "linked_transcript_id" and body.get(c):
            cols.append(c); vals.append("CAST(:%s AS uuid)" % c)
        else:
            cols.append(c); vals.append(":" + c)
        params[c] = body.get(c)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "INSERT INTO depo_schedule (" + ", ".join(cols) + ") VALUES (" +
                ", ".join(vals) + ") RETURNING id::text"), params)
            new_id = r.scalar()
            await session.commit()
        return JSONResponse({"id": new_id, "status": status}, 201)
    except Exception as e:
        logger.exception("create_schedule failed")
        return JSONResponse({"error": str(e)}, 500)


@router.patch("/schedule/{schedule_id}")
async def update_schedule(request: Request, schedule_id: str,
                          user=Depends(get_current_user)):
    tid = _tenant(request)
    body = await request.json()
    sets = ["updated_at = now()"]
    params = {"tid": tid, "sid": schedule_id}
    for c in _WRITABLE:
        if c not in body:
            continue
        if c == "status" and body[c] not in _STATUSES:
            return JSONResponse({"error": f"invalid status '{body[c]}'",
                                 "valid": sorted(_STATUSES)}, 400)
        if c == "linked_transcript_id" and body.get(c):
            sets.append("%s = CAST(:%s AS uuid)" % (c, c))
        else:
            sets.append("%s = :%s" % (c, c))
        params[c] = body.get(c)
    if len(sets) == 1:
        return JSONResponse({"error": "no writable fields supplied"}, 400)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "UPDATE depo_schedule SET " + ", ".join(sets) +
                " WHERE id = CAST(:sid AS uuid) AND TRIM(tenant_id) = TRIM(:tid) "
                "RETURNING id::text"), params)
            updated = r.scalar()
            await session.commit()
        if not updated:
            return JSONResponse({"error": "not found"}, 404)
        return JSONResponse({"id": updated})
    except Exception as e:
        logger.exception("update_schedule failed")
        return JSONResponse({"error": str(e)}, 500)


@router.delete("/schedule/{schedule_id}")
async def delete_schedule(request: Request, schedule_id: str,
                          user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "DELETE FROM depo_schedule WHERE id = CAST(:sid AS uuid) "
                "AND TRIM(tenant_id) = TRIM(:tid) RETURNING id::text"),
                {"sid": schedule_id, "tid": tid})
            deleted = r.scalar()
            await session.commit()
        if not deleted:
            return JSONResponse({"error": "not found"}, 404)
        return JSONResponse({"deleted": deleted})
    except Exception as e:
        logger.exception("delete_schedule failed")
        return JSONResponse({"error": str(e)}, 500)


# --- homepage summary -------------------------------------------------------

@router.get("/home")
async def home_summary(request: Request, matter_id: str = Query(""),
                       user=Depends(get_current_user)):
    """One call backing the Depositions homepage: schedule + transcripts +
    derived deponents (witness panel) + prep-task count. Alerts panel uses the
    existing /alerts endpoint; ingest is a UI modal."""
    tid = _tenant(request)
    if not matter_id:
        return JSONResponse({"error": "matter_id required"}, 400)
    try:
        async with AsyncSessionLocal() as session:
            sched_r = await session.execute(sa_text(
                _SELECT + " WHERE TRIM(tenant_id) = TRIM(:tid) "
                "AND matter_id = CAST(:mid AS uuid) "
                "ORDER BY COALESCE(scheduled_start, created_at) ASC"),
                {"tid": tid, "mid": matter_id})
            schedule = [dict(x) for x in sched_r.mappings().fetchall()]

            tx_r = await session.execute(sa_text(
                "SELECT id::text, deponent, status, source_format, qa_count, "
                "has_video, page_first, page_last, session_id, imported_at "
                "FROM deposition_transcripts "
                "WHERE TRIM(tenant_id) = TRIM(:tid) AND matter_id = CAST(:mid AS uuid) "
                "ORDER BY imported_at DESC"), {"tid": tid, "mid": matter_id})
            transcripts = [dict(x) for x in tx_r.mappings().fetchall()]

            task_r = await session.execute(sa_text(
                "SELECT COUNT(*) c FROM tasks WHERE TRIM(tenant_id) = TRIM(:tid) "
                "AND matter_id = CAST(:mid AS uuid) AND status != 'deleted' "
                "AND task_type = 'deposition_prep'"), {"tid": tid, "mid": matter_id})
            prep_task_count = task_r.scalar() or 0

        # Derived witness/deponent roster: union of scheduled deponents + ingested
        # transcript deponents. The deposition's witnesses ARE its deponents.
        roster = {}
        for s in schedule:
            name = (s.get("deponent") or "").strip()
            if not name:
                continue
            roster.setdefault(name, {"deponent": name, "depo_role": s.get("depo_role"),
                                     "scheduled": False, "ingested": False})
            roster[name]["scheduled"] = True
            roster[name]["schedule_status"] = s.get("status")
        for t in transcripts:
            name = (t.get("deponent") or "").strip()
            if not name:
                continue
            roster.setdefault(name, {"deponent": name, "depo_role": None,
                                     "scheduled": False, "ingested": False})
            roster[name]["ingested"] = True
            roster[name]["transcript_id"] = t["id"]

        return JSONResponse(_serialize({
            "matter_id": matter_id,
            "schedule": schedule,
            "transcripts": transcripts,
            "deponents": list(roster.values()),
            "prep_task_count": prep_task_count,
            "counts": {
                "scheduled": len(schedule),
                "transcripts": len(transcripts),
                "witnesses": len(roster),
                "prep_tasks": prep_task_count,
            },
        }))
    except Exception as e:
        logger.exception("home_summary failed")
        return JSONResponse({"error": str(e)}, 500)


# --- global landing: matters with deposition activity ----------------------

@router.get("/matters")
async def deposition_matters(request: Request, user=Depends(get_current_user)):
    """Matters that have any deposition activity (scheduled or ingested) — backs
    the top-level Depositions module landing, which drills into /depositions/home/{id}."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT m.id::text AS matter_id, m.matter_name, m.matter_number,
                       cl.client_name,
                       COALESCE(tx.n, 0) AS transcript_count,
                       COALESCE(sc.n, 0) AS schedule_count,
                       COALESCE(sc.upcoming, 0) AS upcoming_count,
                       GREATEST(COALESCE(tx.last_at, '1970-01-01'::timestamptz),
                                COALESCE(sc.last_at, '1970-01-01'::timestamptz)) AS last_activity
                FROM matters m
                LEFT JOIN clients cl ON cl.id = m.client_id AND TRIM(cl.tenant_id) = TRIM(:tid)
                LEFT JOIN (
                    SELECT matter_id, count(*) n, max(imported_at) last_at
                    FROM deposition_transcripts WHERE TRIM(tenant_id) = TRIM(:tid)
                    GROUP BY matter_id
                ) tx ON tx.matter_id = m.id
                LEFT JOIN (
                    SELECT matter_id, count(*) n, max(created_at) last_at,
                           count(*) FILTER (WHERE status NOT IN ('completed','cancelled')) upcoming
                    FROM depo_schedule WHERE TRIM(tenant_id) = TRIM(:tid)
                    GROUP BY matter_id
                ) sc ON sc.matter_id = m.id
                WHERE TRIM(m.tenant_id) = TRIM(:tid)
                  AND (tx.n IS NOT NULL OR sc.n IS NOT NULL)
                ORDER BY last_activity DESC, m.matter_name
            """), {"tid": tid})
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse(_serialize(rows))
    except Exception as e:
        logger.exception("deposition_matters failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/matters/search")
async def deposition_matters_search(request: Request, q: str = Query(""),
                                    user=Depends(get_current_user)):
    """Matter picker for starting a fresh deposition workflow on any matter.
    Returns {matters:[{id,number,name,client}]}."""
    tid = _tenant(request)
    # TODO (later): scope to litigation only — add `AND m.matter_type = 'litigation'`
    # (or practice_area) here once matter typing is confirmed/backfilled.
    litigation_only = ""
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(f"""
                SELECT m.id::text AS id, m.matter_number, m.matter_name, c.client_name
                FROM matters m
                LEFT JOIN clients c ON c.id = m.client_id AND TRIM(c.tenant_id) = TRIM(:tid)
                WHERE TRIM(m.tenant_id) = TRIM(:tid)
                  AND (m.matter_name ILIKE :q OR m.matter_number ILIKE :q
                       OR c.client_name ILIKE :q)
                  {litigation_only}
                ORDER BY m.matter_name LIMIT 20
            """), {"tid": tid, "q": f"%{q}%"})
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse({"matters": [
            {"id": x["id"], "number": x["matter_number"],
             "name": x["matter_name"], "client": x["client_name"]} for x in rows]})
    except Exception as e:
        logger.exception("deposition_matters_search failed")
        return JSONResponse({"error": str(e)}, 500)
