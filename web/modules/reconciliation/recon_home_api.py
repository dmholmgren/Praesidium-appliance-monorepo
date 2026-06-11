"""
modules/reconciliation/recon_home_api.py
JSON API for the Reconciliation home dashboard.
GET /api/v1/reconciliation/home -> summary of all rec types
"""
from __future__ import annotations
import logging
from decimal import Decimal
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/reconciliation", tags=["reconciliation-api"])

def _tid(r):
    return (getattr(r.state, "tenant_id", "") or "").strip()

def _uid(r):
    user = getattr(r.state, "current_user", None)
    return getattr(user, "id", 0) if user else 0

@router.get("/home")
async def recon_home_api(request: Request):
    tid = _tid(request)
    uid = _uid(request)
    result = {
        "timesheet": {"total_sessions": 0, "recent": [], "running": 0, "pending_drafts": 0},
        "email": {"status": "live", "total": 0, "pending": 0, "matched": 0, "filed": 0},
        "expense": {"status": "coming_soon"},
        "trust": {"status": "coming_soon"},
    }
    async with AsyncSessionLocal() as db:
        sess_row = await db.execute(sa_text("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running
            FROM timesheet_sessions
            WHERE trim(tenant_id) = trim(:tid) AND user_id = :uid
        """), {"tid": tid, "uid": uid})
        stats = sess_row.mappings().fetchone()
        result["timesheet"]["total_sessions"] = stats["total"] or 0
        result["timesheet"]["running"] = stats["running"] or 0

        pending_row = await db.execute(sa_text("""
            SELECT COUNT(*) AS cnt
            FROM timesheet_drafts td
            JOIN timesheet_sessions ts ON ts.id = td.session_id
            WHERE trim(ts.tenant_id) = trim(:tid) AND ts.user_id = :uid
              AND td.status = 'pending'
        """), {"tid": tid, "uid": uid})
        result["timesheet"]["pending_drafts"] = (pending_row.scalar() or 0)

        recent_row = await db.execute(sa_text("""
            SELECT id, date_from, date_to, status,
                   draft_count, approved_count, pushed_count,
                   created_at, completed_at, error_message
            FROM timesheet_sessions
            WHERE trim(tenant_id) = trim(:tid) AND user_id = :uid
            ORDER BY created_at DESC LIMIT 5
        """), {"tid": tid, "uid": uid})

        import datetime
        recent = []
        for r in recent_row.mappings():
            row = dict(r)
            for k, v in row.items():
                if isinstance(v, (datetime.date, datetime.datetime)):
                    row[k] = v.isoformat()
                elif isinstance(v, Decimal):
                    row[k] = float(v)
            recent.append(row)
        result["timesheet"]["recent"] = recent
        # Email stats
        try:
            email_row = await db.execute(sa_text("""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE routing_status = 'pending') AS pending,
                       COUNT(*) FILTER (WHERE routing_status = 'matched') AS matched,
                       COUNT(*) FILTER (WHERE routing_status = 'filed') AS filed
                FROM email_routing_queue WHERE TRIM(tenant_id) = :tid
            """), {"tid": tid})
            er = email_row.mappings().fetchone()
            if er:
                result["email"] = {"status": "live", "total": er["total"] or 0, "pending": er["pending"] or 0, "matched": er["matched"] or 0, "filed": er["filed"] or 0}
        except Exception as e:
            logger.warning("email stats error: %s", e)

    return JSONResponse(result)

@router.get("/email/calendar")
async def email_calendar_api(request: Request, month: str = ""):
    """Per-day email counts for calendar view."""
    tid = _tid(request)
    if not month:
        from datetime import date as _date
        month = _date.today().strftime("%Y-%m")
    from datetime import date as _date, timedelta
    year, mo = int(month[:4]), int(month[5:7])
    first = _date(year, mo, 1)
    last = _date(year + (1 if mo == 12 else 0), (1 if mo == 12 else mo + 1), 1) - timedelta(days=1)
    async with AsyncSessionLocal() as db:
        rows = await db.execute(sa_text("""
            SELECT DATE(received_at) AS d,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE routing_status = 'pending') AS pending,
                   COUNT(*) FILTER (WHERE routing_status = 'matched') AS matched,
                   COUNT(*) FILTER (WHERE routing_status = 'skipped') AS skipped,
                   COUNT(*) FILTER (WHERE filed_to_dms = true) AS filed
            FROM email_routing_queue
            WHERE TRIM(tenant_id) = :tid AND DATE(received_at) BETWEEN :f AND :l
            GROUP BY DATE(received_at)
        """), {"tid": tid, "f": first, "l": last})
        day_data = {}
        for r in rows.mappings():
            day_data[str(r["d"])] = {"total": int(r["total"]), "pending": int(r["pending"]),
                "matched": int(r["matched"]), "skipped": int(r["skipped"]), "filed": int(r["filed"])}
    days = []
    d = first
    while d <= last:
        ds = d.isoformat()
        dd = day_data.get(ds, {"total": 0, "pending": 0, "matched": 0, "skipped": 0, "filed": 0})
        dd["date"] = ds; dd["weekday"] = d.weekday(); days.append(dd)
        d += timedelta(days=1)
    return JSONResponse({"month": month, "days": days,
        "month_total": sum(dy["total"] for dy in days),
        "month_pending": sum(dy["pending"] for dy in days),
        "month_matched": sum(dy["matched"] for dy in days),
        "month_filed": sum(dy["filed"] for dy in days)})
