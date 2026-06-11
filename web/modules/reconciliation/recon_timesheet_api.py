"""
modules/reconciliation/recon_timesheet_api.py
JSON APIs for the timesheet reconciliation workflow.

GET  /api/v1/reconciliation/timesheet/calendar?month=2026-05  -> month calendar data
GET  /api/v1/reconciliation/timesheet/day/{date}              -> daily entries
POST /api/v1/reconciliation/timesheet/day/{date}              -> add manual entry
PUT  /api/v1/reconciliation/timesheet/entry/{id}              -> edit entry inline
POST /api/v1/reconciliation/timesheet/approve                 -> bulk approve entries
GET  /api/v1/reconciliation/timesheet/sessions                -> session list
GET  /api/v1/reconciliation/timesheet/session/{id}            -> session detail + drafts
POST /api/v1/reconciliation/timesheet/start                   -> start AI rec session (JSON)
GET  /api/v1/reconciliation/timesheet/matters                 -> active matters for dropdown
"""
from __future__ import annotations
import logging, uuid as _uuid, math
from datetime import date, datetime, timedelta
from decimal import Decimal
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/reconciliation/timesheet", tags=["recon-timesheet-api"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()
def _uid(r):
    user = getattr(r.state, "current_user", None)
    return getattr(user, "id", 0) if user else 0
def _uname(r):
    user = getattr(r.state, "current_user", None)
    return getattr(user, "full_name", "") or getattr(user, "username", "") if user else ""

def _ser(row):
    import datetime as dt
    out = {}
    for k, v in (row.items() if isinstance(row, dict) else dict(row).items()):
        if isinstance(v, Decimal): out[k] = float(v)
        elif isinstance(v, (dt.date, dt.datetime)): out[k] = v.isoformat()
        elif isinstance(v, _uuid.UUID): out[k] = str(v)
        else: out[k] = v
    return out

@router.get("/calendar")
async def calendar_api(request: Request, month: str = ""):
    tid = _tid(request)
    uid = _uid(request)
    if not month: month = date.today().strftime("%Y-%m")
    year, mo = int(month[:4]), int(month[5:7])
    first = date(year, mo, 1)
    last = date(year + (1 if mo == 12 else 0), (1 if mo == 12 else mo + 1), 1) - timedelta(days=1)

    async with AsyncSessionLocal() as db:
        ts_rows = await db.execute(sa_text("""
            SELECT slip_date AS d, COUNT(*) AS entries, SUM(hours) AS hours, SUM(wip_value) AS value
            FROM ts_slips WHERE trim(tenant_id) = trim(:tid) AND slip_date BETWEEN :f AND :l
            GROUP BY slip_date
        """), {"tid": tid, "f": first, "l": last})
        ts_data = {str(r["d"]): _ser(dict(r)) for r in ts_rows.mappings()}

        te_rows = await db.execute(sa_text("""
            SELECT date AS d, COUNT(*) AS entries, SUM(hours) AS hours,
                   SUM(COALESCE(amount, hours * COALESCE(rate, 0))) AS value
            FROM time_entries WHERE trim(tenant_id) = trim(:tid) AND date BETWEEN :f AND :l
            GROUP BY date
        """), {"tid": tid, "f": first, "l": last})
        te_data = {str(r["d"]): _ser(dict(r)) for r in te_rows.mappings()}

        dr_rows = await db.execute(sa_text("""
            SELECT entry_date AS d, COUNT(*) AS pending
            FROM timesheet_drafts td JOIN timesheet_sessions ts ON ts.id = td.session_id
            WHERE trim(ts.tenant_id) = trim(:tid) AND ts.user_id = :uid
              AND td.status = 'pending' AND td.entry_date BETWEEN :f AND :l
            GROUP BY entry_date
        """), {"tid": tid, "uid": uid, "f": first, "l": last})
        draft_data = {str(r["d"]): int(r["pending"]) for r in dr_rows.mappings()}

    days = []
    d = first
    while d <= last:
        ds = d.isoformat()
        ts = ts_data.get(ds, {}); te = te_data.get(ds, {})
        total_hours = float(ts.get("hours") or 0) + float(te.get("hours") or 0)
        total_entries = int(ts.get("entries") or 0) + int(te.get("entries") or 0)
        total_value = float(ts.get("value") or 0) + float(te.get("value") or 0)
        days.append({"date": ds, "weekday": d.weekday(), "hours": round(total_hours, 1),
                      "entries": total_entries, "value": round(total_value, 2),
                      "pending_drafts": draft_data.get(ds, 0)})
        d += timedelta(days=1)
    month_total = sum(dy["hours"] for dy in days)
    weekdays = sum(1 for dy in days if dy["weekday"] < 5)
    return JSONResponse({"month": month, "days": days, "month_total_hours": round(month_total, 1),
                          "target_hours": weekdays * 8, "weekday_count": weekdays})

@router.get("/day/{day}")
async def daily_entries_api(request: Request, day: str):
    tid = _tid(request); uid = _uid(request); target_date = date.fromisoformat(day)
    include_hidden = request.query_params.get('include_hidden', '') == 'true'
    entries = []
    hidden_count = 0
    async with AsyncSessionLocal() as db:
        ts_rows = await db.execute(sa_text("""
            SELECT s.source_slip_id AS id, s.slip_date AS entry_date, s.hours, s.rate,
                   s.wip_value AS value, s.narrative AS description,
                   COALESCE(tk.ts_name, s.source_tk_id) AS timekeeper_name,
                   tc.ts_name AS matter_name, NULL::uuid AS matter_id,
                   s.billed, 'ts_slip' AS source, 'timeslips' AS source_label, NULL::numeric AS ai_confidence
            FROM ts_slips s
            LEFT JOIN ts_timekeepers tk ON s.source_tk_id = tk.ts_tk_id AND trim(tk.tenant_id) = trim(s.tenant_id)
            LEFT JOIN ts_clients tc ON tc.ts_client_id::text = s.source_client_id AND trim(tc.tenant_id) = trim(s.tenant_id)
            WHERE trim(s.tenant_id) = trim(:tid) AND s.slip_date = :d ORDER BY tk.ts_name
        """), {"tid": tid, "d": target_date})
        for r in ts_rows.mappings():
            entries.append({**_ser(dict(r)), "editable": False, "approved": r["billed"]})

        te_rows = await db.execute(sa_text("""
            SELECT te.id::text, te.date AS entry_date, te.matter_id::text, te.hours, te.rate,
                   COALESCE(te.amount, te.hours * COALESCE(te.rate, 0)) AS value,
                   te.description, te.timekeeper_name, te.billable, te.status, te.source,
                   CASE WHEN te.source = 'timesheet_reconciliation' THEN 'AI Rec'
                        WHEN te.source = 'manual' THEN 'Manual' ELSE COALESCE(te.source, 'Manual') END AS source_label,
                   te.ai_confidence, m.matter_name, m.matter_number
            FROM time_entries te LEFT JOIN matters m ON m.id = te.matter_id
            WHERE trim(te.tenant_id) = trim(:tid) AND te.date = :d ORDER BY te.timekeeper_name
        """), {"tid": tid, "d": target_date})
        for r in te_rows.mappings():
            row = _ser(dict(r)); row["editable"] = row.get("status") in ("draft", None)
            row["approved"] = row.get("status") in ("approved", "billed"); entries.append(row)

        if include_hidden:
            draft_status_filter = "AND td.status IN ('pending', 'personal', 'rejected')"
        else:
            draft_status_filter = "AND td.status = 'pending'"

        dr_rows = await db.execute(sa_text(f"""
            SELECT td.id, td.entry_date, td.matter_id::text, td.matter_name, td.hours,
                   td.description, td.ai_narrative, td.source, td.ai_confidence, td.status, td.session_id
            FROM timesheet_drafts td JOIN timesheet_sessions ts ON ts.id = td.session_id
            WHERE trim(ts.tenant_id) = trim(:tid) AND ts.user_id = :uid
              AND td.entry_date = :d {draft_status_filter} ORDER BY td.ai_confidence DESC
        """), {"tid": tid, "uid": uid, "d": target_date})
        for r in dr_rows.mappings():
            row = _ser(dict(r)); row["source_label"] = "AI Draft"; row["editable"] = True
            row["approved"] = False; row["is_draft"] = True; entries.append(row)

        if not include_hidden:
            hr = await db.execute(sa_text("""
                SELECT COUNT(*) AS cnt FROM timesheet_drafts td
                JOIN timesheet_sessions ts ON ts.id = td.session_id
                WHERE trim(ts.tenant_id) = trim(:tid) AND ts.user_id = :uid
                  AND td.entry_date = :d AND td.status IN ('personal', 'rejected')
            """), {"tid": tid, "uid": uid, "d": target_date})
            hidden_count = hr.scalar() or 0

    return JSONResponse({"date": day, "entries": entries, "hidden_count": hidden_count,
                          "total_hours": round(sum(float(e.get("hours") or 0) for e in entries), 2),
                          "entry_count": len(entries)})


@router.post("/day/{day}")
async def add_manual_entry(request: Request, day: str):
    tid = _tid(request); uid = _uid(request); uname = _uname(request)
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid body"}, status_code=400)
    entry_id = str(_uuid.uuid4())
    hours = math.ceil(float(body.get("hours", 0)) * 4) / 4
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            INSERT INTO time_entries (id, tenant_id, matter_id, user_id, description, hours, date, entry_date, billable, status, source, timekeeper_name, created_at, updated_at)
            VALUES (CAST(:id AS uuid), :tid, CAST(NULLIF(:mid, '') AS uuid), :uid, :desc, :hours, :d, :d, :billable, 'draft', 'manual', :tkname, NOW(), NOW())
        """), {"id": entry_id, "tid": tid, "mid": body.get("matter_id") or "", "uid": uid,
               "desc": body.get("description", ""), "hours": hours, "d": day,
               "billable": body.get("billable", True), "tkname": uname})
        await db.commit()
    return JSONResponse({"id": entry_id, "status": "created"}, status_code=201)

@router.put("/entry/{entry_id}")
async def edit_entry(request: Request, entry_id: str):
    tid = _tid(request)
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid body"}, status_code=400)
    sets = []; params = {"id": entry_id, "tid": tid}
    if "hours" in body: sets.append("hours = :hours"); params["hours"] = math.ceil(float(body["hours"]) * 4) / 4
    if "description" in body: sets.append("description = :desc"); params["desc"] = body["description"]
    if "matter_id" in body: sets.append("matter_id = CAST(NULLIF(:mid, '') AS uuid)"); params["mid"] = body["matter_id"] or ""
    if "billable" in body: sets.append("billable = :billable"); params["billable"] = body["billable"]
    if not sets: return JSONResponse({"error": "Nothing to update"}, status_code=400)
    sets.append("updated_at = NOW()")
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text(f"UPDATE time_entries SET {', '.join(sets)} WHERE id = CAST(:id AS uuid) AND trim(tenant_id) = trim(:tid)"), params)
        await db.commit()
    return JSONResponse({"status": "updated"})

@router.post("/approve")
async def bulk_approve(request: Request):
    tid = _tid(request); uid = _uid(request)
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid body"}, status_code=400)
    entry_ids = body.get("entry_ids", []); draft_ids = body.get("draft_ids", []); approved = 0
    async with AsyncSessionLocal() as db:
        for eid in entry_ids:
            await db.execute(sa_text("UPDATE time_entries SET status = 'approved', reviewed_at = NOW(), reviewed_by = :uid WHERE id = CAST(:id AS uuid) AND trim(tenant_id) = trim(:tid) AND status = 'draft'"), {"id": eid, "tid": tid, "uid": uid})
            approved += 1
        for did in draft_ids:
            dr = await db.execute(sa_text("SELECT * FROM timesheet_drafts WHERE id = :id AND status = 'pending'"), {"id": did})
            draft = dr.mappings().fetchone()
            if not draft: continue
            new_id = str(_uuid.uuid4())
            narrative = draft["ai_narrative"] or draft["description"] or ""
            await db.execute(sa_text("""
                INSERT INTO time_entries (id, tenant_id, matter_id, user_id, description, hours, date, entry_date, billable, status, source, ai_confidence, ai_original_description, created_at, updated_at)
                VALUES (CAST(:id AS uuid), :tid, CAST(:mid AS uuid), :uid, :desc, :hours, :edate, :edate, true, 'approved', :source, :conf, :orig, NOW(), NOW())
            """), {"id": new_id, "tid": tid, "mid": str(draft["matter_id"]) if draft["matter_id"] else None,
                   "uid": uid, "desc": narrative, "hours": float(draft["hours"]), "edate": draft["entry_date"],
                   "source": draft.get("source", "timesheet_reconciliation"),
                   "conf": float(draft["ai_confidence"]) if draft["ai_confidence"] else None, "orig": draft["description"]})
            await db.execute(sa_text("UPDATE timesheet_drafts SET status = 'pushed', time_entry_id = CAST(:teid AS uuid), reviewed_at = NOW(), reviewed_by = :uid WHERE id = :id"),
                             {"teid": new_id, "uid": uid, "id": did})
            approved += 1
        await db.commit()
    return JSONResponse({"approved": approved})

@router.get("/matters")
async def matters_dropdown(request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        rows = await db.execute(sa_text("""
            SELECT m.id::text, m.matter_name, m.matter_number, c.client_name
            FROM matters m LEFT JOIN clients c ON c.id = m.client_id
            WHERE trim(m.tenant_id) = trim(:tid) AND LOWER(m.status) IN ('active', 'open')
            ORDER BY c.client_name, m.matter_name
        """), {"tid": tid})
    return JSONResponse({"matters": [_ser(dict(r)) for r in rows.mappings()]})

@router.get("/sessions")
async def sessions_list(request: Request):
    tid = _tid(request); uid = _uid(request)
    async with AsyncSessionLocal() as db:
        rows = await db.execute(sa_text("""
            SELECT id, date_from, date_to, status, draft_count, approved_count, pushed_count, created_at, completed_at, error_message
            FROM timesheet_sessions WHERE trim(tenant_id) = trim(:tid) AND user_id = :uid ORDER BY created_at DESC LIMIT 20
        """), {"tid": tid, "uid": uid})
    return JSONResponse({"sessions": [_ser(dict(r)) for r in rows.mappings()]})

@router.get("/session/{session_id}")
async def session_detail(request: Request, session_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        sr = await db.execute(sa_text("SELECT * FROM timesheet_sessions WHERE id = :sid AND trim(tenant_id) = trim(:tid)"), {"sid": session_id, "tid": tid})
        session = sr.mappings().fetchone()
        if not session: return JSONResponse({"error": "Not found"}, status_code=404)
        dr = await db.execute(sa_text("""
            SELECT id, entry_date, matter_id::text, matter_name, hours, description, ai_narrative, source, ai_confidence, status, source_detail, reviewed_at, time_entry_id::text
            FROM timesheet_drafts WHERE session_id = :sid ORDER BY entry_date ASC, ai_confidence DESC
        """), {"sid": session_id})
        drafts = [_ser(dict(r)) for r in dr.mappings()]
        mr = await db.execute(sa_text("SELECT id::text, matter_name FROM matters WHERE trim(tenant_id) = trim(:tid) AND LOWER(status) IN ('active', 'open') ORDER BY matter_name"), {"tid": tid})
        matters = {r["id"]: r["matter_name"] for r in mr.mappings()}
    return JSONResponse({"session": _ser(dict(session)), "drafts": drafts, "matters": matters,
                          "pending_count": sum(1 for d in drafts if d.get("status") == "pending")})

@router.post("/start")
async def start_session_api(request: Request):
    tid = _tid(request); uid = _uid(request); uname = _uname(request)
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid body"}, status_code=400)
    try: df = date.fromisoformat(body.get("date_from", "")); dt = date.fromisoformat(body.get("date_to", ""))
    except ValueError: return JSONResponse({"error": "Invalid dates"}, status_code=400)
    session_id = str(_uuid.uuid4())
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("INSERT INTO timesheet_sessions (id, tenant_id, user_id, date_from, date_to, status) VALUES (:id, :tid, :uid, :df, :dt, 'running')"),
                         {"id": session_id, "tid": tid, "uid": uid, "df": df, "dt": dt})
        await db.commit()
    try:
        import os, redis as _redis; from rq import Queue
        redis_conn = _redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://10.10.0.10:6379/0"))
        Queue("default", connection=redis_conn).enqueue("jobs.timesheet_reconcile_job.run", session_id, tid, uid, uname, df.isoformat(), dt.isoformat(), [], job_timeout=600)
    except Exception as exc:
        log.error("Failed to enqueue timesheet job: %s", exc)
        async with AsyncSessionLocal() as db:
            await db.execute(sa_text("UPDATE timesheet_sessions SET status='failed', error_message=:err, completed_at=NOW() WHERE id=:id"), {"err": str(exc)[:300], "id": session_id})
            await db.commit()
    return JSONResponse({"session_id": session_id, "status": "running"}, status_code=201)

@router.delete("/draft/{draft_id}")
async def delete_draft(request: Request, draft_id: str):
    """Delete a pending timesheet draft."""
    tid = _tid(request); uid = _uid(request)
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text(
            "DELETE FROM timesheet_drafts WHERE id = :id AND status = 'pending' "
            "AND session_id IN (SELECT id FROM timesheet_sessions WHERE trim(tenant_id) = trim(:tid) AND user_id = :uid)"
        ), {"id": draft_id, "tid": tid, "uid": uid})
        await db.commit()
    return JSONResponse({"status": "deleted"})

@router.delete("/entry/{entry_id}")
async def delete_entry(request: Request, entry_id: str):
    """Delete a draft time_entry (not approved/billed)."""
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text(
            "DELETE FROM time_entries WHERE id = CAST(:id AS uuid) AND trim(tenant_id) = trim(:tid) AND status = 'draft'"
        ), {"id": entry_id, "tid": tid})
        await db.commit()
    return JSONResponse({"status": "deleted"})

@router.post("/bulk-delete")
async def bulk_delete(request: Request):
    """Bulk delete drafts and/or draft time_entries."""
    tid = _tid(request); uid = _uid(request)
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid body"}, status_code=400)
    entry_ids = body.get("entry_ids", []); draft_ids = body.get("draft_ids", []); deleted = 0
    async with AsyncSessionLocal() as db:
        for eid in entry_ids:
            r = await db.execute(sa_text(
                "DELETE FROM time_entries WHERE id = CAST(:id AS uuid) AND trim(tenant_id) = trim(:tid) AND status = 'draft' RETURNING id"
            ), {"id": eid, "tid": tid})
            if r.fetchone(): deleted += 1
        for did in draft_ids:
            r = await db.execute(sa_text(
                "DELETE FROM timesheet_drafts WHERE id = :id AND status = 'pending' "
                "AND session_id IN (SELECT id FROM timesheet_sessions WHERE trim(tenant_id) = trim(:tid) AND user_id = :uid) RETURNING id"
            ), {"id": did, "tid": tid, "uid": uid})
            if r.fetchone(): deleted += 1
        await db.commit()
    return JSONResponse({"deleted": deleted})
