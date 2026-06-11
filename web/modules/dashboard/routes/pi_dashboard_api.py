"""
Practice Intelligence Dashboard — JSON API v2
===============================================
GET /api/v1/dashboard/pi?tab_slug=firm_view[&attorney_id=N]

Tab-aware unified JSON. asyncpg-safe: date objects not strings.
"""
from __future__ import annotations
import asyncio, logging, re
from datetime import date, datetime, timedelta, timezone
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal
import redis
from rq import Queue

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard-api"])


# ═══════════════════════════════════════════════════════════════
# Redis Dashboard Cache — stale-while-revalidate pattern
# ═══════════════════════════════════════════════════════════════
import json as _json, os as _os

_REDIS_URL = _os.environ.get("REDIS_URL", "redis://redis:6379/0")
_CACHE_VER = "v1"  # bump on schema changes to invalidate all

# TTLs in seconds by data function
_TTL = {
    "firm_stats": 300,       # 5 min — changes on matter create/close
    "matter_tree": 300,      # 5 min
    "ar_aging": 300,         # 5 min
    "wip_by_matter": 120,    # 2 min — changes on time entry
    "calendar": 60,          # 1 min
    "deadlines": 60,         # 1 min
    "tasks": 60,             # 1 min
    "email_summary": 30,     # 30s — changes on sync
    "new_service_items": 60, # 1 min
    "fmi_summary": 300,      # 5 min — expensive Claude call
}

def _cache_key(func_name: str, tid: str, uid=None, extra: str = "") -> str:
    parts = [f"pi:{_CACHE_VER}:{func_name}:{tid.strip()}"]
    if uid: parts.append(str(uid))
    if extra: parts.append(extra)
    return ":".join(parts)

def _get_redis():
    try:
        return redis.from_url(_REDIS_URL, decode_responses=True, socket_connect_timeout=1)
    except Exception:
        return None

def _cache_get(key: str):
    """Get cached JSON, return parsed dict or None."""
    try:
        r = _get_redis()
        if not r: return None
        val = r.get(key)
        if val: return _json.loads(val)
    except Exception as exc:
        logger.debug("cache_get %s: %s", key, exc)
    return None

def _cache_set(key: str, data, ttl: int):
    """Set cache entry."""
    try:
        r = _get_redis()
        if not r: return
        r.setex(key, ttl, _json.dumps(data, default=str))
    except Exception as exc:
        logger.debug("cache_set %s: %s", key, exc)

async def _cached(func_name: str, coro, tid: str, uid=None, extra: str = "", bust: bool = False):
    """Run coro with cache. Returns cached if fresh, else runs and caches."""
    key = _cache_key(func_name, tid, uid, extra)
    ttl = _TTL.get(func_name, 60)
    if not bust:
        cached = _cache_get(key)
        if cached is not None:
            return cached
    result = await coro
    _cache_set(key, result, ttl)
    return result


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()
def _user_id(request: Request):
    u = getattr(request.state, "current_user", None)
    return getattr(u, "id", None) if u else None

async def _firm_stats(tid: str) -> list[dict]:
    try:
        async with AsyncSessionLocal() as session:
            active = (await session.execute(sa_text("SELECT COUNT(*) FROM matters WHERE trim(tenant_id)=trim(:tid) AND status='active'"), {"tid": tid})).scalar() or 0
            clients = (await session.execute(sa_text("SELECT COUNT(*) FROM clients WHERE trim(tenant_id)=trim(:tid)"), {"tid": tid})).scalar() or 0
            w = (await session.execute(sa_text("SELECT COALESCE(SUM(wip_value),0) AS total, COALESCE(SUM(hours),0) AS hrs FROM ts_slips WHERE trim(tenant_id)=trim(:tid) AND billed=false"), {"tid": tid})).mappings().fetchone()
            total_wip = float(w["total"]) if w else 0; total_hrs = float(w["hrs"]) if w else 0
            dl_cnt = (await session.execute(sa_text("SELECT COUNT(*) FROM deadlines d JOIN matters m ON d.matter_id=m.id AND trim(m.tenant_id)=trim(:tid) AND m.status='active' WHERE trim(d.tenant_id)=trim(:tid) AND d.completed_at IS NULL AND d.deadline_date::date >= CURRENT_DATE AND d.deadline_date::date <= CURRENT_DATE + 14"), {"tid": tid})).scalar() or 0
            doc_cnt = (await session.execute(sa_text("SELECT COUNT(*) FROM dms_documents WHERE trim(tenant_id)=trim(:tid)"), {"tid": tid})).scalar() or 0
        return [
            {"label": "Active Matters", "value": str(active), "icon": "briefcase", "subtitle": f"{clients} clients"},
            {"label": "Unbilled WIP", "value": f"${total_wip:,.0f}", "icon": "dollar", "subtitle": f"{total_hrs:,.1f} hours"},
            {"label": "Deadlines", "value": str(dl_cnt), "icon": "clock", "subtitle": "Next 14 days"},
            {"label": "Documents", "value": f"{doc_cnt:,}", "icon": "doc", "subtitle": "Total indexed"},
        ]
    except Exception as exc:
        logger.error("_firm_stats: %s", exc)
        return [{"label": "Error", "value": "—", "icon": "briefcase", "subtitle": str(exc)}]

async def _matter_tree(tid: str) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text("""
                SELECT m.id::text AS id, m.matter_name, m.matter_number, m.status, c.id::text AS client_id, c.client_name
                FROM matters m LEFT JOIN clients c ON m.client_id=c.id AND trim(c.tenant_id)=trim(m.tenant_id)
                WHERE trim(m.tenant_id)=trim(:tid) AND m.status='active' ORDER BY c.client_name NULLS LAST, m.matter_name
            """), {"tid": tid})
            cm = {}
            for r in rows.mappings():
                cid = r["client_id"] or "unknown"
                if cid not in cm: cm[cid] = {"client_id": cid, "client_name": r["client_name"] or "Unknown Client", "matters": []}
                cm[cid]["matters"].append({"id": r["id"], "matter_name": r["matter_name"] or "Untitled", "matter_number": r["matter_number"] or "", "status": r["status"]})
        cl = sorted(cm.values(), key=lambda x: x["client_name"])
        return {"clients": cl, "total_matters": sum(len(c["matters"]) for c in cl), "total_clients": len(cl)}
    except Exception as exc:
        logger.error("_matter_tree: %s", exc); return {"clients": [], "total_matters": 0, "total_clients": 0, "error": str(exc)}

async def _wip_by_matter(tid: str, limit: int = 12) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text("""
                SELECT m.matter_name, m.matter_number, m.id::text AS matter_id, COALESCE(SUM(s.wip_value),0) AS wip_value, COALESCE(SUM(s.hours),0) AS wip_hours
                FROM matters m JOIN ts_clients tc ON tc.ts_raw->>'nickname2'=m.matter_number AND trim(tc.tenant_id)=trim(:tid)
                JOIN ts_slips s ON s.source_client_id=tc.ts_client_id AND trim(s.tenant_id)=trim(:tid) AND s.billed=false
                WHERE trim(m.tenant_id)=trim(:tid) AND m.status='active'
                GROUP BY m.id, m.matter_name, m.matter_number HAVING SUM(s.wip_value)>0 ORDER BY wip_value DESC LIMIT :lim
            """), {"tid": tid, "lim": limit})
            data = [{"matter_name": r["matter_name"] or "Untitled", "matter_number": r["matter_number"] or "",
                "matter_id": r["matter_id"], "wip_value": float(r["wip_value"]), "wip_hours": float(r["wip_hours"])} for r in rows.mappings()]
        mx = max((d["wip_value"] for d in data), default=1) or 1
        for d in data: d["bar_pct"] = round(d["wip_value"]/mx*100, 1)
        return {"chart_data": data, "total_wip": sum(d["wip_value"] for d in data)}
    except Exception as exc:
        logger.error("_wip_by_matter: %s", exc); return {"chart_data": [], "total_wip": 0, "error": str(exc)}

async def _ar_aging(tid: str) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text("""
                SELECT bucket, COUNT(*) AS cnt, SUM(net_due) AS total FROM (SELECT net_due, CASE
                    WHEN CURRENT_DATE - created_at::date <= 30 THEN '0-30' WHEN CURRENT_DATE - created_at::date <= 60 THEN '31-60'
                    WHEN CURRENT_DATE - created_at::date <= 90 THEN '61-90' WHEN CURRENT_DATE - created_at::date <= 120 THEN '91-120' ELSE '120+' END AS bucket
                FROM ts_invoices WHERE trim(tenant_id)=trim(:tid) AND paid_in_full=false AND net_due>0) sub GROUP BY bucket
            """), {"tid": tid})
            raw = {r.bucket: {"count": int(r.cnt), "amount": float(r.total)} for r in rows.mappings()}
        order = ["0-30","31-60","61-90","91-120","120+"]
        buckets = [{"label": b, "count": raw.get(b,{}).get("count",0), "amount": raw.get(b,{}).get("amount",0.0)} for b in order]
        return {"buckets": buckets, "total_ar": sum(b["amount"] for b in buckets)}
    except Exception as exc:
        logger.error("_ar_aging: %s", exc); return {"buckets": [], "total_ar": 0, "error": str(exc)}

async def _calendar(tid: str, attorney_id=None) -> dict:
    today = date.today(); end = today + timedelta(days=7)
    # Use native date objects for asyncpg — NOT isoformat strings
    params = {"tid": tid, "start": today, "end": end}
    ac = ""
    if attorney_id:
        ac = "AND e.attorney_user_id = :atty_uid"
        params["atty_uid"] = int(attorney_id)
    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text(f"""
                SELECT e.subject, e.start_at, e.end_at, e.is_all_day, e.location, m.matter_name
                FROM exchange_calendar_events e LEFT JOIN matters m ON e.matter_id=m.id AND trim(m.tenant_id)=trim(:tid)
                WHERE trim(e.tenant_id)=trim(:tid) AND e.start_at::date >= :start AND e.start_at::date <= :end {ac}
                ORDER BY e.start_at ASC LIMIT 50
            """), params)
            events = [{"subject": r["subject"] or "(No Subject)", "start_at": r["start_at"].isoformat() if r["start_at"] else None,
                "is_all_day": r["is_all_day"], "location": r["location"] or "", "matter_name": r["matter_name"] or "",
                "is_today": r["start_at"].date()==today if r["start_at"] else False} for r in rows.mappings()]
        days = []
        for i in range(7):
            d = today + timedelta(days=i)
            de = [e for e in events if e["start_at"] and datetime.fromisoformat(e["start_at"]).date()==d]
            days.append({"date": d.isoformat(), "label": d.strftime("%a"), "day_num": d.day, "is_today": d==today, "events": de})
        return {"days": days, "total_events": len(events)}
    except Exception as exc:
        logger.error("_calendar error: %s", exc); return {"days": [], "total_events": 0, "error": str(exc)}

async def _deadlines(tid: str, attorney_id=None) -> dict:
    today = date.today(); end = today + timedelta(days=14)
    # Use native date objects for asyncpg
    params = {"tid": tid, "today": today, "end": end}
    ac = ""
    if attorney_id:
        ac = "AND m.originating_attorney_id = :uid"
        params["uid"] = int(attorney_id)
    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text(f"""
                SELECT d.id, d.title, d.deadline_date, d.matter_id::text, m.matter_name, c.client_name
                FROM deadlines d JOIN matters m ON d.matter_id=m.id AND trim(m.tenant_id)=trim(:tid) AND m.status='active' {ac}
                LEFT JOIN clients c ON m.client_id=c.id AND trim(c.tenant_id)=trim(:tid)
                WHERE trim(d.tenant_id)=trim(:tid) AND d.completed_at IS NULL
                  AND d.deadline_date::date >= :today AND d.deadline_date::date <= :end
                ORDER BY d.deadline_date ASC LIMIT 50
            """), params)
            dls = []
            for r in rows.mappings():
                dl = r["deadline_date"]; do_ = (dl.date()-today).days if dl else 0
                urg = "today" if do_==0 else "critical" if do_<=3 else "soon" if do_<=7 else "normal"
                dls.append({"id": r["id"], "title": r["title"], "date_label": dl.strftime("%b %-d") if dl else "",
                    "days_out": do_, "urgency": urg, "matter_name": r["matter_name"] or "", "client_name": r["client_name"] or ""})
        return {"deadlines": dls, "total": len(dls)}
    except Exception as exc:
        logger.error("_deadlines error: %s", exc); return {"deadlines": [], "total": 0, "error": str(exc)}

async def _firm_tasks(tid: str) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text("""
                SELECT t.id, t.title, t.status, t.priority, t.due_date, t.created_at, m.matter_name, u.full_name AS created_by_name
                FROM tasks t LEFT JOIN matters m ON t.matter_id=m.id AND trim(m.tenant_id)=trim(:tid)
                LEFT JOIN users u ON t.created_by=u.id AND trim(u.tenant_id)=trim(:tid)
                WHERE trim(t.tenant_id)=trim(:tid) AND t.status NOT IN ('completed','cancelled')
                ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, t.due_date ASC NULLS LAST LIMIT 15
            """), {"tid": tid})
            tasks = [dict(r._mapping) for r in rows.fetchall()]
            for t in tasks:
                for k in ("due_date","created_at"):
                    if t.get(k) and hasattr(t[k],"isoformat"): t[k] = t[k].isoformat()
        return {"tasks": tasks, "total": len(tasks)}
    except Exception as exc:
        logger.error("_firm_tasks: %s", exc); return {"tasks": [], "total": 0, "error": str(exc)}

async def _email_summary(tid: str, user_id=None) -> dict:
    if not user_id: return {"emails": [], "unread_count": 0, "total": 0}
    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text("""
                SELECT id::text, subject, from_display, from_email, received_at, body_preview, has_attachments, routing_status, matched_matter_id::text AS matter_id
                FROM email_routing_queue WHERE trim(tenant_id)=trim(:tid) AND attorney_user_id = :uid
                ORDER BY received_at DESC NULLS LAST LIMIT 20
            """), {"tid": tid, "uid": int(user_id)})
            emails = []
            for r in rows.mappings():
                recv = r["received_at"]
                emails.append({"id": r["id"], "subject": r["subject"] or "(No Subject)",
                    "from": r["from_display"] or r["from_email"] or "", "received_at": recv.isoformat() if recv else None,
                    "time_label": _relative_time(recv), "body_preview": (r["body_preview"] or "")[:120],
                    "has_attachments": r["has_attachments"], "status": r["routing_status"] or "pending",
                    "matter_id": r["matter_id"], "is_filed": r["routing_status"]=="filed"})
        unread = sum(1 for e in emails if not e["is_filed"])
        return {"emails": emails, "unread_count": unread, "total": len(emails)}
    except Exception as exc:
        logger.error("_email_summary: %s", exc); return {"emails": [], "unread_count": 0, "total": 0, "error": str(exc)}

_DOC_PATS = [(re.compile(r"\border\b",re.I),"Order"),(re.compile(r"\bmotion\b|\bplead",re.I),"Pleading"),
    (re.compile(r"\bdiscover|interrogat|deposition",re.I),"Discovery"),(re.compile(r"\bbrief\b|\bmemo\b",re.I),"Brief"),
    (re.compile(r"\bcontract\b|\bagreement\b",re.I),"Agreement")]
def _classify(fp):
    fn = fp.rsplit("/",1)[-1] if "/" in fp else fp
    for pat,lbl in _DOC_PATS:
        if pat.search(fn): return lbl
    return "Document"

async def _new_service_items(tid: str) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=48); items = []
    try:
        async with AsyncSessionLocal() as session:
            dms = await session.execute(sa_text("SELECT id::text, file_path, indexed_at, source FROM dms_documents WHERE trim(tenant_id)=trim(:tid) AND indexed_at >= :since ORDER BY indexed_at DESC LIMIT 15"), {"tid": tid, "since": since})
            for r in dms.mappings():
                fn = r["file_path"].rsplit("/",1)[-1] if r["file_path"] else "?"
                items.append({"type":"dms","id":r["id"],"title":fn,"subtitle":f"From {r['source'] or 'filesystem'}","timestamp":r["indexed_at"].isoformat() if r["indexed_at"] else None,"doc_type":_classify(fn)})
        async with AsyncSessionLocal() as session:
            em = await session.execute(sa_text("SELECT id::text, subject, from_display, from_email, received_at, has_attachments FROM email_routing_queue WHERE trim(tenant_id)=trim(:tid) AND received_at >= :since ORDER BY received_at DESC LIMIT 15"), {"tid": tid, "since": since})
            for r in em.mappings():
                items.append({"type":"email","id":r["id"],"title":r["subject"] or "(No Subject)","subtitle":f"From: {r['from_display'] or r['from_email'] or ''}","doc_type":"Email","timestamp":r["received_at"].isoformat() if r["received_at"] else None,"has_attachments":r["has_attachments"]})
    except Exception as exc:
        logger.error("_new_service_items: %s", exc)
    items.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return {"items": items[:20], "total": len(items)}

def _relative_time(dt) -> str:
    if not dt: return ""
    try:
        now = datetime.now(timezone.utc)
        if not dt.tzinfo: dt = dt.replace(tzinfo=timezone.utc)
        mins = int((now-dt).total_seconds()/60)
        if mins < 1: return "just now"
        if mins < 60: return f"{mins}m ago"
        hrs = mins//60
        if hrs < 24: return f"{hrs}h ago"
        d = hrs//24
        return "yesterday" if d==1 else f"{d}d ago" if d<7 else dt.strftime("%b %-d")
    except: return ""



@router.post("/pi/email-sync")
async def trigger_email_sync(request: Request):
    """Enqueue Exchange email sync as RQ job. Returns job ID for polling."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"ok": False, "error": "No tenant"}, status_code=400)
    try:
        r = redis.from_url("redis://redis:6379/0")
        q = Queue("default", connection=r)
        job = q.enqueue(
            "jobs.run_connector_sync.run_connector_sync",
            tid, "exchange", "manual",
            job_timeout=1800
        )
        return JSONResponse({"ok": True, "job_id": job.id, "message": "Exchange sync queued"})
    except Exception as exc:
        logger.error("trigger_email_sync: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

@router.get("/pi/email-refresh")
async def email_refresh(request: Request):
    """Re-fetch just the email summary data (lightweight refresh)."""
    tid = _tid(request); uid = _user_id(request)
    return JSONResponse({"email_summary": await _email_summary(tid, user_id=uid)})

@router.get("/pi")
async def practice_intelligence_data(request: Request, tab_slug: str = "firm_view", attorney_id: str | None = None, bust: int = 0):
    tid = _tid(request); uid = _user_id(request); b = bool(bust)
    stats, tree, wip = await asyncio.gather(
        _cached("firm_stats", _firm_stats(tid), tid, bust=b),
        _cached("matter_tree", _matter_tree(tid), tid, bust=b),
        _cached("wip_by_matter", _wip_by_matter(tid), tid, bust=b))
    if tab_slug == "firm_view":
        ar, cal, dl, tasks = await asyncio.gather(
            _cached("ar_aging", _ar_aging(tid), tid, bust=b),
            _cached("calendar", _calendar(tid), tid, bust=b),
            _cached("deadlines", _deadlines(tid), tid, bust=b),
            _cached("tasks", _firm_tasks(tid), tid, bust=b))
        return JSONResponse({"tab_slug": tab_slug, "stats": stats, "matter_tree": tree, "wip_by_matter": wip,
            "ar_aging": ar, "calendar": cal, "deadlines": dl, "tasks": tasks})
    elif tab_slug == "attorney_view":
        atty = (attorney_id if attorney_id else None) or uid
        cal, dl, em, nsi = await asyncio.gather(
            _cached("calendar", _calendar(tid, attorney_id=atty), tid, uid=atty, bust=b),
            _cached("deadlines", _deadlines(tid, attorney_id=atty), tid, uid=atty, bust=b),
            _cached("email_summary", _email_summary(tid, user_id=atty), tid, uid=atty, bust=b),
            _cached("new_service_items", _new_service_items(tid), tid, bust=b))
        return JSONResponse({"tab_slug": tab_slug, "stats": stats, "matter_tree": tree, "wip_by_matter": wip,
            "calendar": cal, "deadlines": dl, "email_summary": em, "new_service_items": nsi})
    elif tab_slug == "my_view":
        cal, dl, em, tasks = await asyncio.gather(
            _cached("calendar", _calendar(tid, attorney_id=uid), tid, uid=uid, bust=b),
            _cached("deadlines", _deadlines(tid, attorney_id=uid), tid, uid=uid, bust=b),
            _cached("email_summary", _email_summary(tid, user_id=uid), tid, uid=uid, bust=b),
            _cached("tasks", _firm_tasks(tid), tid, bust=b))
        return JSONResponse({"tab_slug": tab_slug, "stats": stats, "matter_tree": tree, "wip_by_matter": wip,
            "calendar": cal, "deadlines": dl, "email_summary": em, "tasks": tasks})
    else:
        return JSONResponse({"tab_slug": tab_slug, "stats": stats, "matter_tree": tree, "wip_by_matter": wip,
            "calendar": await _calendar(tid), "deadlines": await _deadlines(tid)})
