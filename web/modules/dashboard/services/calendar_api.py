"""
Calendar API v4.1 — Dual Calendar Architecture (cross-check pulls first)
  - Calendar (manual, human-editable) = calendar_events table
  - AI Calendar (system-generated, read-only) = ai_calendar_events table
  - Cross-check: pull → dedup → compare both directions → log
Aligned with patent FIG. 4: Admin Calendar 444 + System Calendar 442 + Agent 450
"""
from datetime import datetime, date, timedelta
import hashlib, json
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard-calendar"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()
def _uid(r):
    u = getattr(r.state, "current_user", None)
    return u.id if u else None
def _uemail(r):
    u = getattr(r.state, "current_user", None)
    return (u.email or "").lower() if u else ""

def _dedup_key(subject: str, start_at, matter_id=None) -> str:
    """Normalized fingerprint for deduplication between calendars."""
    norm = (subject or "").strip().lower()
    dt = start_at.strftime("%Y-%m-%d-%H%M") if start_at else ""
    mid = str(matter_id or "")
    raw = f"{norm}|{dt}|{mid}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ═══════════════════════════════════════════════════════════════
#  CALENDAR (manual / human-editable)
# ═══════════════════════════════════════════════════════════════

@router.get("/calendar/events")
async def calendar_events(request: Request, start: str = None, end: str = None,
                          view: str = "combined", mailbox: str = None):
    """List manual calendar events. Full CRUD for humans."""
    tid = _tid(request)
    uemail = _uemail(request)
    if not start:
        today = date.today()
        start = today.replace(day=1).isoformat()
    if not end:
        d = date.fromisoformat(start)
        end = (d.replace(year=d.year+1, month=1, day=1) if d.month == 12 else d.replace(month=d.month+1, day=1)).isoformat()

    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)

    extra_where = ""
    params = {"tid": tid, "start": start_dt, "end": end_dt}
    if view == "my":
        extra_where = " AND (LOWER(e.mailbox) = :uemail OR LOWER(e.organizer_email) = :uemail)"
        params["uemail"] = uemail
    elif view == "personal":
        extra_where = " AND LOWER(e.mailbox) = :uemail AND e.source_calendar = 'personal'"
        params["uemail"] = uemail
    if mailbox:
        extra_where += " AND LOWER(e.mailbox) = :mb"
        params["mb"] = mailbox.lower()

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(f"""
            SELECT e.id, e.subject, e.start_at, e.end_at, e.is_all_day,
                   e.location, e.mailbox, e.organizer_email, e.organizer_name,
                   e.attendees, e.body_preview, e.is_recurring, e.source_calendar,
                   e.matter_id, e.source, e.source_id, e.event_type,
                   e.lifecycle_state, e.lifecycle_reason_code,
                   e.lifecycle_reason_note, e.superseded_by_event_id,
                   e.task_id, e.docs_status,
                   m.matter_name, c.client_name
            FROM calendar_events e
            LEFT JOIN matters m ON m.id = e.matter_id AND TRIM(m.tenant_id) = :tid
            LEFT JOIN clients c ON c.id = m.client_id AND TRIM(c.tenant_id) = :tid
            WHERE TRIM(e.tenant_id) = :tid
              AND e.start_at >= :start
              AND e.start_at < :end
              {extra_where}
            ORDER BY e.start_at
        """), params)
        rows = [dict(row) for row in r.mappings()]

    async with AsyncSessionLocal() as session:
        mb_r = await session.execute(sa_text(
            "SELECT DISTINCT mailbox, COUNT(*) as event_count FROM calendar_events WHERE TRIM(tenant_id) = :tid GROUP BY mailbox ORDER BY mailbox"
        ), {"tid": tid})
        mailboxes = [dict(row) for row in mb_r.mappings()]

    events = []
    for row in rows:
        events.append({
            "id": str(row["id"]),
            "title": row["subject"] or "(No Subject)",
            "start": row["start_at"].isoformat() if row["start_at"] else None,
            "end": row["end_at"].isoformat() if row["end_at"] else None,
            "all_day": row["is_all_day"],
            "location": row["location"],
            "mailbox": row["mailbox"],
            "organizer": row["organizer_name"] or row["organizer_email"],
            "attendees": row["attendees"] or [],
            "body_preview": (row["body_preview"] or "")[:200],
            "recurring": row["is_recurring"],
            "source": row["source"],
            "source_calendar": row["source_calendar"],
            "matter_id": str(row["matter_id"]) if row["matter_id"] else None,
            "event_type": row["event_type"],
            "matter_name": row["matter_name"],
            "client_name": row["client_name"],
            "calendar_type": "manual",
            # lifecycle: non-active states render crossed-out ("what was
            # supposed to happen but didn't"); deleted rows are gone entirely.
            "lifecycle_state": row.get("lifecycle_state") or "active",
            "lifecycle_reason_code": row.get("lifecycle_reason_code"),
            "lifecycle_reason_note": row.get("lifecycle_reason_note"),
            "superseded_by_event_id": str(row["superseded_by_event_id"])
                if row.get("superseded_by_event_id") else None,
            "task_id": row.get("task_id"),
            "docs_status": row.get("docs_status") or "pending",
        })
    # Live JMAP events (additive source of truth; never blanks the table view)
    if view != "personal":
        try:
            from core.services import calendar_jmap as _caljmap
            seen = {((e.get("title") or "").strip().lower(), (e.get("start") or "")[:16]) for e in events}
            jm = await _caljmap.events_for(uemail, start_dt.isoformat(), end_dt.isoformat())
            if view in ("combined", None, ""):
                jm += await _caljmap.events_for("calendar@hjmmlegal.com", start_dt.isoformat(), end_dt.isoformat())
            for _ev in jm:
                _k = ((_ev.get("title") or "").strip().lower(), (_ev.get("start") or "")[:16])
                if _k not in seen:
                    seen.add(_k); events.append(_ev)
        except Exception:
            pass

    return {"events": events, "mailboxes": mailboxes, "start": start, "end": end,
            "view": view, "total": len(events), "calendar_type": "manual"}


class EventCreate(BaseModel):
    subject: str
    start_at: str
    end_at: str
    is_all_day: bool = False
    location: Optional[str] = None
    body: Optional[str] = None
    mailbox: Optional[str] = None
    matter_id: Optional[str] = None
    event_type: Optional[str] = None
    attendees: Optional[list] = []

class EventUpdate(BaseModel):
    subject: Optional[str] = None
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    is_all_day: Optional[bool] = None
    location: Optional[str] = None
    body: Optional[str] = None
    matter_id: Optional[str] = None
    event_type: Optional[str] = None


@router.post("/calendar/events")
async def create_event(request: Request, body: EventCreate):
    tid = _tid(request)
    uid = _uid(request)
    uemail = _uemail(request)
    mailbox = body.mailbox or uemail
    start_dt = datetime.fromisoformat(body.start_at)
    end_dt = datetime.fromisoformat(body.end_at)
    matter_uuid = body.matter_id if body.matter_id else None

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            INSERT INTO calendar_events
                (tenant_id, user_id, mailbox, subject, start_at, end_at, is_all_day,
                 location, organizer_email, organizer_name, attendees, body_preview,
                 source_calendar, matter_id, source, created_by, event_type)
            VALUES (:tid, :uid, :mb, :subj, :start, :end, :allday,
                    :loc, :org, '', CAST(:att AS jsonb), :body,
                    'manual', CAST(:mid AS uuid), 'manual', :uid, :etype)
            RETURNING id
        """), {
            "tid": tid, "uid": uid, "mb": mailbox,
            "subj": body.subject, "start": start_dt, "end": end_dt, "allday": body.is_all_day,
            "loc": body.location, "org": uemail, "att": json.dumps(body.attendees or []),
            "body": body.body or "", "mid": matter_uuid, "etype": body.event_type or None,
        })
        row = r.fetchone()
        await session.commit()

    return {"id": str(row[0]), "status": "created", "calendar_type": "manual"}


@router.put("/calendar/events/{event_id}")
async def update_event(request: Request, event_id: str, body: EventUpdate):
    tid = _tid(request)
    sets = []
    params = {"tid": tid, "eid": event_id}
    if body.subject is not None:
        sets.append("subject = :subj"); params["subj"] = body.subject
    if body.start_at is not None:
        sets.append("start_at = :start"); params["start"] = datetime.fromisoformat(body.start_at)
    if body.end_at is not None:
        sets.append("end_at = :end"); params["end"] = datetime.fromisoformat(body.end_at)
    if body.is_all_day is not None:
        sets.append("is_all_day = :allday"); params["allday"] = body.is_all_day
    if body.location is not None:
        sets.append("location = :loc"); params["loc"] = body.location
    if body.body is not None:
        sets.append("body_preview = :body"); params["body"] = body.body
    if body.matter_id is not None:
        sets.append("matter_id = CAST(:mid AS uuid)"); params["mid"] = body.matter_id or None
    if body.event_type is not None:
        sets.append("event_type = :etype"); params["etype"] = body.event_type or None
    if not sets:
        return {"status": "no_changes"}
    sets.append("updated_at = NOW()")

    async with AsyncSessionLocal() as session:
        await session.execute(sa_text(f"""
            UPDATE calendar_events
            SET {', '.join(sets)}
            WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
        """), params)
        await session.commit()

    return {"id": event_id, "status": "updated", "calendar_type": "manual"}


@router.delete("/calendar/events/{event_id}")
async def delete_event(request: Request, event_id: str, hard: bool = False,
                       reason_code: str = "removed"):
    """Provenance-true delete. Default is SOFT: the event is crossed out and
    logged (so "what was supposed to happen but didn't" stays visible). Pass
    ?hard=true to truly delete the row (a snapshot is still logged). The richer
    guided-reason flow lives at /api/v1/calendar-lifecycle/event/{id}/remove."""
    tid = _tid(request)
    from modules.intelligence import calendar_lifecycle as _cl
    out = await _cl.remove_event(tid, event_id, reason_code, hard=hard,
                                 actor=_uid(request), source="ui")
    return {"id": event_id, "status": out.get("lifecycle_state", "removed"),
            "hard": out.get("hard", hard)}


@router.get("/calendar/event/{event_id}")
async def get_event(request: Request, event_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT e.*, m.matter_name, c.client_name
            FROM calendar_events e
            LEFT JOIN matters m ON m.id = e.matter_id AND TRIM(m.tenant_id) = :tid
            LEFT JOIN clients c ON c.id = m.client_id AND TRIM(c.tenant_id) = :tid
            WHERE e.id = CAST(:eid AS uuid) AND TRIM(e.tenant_id) = :tid
        """), {"tid": tid, "eid": event_id})
        row = r.mappings().first()
    if not row:
        return {"error": "not_found"}
    d = dict(row)
    return {
        "id": str(d["id"]), "title": d["subject"],
        "start": d["start_at"].isoformat() if d["start_at"] else None,
        "end": d["end_at"].isoformat() if d["end_at"] else None,
        "all_day": d["is_all_day"], "location": d["location"], "mailbox": d["mailbox"],
        "organizer": d["organizer_name"] or d["organizer_email"],
        "attendees": d["attendees"] or [], "body_preview": d["body_preview"],
        "recurring": d["is_recurring"], "source": d["source"],
        "source_calendar": d["source_calendar"],
        "matter_id": str(d["matter_id"]) if d["matter_id"] else None,
        "matter_name": d.get("matter_name"), "client_name": d.get("client_name"),
        "calendar_type": "manual",
    }


# ═══════════════════════════════════════════════════════════════
#  AI CALENDAR (system-generated, read-only for humans)
# ═══════════════════════════════════════════════════════════════

@router.get("/calendar/ai-events")
async def ai_calendar_events(request: Request, start: str = None, end: str = None,
                              view: str = "combined", include_deduped: bool = False):
    tid = _tid(request)
    uid = _uid(request)
    if not start:
        today = date.today()
        start = today.replace(day=1).isoformat()
    if not end:
        d = date.fromisoformat(start)
        end = (d.replace(year=d.year+1, month=1, day=1) if d.month == 12 else d.replace(month=d.month+1, day=1)).isoformat()

    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)

    dedup_filter = "" if include_deduped else " AND a.is_deduplicated = FALSE"
    view_filter = ""
    params = {"tid": tid, "start": start_dt, "end": end_dt}
    if view == "my":
        view_filter = " AND a.user_id = :uid"
        params["uid"] = uid

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(f"""
            SELECT a.id, a.subject, a.start_at, a.end_at, a.is_all_day,
                   a.location, a.matter_id, a.source_type, a.source_ref,
                   a.ai_model, a.ai_confidence, a.ai_reasoning,
                   a.is_deduplicated, a.matched_calendar_event_id,
                   a.confirmed_by, a.confirmed_at, a.is_active,
                   a.created_at,
                   m.matter_name, c.client_name
            FROM ai_calendar_events a
            LEFT JOIN matters m ON m.id = a.matter_id AND TRIM(m.tenant_id) = :tid
            LEFT JOIN clients c ON c.id = m.client_id AND TRIM(c.tenant_id) = :tid
            WHERE TRIM(a.tenant_id) = :tid
              AND a.is_active = TRUE
              AND a.start_at >= :start
              AND a.start_at < :end
              {dedup_filter}
              {view_filter}
            ORDER BY a.start_at
        """), params)
        rows = [dict(row) for row in r.mappings()]

    events = []
    for row in rows:
        events.append({
            "id": str(row["id"]),
            "title": row["subject"],
            "start": row["start_at"].isoformat() if row["start_at"] else None,
            "end": row["end_at"].isoformat() if row["end_at"] else None,
            "all_day": row["is_all_day"],
            "location": row["location"],
            "matter_id": str(row["matter_id"]) if row["matter_id"] else None,
            "matter_name": row["matter_name"],
            "client_name": row["client_name"],
            "source_type": row["source_type"],
            "ai_confidence": float(row["ai_confidence"]) if row["ai_confidence"] else None,
            "ai_reasoning": row["ai_reasoning"],
            "is_deduplicated": row["is_deduplicated"],
            "matched_manual_event": str(row["matched_calendar_event_id"]) if row["matched_calendar_event_id"] else None,
            "confirmed_by": row["confirmed_by"],
            "confirmed_at": row["confirmed_at"].isoformat() if row["confirmed_at"] else None,
            "calendar_type": "ai",
        })
    return {"events": events, "start": start, "end": end, "view": view,
            "total": len(events), "calendar_type": "ai"}


@router.get("/calendar/ai-events/{event_id}")
async def get_ai_event(request: Request, event_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT a.*, m.matter_name, c.client_name
            FROM ai_calendar_events a
            LEFT JOIN matters m ON m.id = a.matter_id AND TRIM(m.tenant_id) = :tid
            LEFT JOIN clients c ON c.id = m.client_id AND TRIM(c.tenant_id) = :tid
            WHERE a.id = CAST(:eid AS uuid) AND TRIM(a.tenant_id) = :tid
        """), {"tid": tid, "eid": event_id})
        row = r.mappings().first()
    if not row:
        return {"error": "not_found"}
    d = dict(row)
    return {
        "id": str(d["id"]), "title": d["subject"],
        "start": d["start_at"].isoformat() if d["start_at"] else None,
        "end": d["end_at"].isoformat() if d["end_at"] else None,
        "all_day": d["is_all_day"], "location": d["location"],
        "matter_id": str(d["matter_id"]) if d["matter_id"] else None,
        "matter_name": d.get("matter_name"), "client_name": d.get("client_name"),
        "source_type": d["source_type"], "source_ref": d["source_ref"],
        "ai_model": d["ai_model"],
        "ai_confidence": float(d["ai_confidence"]) if d["ai_confidence"] else None,
        "ai_reasoning": d["ai_reasoning"],
        "is_deduplicated": d["is_deduplicated"],
        "matched_manual_event": str(d["matched_calendar_event_id"]) if d["matched_calendar_event_id"] else None,
        "confirmed_by": d["confirmed_by"],
        "confirmed_at": d["confirmed_at"].isoformat() if d["confirmed_at"] else None,
        "calendar_type": "ai",
    }


@router.post("/calendar/ai-events/{event_id}/confirm")
async def confirm_ai_event(request: Request, event_id: str):
    tid = _tid(request)
    uid = _uid(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            UPDATE ai_calendar_events
            SET confirmed_by = :uid, confirmed_at = NOW()
            WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"tid": tid, "eid": event_id, "uid": uid})
        await session.commit()
    return {"id": event_id, "status": "confirmed", "confirmed_by": uid}


@router.post("/calendar/ai-events/{event_id}/deactivate")
async def deactivate_ai_event(request: Request, event_id: str):
    """Mark an AI-generated event as inactive (soft delete)."""
    tid = _tid(request)
    uid = _uid(request)
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            UPDATE ai_calendar_events
            SET is_active = FALSE
            WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"tid": tid, "eid": event_id})
        await session.commit()
    return {"id": event_id, "status": "deactivated"}


# ═══════════════════════════════════════════════════════════════
#  AI CALENDAR PULL — ingest from manual calendar, deduplicate
# ═══════════════════════════════════════════════════════════════

async def _pull_manual_to_ai(tid: str, uid, days_ahead: int = 90):
    """Internal: Pull manual calendar entries into AI calendar with dedup.
    Returns (created, skipped_dedup)."""
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT id, subject, start_at, end_at, is_all_day, location, matter_id
            FROM calendar_events
            WHERE TRIM(tenant_id) = :tid
              AND start_at >= NOW()
              AND start_at < NOW() + CAST(:days AS integer) * INTERVAL '1 day'
        """), {"tid": tid, "days": days_ahead})
        manual_events = [dict(row) for row in r.mappings()]

    created = 0
    skipped = 0
    for evt in manual_events:
        dk = _dedup_key(evt["subject"], evt["start_at"], evt["matter_id"])
        async with AsyncSessionLocal() as session:
            existing = await session.execute(sa_text("""
                SELECT id FROM ai_calendar_events
                WHERE TRIM(tenant_id) = :tid
                  AND (dedup_key = :dk OR matched_calendar_event_id = CAST(:ceid AS uuid))
                  AND is_active = TRUE
                LIMIT 1
            """), {"tid": tid, "dk": dk, "ceid": str(evt["id"])})
            if existing.first():
                skipped += 1
                continue
            await session.execute(sa_text("""
                INSERT INTO ai_calendar_events
                    (tenant_id, user_id, subject, start_at, end_at, is_all_day,
                     location, matter_id, source_type, source_ref,
                     dedup_key, matched_calendar_event_id, is_deduplicated)
                VALUES (:tid, :uid, :subj, :start, :end, :allday,
                        :loc, CAST(:mid AS uuid), 'calendar_pull', :srcref,
                        :dk, CAST(:ceid AS uuid), FALSE)
            """), {
                "tid": tid, "uid": uid, "subj": evt["subject"],
                "start": evt["start_at"], "end": evt["end_at"], "allday": evt["is_all_day"],
                "loc": evt["location"], "mid": str(evt["matter_id"]) if evt["matter_id"] else None,
                "srcref": str(evt["id"]), "dk": dk, "ceid": str(evt["id"]),
            })
            await session.commit()
            created += 1
    return created, skipped


async def _deduplicate_ai(tid: str):
    """Internal: Scan AI events for manual calendar matches, mark as deduped.
    Returns count deduped."""
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT a.id, a.subject, a.start_at, a.matter_id, a.dedup_key
            FROM ai_calendar_events a
            WHERE TRIM(a.tenant_id) = :tid
              AND a.is_active = TRUE
              AND a.is_deduplicated = FALSE
              AND a.source_type != 'calendar_pull'
        """), {"tid": tid})
        ai_events = [dict(row) for row in r.mappings()]

    deduped = 0
    for ai_evt in ai_events:
        dk = ai_evt["dedup_key"] or _dedup_key(ai_evt["subject"], ai_evt["start_at"], ai_evt["matter_id"])
        async with AsyncSessionLocal() as session:
            match = await session.execute(sa_text("""
                SELECT id FROM calendar_events
                WHERE TRIM(tenant_id) = :tid
                  AND LOWER(TRIM(subject)) = LOWER(TRIM(:subj))
                  AND DATE(start_at) = DATE(:start)
                LIMIT 1
            """), {"tid": tid, "subj": ai_evt["subject"], "start": ai_evt["start_at"]})
            manual_match = match.first()
            if manual_match:
                await session.execute(sa_text("""
                    UPDATE ai_calendar_events
                    SET is_deduplicated = TRUE,
                        matched_calendar_event_id = CAST(:ceid AS uuid),
                        dedup_key = :dk
                    WHERE id = CAST(:aid AS uuid) AND TRIM(tenant_id) = :tid
                """), {"tid": tid, "aid": str(ai_evt["id"]), "ceid": str(manual_match[0]), "dk": dk})
                await session.commit()
                deduped += 1
    return deduped


@router.post("/calendar/ai-events/pull")
async def pull_to_ai_calendar(request: Request, days_ahead: int = 90):
    """Pull new manual calendar entries into AI calendar, deduplicating."""
    tid = _tid(request)
    uid = _uid(request)
    created, skipped = await _pull_manual_to_ai(tid, uid, days_ahead)
    return {"pulled": created, "skipped_dedup": skipped, "source_window_days": days_ahead}


@router.post("/calendar/ai-events/deduplicate")
async def deduplicate_ai_calendar(request: Request):
    """Scan AI calendar for entries that match manual calendar entries."""
    tid = _tid(request)
    deduped = await _deduplicate_ai(tid)
    return {"deduplicated": deduped}


# ═══════════════════════════════════════════════════════════════
#  CROSS-CHECK API (patent: Calendar Cross-Check Agent 450)
#  Now: pull → dedup → compare both directions → log
# ═══════════════════════════════════════════════════════════════

@router.post("/calendar/crosscheck")
async def run_crosscheck(request: Request, days_ahead: int = 90):
    """Full dual-calendar cross-check.
    Step 1: Pull manual calendar → AI calendar (fills gaps).
    Step 2: Deduplicate AI entries against manual.
    Step 3: Compare both directions within window.
    Step 4: Clear stale unresolved logs for this window, write fresh.
    Identifies: (a) AI-only, (b) Calendar-only, (c) date/time conflicts."""
    tid = _tid(request)
    uid = _uid(request)
    window_start = date.today()
    window_end = window_start + timedelta(days=days_ahead)
    ws_dt = datetime(window_start.year, window_start.month, window_start.day)
    we_dt = datetime(window_end.year, window_end.month, window_end.day)

    # Step 1: Pull manual → AI
    pulled, pull_skipped = await _pull_manual_to_ai(tid, uid, days_ahead)

    # Step 2: Dedup AI against manual
    deduped = await _deduplicate_ai(tid)

    # Step 3: Load both calendars for window
    async with AsyncSessionLocal() as session:
        ai_r = await session.execute(sa_text("""
            SELECT id, subject, start_at, end_at, matter_id, source_type,
                   matched_calendar_event_id, is_deduplicated
            FROM ai_calendar_events
            WHERE TRIM(tenant_id) = :tid AND is_active = TRUE
              AND start_at >= :ws AND start_at < :we
        """), {"tid": tid, "ws": ws_dt, "we": we_dt})
        ai_events = [dict(r) for r in ai_r.mappings()]

        cal_r = await session.execute(sa_text("""
            SELECT id, subject, start_at, end_at, matter_id, source
            FROM calendar_events
            WHERE TRIM(tenant_id) = :tid
              AND start_at >= :ws AND start_at < :we
        """), {"tid": tid, "ws": ws_dt, "we": we_dt})
        cal_events = [dict(r) for r in cal_r.mappings()]

    # Index by subject+date key
    cal_by_key = {}
    for ce in cal_events:
        key = (ce["subject"] or "").strip().lower() + "|" + (ce["start_at"].strftime("%Y-%m-%d") if ce["start_at"] else "")
        cal_by_key[key] = ce

    ai_by_key = {}
    for ae in ai_events:
        key = (ae["subject"] or "").strip().lower() + "|" + (ae["start_at"].strftime("%Y-%m-%d") if ae["start_at"] else "")
        ai_by_key[key] = ae

    # Build matched_cal_ids — manual events linked to an AI entry via pull or dedup
    matched_cal_ids = set()
    for ae in ai_events:
        if ae["matched_calendar_event_id"]:
            matched_cal_ids.add(str(ae["matched_calendar_event_id"]))

    discrepancies = []

    # (a) AI-only: in AI calendar but not in manual calendar
    #     Exclude calendar_pull entries (they came FROM manual, so the manual version is authoritative)
    for key, ae in ai_by_key.items():
        if key not in cal_by_key and not ae["is_deduplicated"] and ae["source_type"] != "calendar_pull":
            discrepancies.append({
                "type": "ai_only",
                "severity": "warning",
                "ai_event_id": str(ae["id"]),
                "calendar_event_id": None,
                "ai_subject": ae["subject"],
                "calendar_subject": None,
                "ai_date": ae["start_at"].isoformat() if ae["start_at"] else None,
                "calendar_date": None,
                "delta_minutes": None,
                "matter_id": str(ae["matter_id"]) if ae["matter_id"] else None,
                "description": f"AI Calendar has '{ae['subject']}' on {ae['start_at'].strftime('%Y-%m-%d') if ae['start_at'] else '?'} — no matching entry in Calendar. Review whether this AI-generated deadline should be added to Calendar.",
            })

    # (b) Calendar-only: in manual calendar but NOT matched to any AI entry
    #     These are human-entered events with no AI counterpart — the AI system
    #     may have missed a deadline or the event is informational (meeting, etc.)
    for key, ce in cal_by_key.items():
        cal_id_str = str(ce["id"])
        # Only flag if this manual event has no AI match by key AND no AI match by link
        if key not in ai_by_key and cal_id_str not in matched_cal_ids:
            discrepancies.append({
                "type": "calendar_only",
                "severity": "info",
                "ai_event_id": None,
                "calendar_event_id": str(ce["id"]),
                "ai_subject": None,
                "calendar_subject": ce["subject"],
                "ai_date": None,
                "calendar_date": ce["start_at"].isoformat() if ce["start_at"] else None,
                "delta_minutes": None,
                "matter_id": str(ce["matter_id"]) if ce["matter_id"] else None,
                "description": f"Calendar has '{ce['subject']}' on {ce['start_at'].strftime('%Y-%m-%d') if ce['start_at'] else '?'} — no matching entry in AI Calendar. The AI system has no corresponding deadline.",
            })

    # (c) Date/time conflicts between matched events (same subject+date but different times)
    for key in set(ai_by_key.keys()) & set(cal_by_key.keys()):
        ae = ai_by_key[key]
        ce = cal_by_key[key]
        if ae["start_at"] and ce["start_at"]:
            delta = abs((ae["start_at"] - ce["start_at"]).total_seconds())
            if delta > 60:  # >1 minute
                delta_min = int(delta / 60)
                sev = "critical" if delta > 86400 else "warning"
                discrepancies.append({
                    "type": "date_conflict" if delta > 86400 else "time_conflict",
                    "severity": sev,
                    "ai_event_id": str(ae["id"]),
                    "calendar_event_id": str(ce["id"]),
                    "ai_subject": ae["subject"],
                    "calendar_subject": ce["subject"],
                    "ai_date": ae["start_at"].isoformat(),
                    "calendar_date": ce["start_at"].isoformat(),
                    "delta_minutes": delta_min,
                    "matter_id": str(ae["matter_id"]) if ae["matter_id"] else None,
                    "description": f"Time conflict: AI says {ae['start_at'].strftime('%Y-%m-%d %H:%M')}, Calendar says {ce['start_at'].strftime('%Y-%m-%d %H:%M')} ({delta_min}m diff)",
                })

    # Step 4: Clear stale unresolved discrepancies in this window, write fresh
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            DELETE FROM calendar_crosscheck_log
            WHERE TRIM(tenant_id) = :tid AND is_resolved = FALSE
              AND window_start = :ws AND window_end = :we
        """), {"tid": tid, "ws": window_start, "we": window_end})

        for d in discrepancies:
            await session.execute(sa_text("""
                INSERT INTO calendar_crosscheck_log
                    (tenant_id, window_start, window_end, discrepancy_type, severity,
                     ai_event_id, calendar_event_id, ai_subject, calendar_subject,
                     ai_date, calendar_date, delta_minutes, matter_id, description)
                VALUES (:tid, :ws, :we, :dtype, :sev,
                        CAST(:aeid AS uuid), CAST(:ceid AS uuid), :asubj, :csubj,
                        CAST(:adate AS timestamptz), CAST(:cdate AS timestamptz),
                        :delta, CAST(:mid AS uuid), :desc)
            """), {
                "tid": tid, "ws": window_start, "we": window_end,
                "dtype": d["type"], "sev": d["severity"],
                "aeid": d.get("ai_event_id"), "ceid": d.get("calendar_event_id"),
                "asubj": d.get("ai_subject"), "csubj": d.get("calendar_subject"),
                "adate": d.get("ai_date"), "cdate": d.get("calendar_date"),
                "delta": d.get("delta_minutes"), "mid": d.get("matter_id"),
                "desc": d["description"],
            })
        await session.commit()

    summary = {
        "ai_only": len([d for d in discrepancies if d["type"] == "ai_only"]),
        "calendar_only": len([d for d in discrepancies if d["type"] == "calendar_only"]),
        "date_conflicts": len([d for d in discrepancies if d["type"] == "date_conflict"]),
        "time_conflicts": len([d for d in discrepancies if d["type"] == "time_conflict"]),
        "critical": len([d for d in discrepancies if d["severity"] == "critical"]),
        "pull_created": pulled,
        "pull_skipped": pull_skipped,
        "deduped": deduped,
    }

    return {
        "discrepancies": discrepancies,
        "summary": summary,
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "total": len(discrepancies),
    }


@router.get("/calendar/crosscheck/unresolved")
async def get_unresolved_discrepancies(request: Request, limit: int = 50):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT id, discrepancy_type, severity, ai_event_id, calendar_event_id,
                   ai_subject, calendar_subject, ai_date, calendar_date,
                   delta_minutes, matter_id, description, run_at
            FROM calendar_crosscheck_log
            WHERE TRIM(tenant_id) = :tid AND is_resolved = FALSE
            ORDER BY
                CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                run_at DESC
            LIMIT :lim
        """), {"tid": tid, "lim": limit})
        rows = [dict(row) for row in r.mappings()]

    return {"discrepancies": [{
        "id": str(r["id"]),
        "type": r["discrepancy_type"],
        "severity": r["severity"],
        "ai_event_id": str(r["ai_event_id"]) if r["ai_event_id"] else None,
        "calendar_event_id": str(r["calendar_event_id"]) if r["calendar_event_id"] else None,
        "ai_subject": r["ai_subject"],
        "calendar_subject": r["calendar_subject"],
        "ai_date": r["ai_date"].isoformat() if r["ai_date"] else None,
        "calendar_date": r["calendar_date"].isoformat() if r["calendar_date"] else None,
        "delta_minutes": r["delta_minutes"],
        "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
        "description": r["description"],
        "flagged_at": r["run_at"].isoformat() if r["run_at"] else None,
    } for r in rows], "total": len(rows)}


class CrosscheckResolve(BaseModel):
    action: str  # accepted_ai, accepted_calendar, merged, dismissed
    note: Optional[str] = None

@router.post("/calendar/crosscheck/{discrepancy_id}/resolve")
async def resolve_discrepancy(request: Request, discrepancy_id: str, body: CrosscheckResolve):
    tid = _tid(request)
    uid = _uid(request)
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            UPDATE calendar_crosscheck_log
            SET is_resolved = TRUE, resolved_at = NOW(), resolved_by = :uid,
                resolution_action = :act, resolution_note = :note
            WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
        """), {"tid": tid, "did": discrepancy_id, "uid": uid, "act": body.action, "note": body.note})
        await session.commit()
    return {"id": discrepancy_id, "status": "resolved", "action": body.action}


# ═══════════════════════════════════════════════════════════════
#  COMBINED VIEW — both calendars merged
# ═══════════════════════════════════════════════════════════════

@router.get("/calendar/combined")
async def combined_calendar(request: Request, start: str = None, end: str = None,
                            view: str = "combined"):
    tid = _tid(request)
    uemail = _uemail(request)
    uid = _uid(request)
    if not start:
        today = date.today()
        start = today.replace(day=1).isoformat()
    if not end:
        d = date.fromisoformat(start)
        end = (d.replace(year=d.year+1, month=1, day=1) if d.month == 12 else d.replace(month=d.month+1, day=1)).isoformat()

    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    params = {"tid": tid, "start": start_dt, "end": end_dt}

    view_manual = ""
    view_ai = ""
    if view == "my":
        view_manual = " AND (LOWER(e.mailbox) = :uemail OR LOWER(e.organizer_email) = :uemail)"
        view_ai = " AND a.user_id = :uid"
        params["uemail"] = uemail
        params["uid"] = uid

    async with AsyncSessionLocal() as session:
        mr = await session.execute(sa_text(f"""
            SELECT e.id, e.subject AS title, e.start_at, e.end_at, e.is_all_day,
                   e.location, e.matter_id, 'manual' AS calendar_type,
                   e.source, NULL AS ai_confidence, NULL AS source_type,
                   m.matter_name, c.client_name
            FROM calendar_events e
            LEFT JOIN matters m ON m.id = e.matter_id AND TRIM(m.tenant_id) = :tid
            LEFT JOIN clients c ON c.id = m.client_id AND TRIM(c.tenant_id) = :tid
            WHERE TRIM(e.tenant_id) = :tid
              AND e.start_at >= :start AND e.start_at < :end
              {view_manual}
        """), params)
        manual = [dict(r) for r in mr.mappings()]

        ar = await session.execute(sa_text(f"""
            SELECT a.id, a.subject AS title, a.start_at, a.end_at, a.is_all_day,
                   a.location, a.matter_id, 'ai' AS calendar_type,
                   a.source_type AS source, a.ai_confidence, a.source_type,
                   m.matter_name, c.client_name
            FROM ai_calendar_events a
            LEFT JOIN matters m ON m.id = a.matter_id AND TRIM(m.tenant_id) = :tid
            LEFT JOIN clients c ON c.id = m.client_id AND TRIM(c.tenant_id) = :tid
            WHERE TRIM(a.tenant_id) = :tid AND a.is_active = TRUE
              AND a.is_deduplicated = FALSE
              AND a.start_at >= :start AND a.start_at < :end
              {view_ai}
        """), params)
        ai = [dict(r) for r in ar.mappings()]

    all_events = []
    for row in manual + ai:
        all_events.append({
            "id": str(row["id"]),
            "title": row["title"] or "(No Subject)",
            "start": row["start_at"].isoformat() if row["start_at"] else None,
            "end": row["end_at"].isoformat() if row["end_at"] else None,
            "all_day": row["is_all_day"],
            "location": row["location"],
            "matter_id": str(row["matter_id"]) if row["matter_id"] else None,
            "matter_name": row["matter_name"],
            "client_name": row["client_name"],
            "calendar_type": row["calendar_type"],
            "source": row["source"],
            "ai_confidence": float(row["ai_confidence"]) if row["ai_confidence"] else None,
        })

    all_events.sort(key=lambda e: e["start"] or "")

    return {"events": all_events, "start": start, "end": end, "view": view,
            "total": len(all_events),
            "manual_count": len(manual), "ai_count": len(ai)}


# ═══════════════════════════════════════════════════════════════
#  TASKS / DEADLINES / MATTERS SEARCH (unchanged from v3)
# ═══════════════════════════════════════════════════════════════

@router.get("/calendar/tasks")
async def calendar_tasks(request: Request, view: str = "combined"):
    tid = _tid(request)
    uid = _uid(request)
    extra_where = ""
    params = {"tid": tid}
    if view == "my" and uid:
        extra_where = " AND t.created_by = :uid"
        params["uid"] = uid

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(f"""
            SELECT t.id, t.title, t.description, t.status, t.priority,
                   t.due_date, t.matter_id, t.created_by, t.created_at, t.task_type,
                   t.completed_at,
                   m.matter_name, c.client_name
            FROM tasks t
            LEFT JOIN matters m ON m.id = t.matter_id AND TRIM(m.tenant_id) = :tid
            LEFT JOIN clients c ON c.id = m.client_id AND TRIM(c.tenant_id) = :tid
            WHERE TRIM(t.tenant_id) = :tid
              AND (
                    (COALESCE(t.status, 'open') NOT IN ('completed', 'snoozed')
                     OR t.completed_at >= NOW() - INTERVAL '3 days')
                    OR (t.status = 'snoozed'
                        AND (t.tags->>'snooze_until')::timestamptz <= NOW())
                  )
              {extra_where}
            ORDER BY
              CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END,
              CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
              t.due_date NULLS LAST
            LIMIT 100
        """), params)
        rows = [dict(row) for row in r.mappings()]

    tasks = []
    for row in rows:
        tasks.append({
            "id": str(row["id"]), "title": row["title"], "description": row["description"],
            "status": row["status"], "priority": row["priority"],
            "due_date": row["due_date"].isoformat() if row["due_date"] else None,
            "completed_at": row["completed_at"].isoformat() if row.get("completed_at") else None,
            "matter_id": str(row["matter_id"]) if row["matter_id"] else None,
            "matter_name": row["matter_name"], "client_name": row["client_name"],
            "task_type": row["task_type"],
        })
    return {"tasks": tasks, "total": len(tasks), "view": view}


@router.get("/calendar/deadlines")
async def calendar_deadlines(request: Request, view: str = "combined", days: int = 90):
    # Deadlines-as-tasks: a deadline is a task with a deadline-flavored task_type
    # (deadline/sol) or one carried over by the deadline backfill. Reads `tasks`,
    # not the legacy `deadlines` table. Response shape unchanged for DeadlinesPanel.
    tid = _tid(request)
    uid = _uid(request)
    extra_where = ""
    params = {"tid": tid, "days": days}
    if view == "my" and uid:
        extra_where = " AND m.originating_attorney_id = :uid"
        params["uid"] = uid

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(f"""
            SELECT t.id, t.title, t.due_date,
                   COALESCE(t.tags->>'orig_deadline_type', t.task_type) AS dtype,
                   t.notes,
                   (t.task_type = 'sol'
                    OR COALESCE((t.tags->>'is_sol')::boolean, false)) AS is_sol,
                   t.matter_id, m.matter_name, c.client_name
            FROM tasks t
            LEFT JOIN matters m ON m.id = t.matter_id AND TRIM(m.tenant_id) = :tid
            LEFT JOIN clients c ON c.id = m.client_id AND TRIM(c.tenant_id) = :tid
            WHERE TRIM(t.tenant_id) = :tid
              AND t.due_date IS NOT NULL
              AND t.due_date >= CURRENT_DATE
              AND t.due_date <= CURRENT_DATE + CAST(:days AS integer)
              AND COALESCE(t.status, 'open') NOT IN ('completed', 'cancelled', 'snoozed')
              AND (t.task_type IN ('deadline', 'sol') OR t.source = 'deadline_backfill')
              {extra_where}
            ORDER BY t.due_date
            LIMIT 100
        """), params)
        rows = [dict(row) for row in r.mappings()]

    deadlines = []
    for row in rows:
        dd = row["due_date"].date() if hasattr(row["due_date"], 'date') else row["due_date"]
        days_out = (dd - date.today()).days if dd else None
        deadlines.append({
            "id": str(row["id"]), "title": row["title"],
            "due_date": dd.isoformat() if dd else None, "days_out": days_out,
            "type": row["dtype"], "notes": row["notes"], "is_sol": row["is_sol"],
            "matter_id": str(row["matter_id"]) if row["matter_id"] else None,
            "matter_name": row["matter_name"], "client_name": row["client_name"],
        })
    return {"deadlines": deadlines, "total": len(deadlines), "view": view}


@router.get("/calendar/tasks/past")
async def past_tasks(request: Request, view: str = "combined", page: int = 0):
    tid = _tid(request)
    uid = _uid(request)
    extra_where = ""
    params = {"tid": tid, "offset": page * 50}
    if view == "my" and uid:
        extra_where = " AND t.created_by = :uid"
        params["uid"] = uid

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(f"""
            SELECT t.id, t.title, t.description, t.status, t.priority,
                   t.due_date, t.matter_id, t.created_by, t.created_at, t.task_type,
                   t.completed_at,
                   m.matter_name, c.client_name
            FROM tasks t
            LEFT JOIN matters m ON m.id = t.matter_id AND TRIM(m.tenant_id) = :tid
            LEFT JOIN clients c ON c.id = m.client_id AND TRIM(c.tenant_id) = :tid
            WHERE TRIM(t.tenant_id) = :tid
              AND t.status = 'completed'
              AND t.completed_at < NOW() - INTERVAL '3 days'
              {extra_where}
            ORDER BY t.completed_at DESC
            LIMIT 50 OFFSET :offset
        """), params)
        rows = [dict(row) for row in r.mappings()]

    tasks = []
    for row in rows:
        tasks.append({
            "id": str(row["id"]), "title": row["title"], "description": row["description"],
            "status": row["status"], "priority": row["priority"],
            "due_date": row["due_date"].isoformat() if row["due_date"] else None,
            "completed_at": row["completed_at"].isoformat() if row.get("completed_at") else None,
            "matter_id": str(row["matter_id"]) if row["matter_id"] else None,
            "matter_name": row["matter_name"], "client_name": row["client_name"],
            "task_type": row["task_type"],
        })
    return {"tasks": tasks, "total": len(tasks), "view": view, "page": page}


class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    priority: str = "medium"
    due_date: Optional[str] = None
    matter_id: Optional[str] = None
    task_type: str = "general"

class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    due_date: Optional[str] = None
    matter_id: Optional[str] = None
    task_type: Optional[str] = None


@router.post("/calendar/tasks")
async def create_task(request: Request, body: TaskCreate):
    tid = _tid(request)
    uid = _uid(request)
    due = datetime.fromisoformat(body.due_date) if body.due_date else None
    mid = body.matter_id if body.matter_id else None
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            INSERT INTO tasks (tenant_id, title, description, priority, due_date,
                               matter_id, task_type, source, created_by, status)
            VALUES (:tid, :title, :desc, :pri, :due,
                    CAST(:mid AS uuid), :ttype, 'manual', :uid, 'open')
            RETURNING id
        """), {"tid": tid, "title": body.title, "desc": body.description,
               "pri": body.priority, "due": due, "mid": mid,
               "ttype": body.task_type, "uid": uid})
        row = r.fetchone()
        await session.commit()
    return {"id": str(row[0]), "status": "created"}


@router.put("/calendar/tasks/{task_id}")
async def update_task(request: Request, task_id: str, body: TaskUpdate):
    tid = _tid(request)
    sets = []
    params = {"tid": tid, "tid2": int(task_id)}
    if body.title is not None: sets.append("title = :title"); params["title"] = body.title
    if body.description is not None: sets.append("description = :desc"); params["desc"] = body.description
    if body.priority is not None: sets.append("priority = :pri"); params["pri"] = body.priority
    if body.status is not None:
        sets.append("status = :st"); params["st"] = body.status
        if body.status == "completed": sets.append("completed_at = NOW()")
    if body.due_date is not None:
        sets.append("due_date = :due")
        params["due"] = datetime.fromisoformat(body.due_date) if body.due_date else None
    if body.matter_id is not None:
        sets.append("matter_id = CAST(:mid AS uuid)")
        params["mid"] = body.matter_id if body.matter_id else None
    if body.task_type is not None: sets.append("task_type = :ttype"); params["ttype"] = body.task_type
    if not sets: return {"status": "no_changes"}
    sets.append("updated_at = NOW()")
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text(f"""
            UPDATE tasks SET {', '.join(sets)}
            WHERE id = :tid2 AND TRIM(tenant_id) = :tid
        """), params)
        await session.commit()
    return {"id": task_id, "status": "updated"}


@router.delete("/calendar/tasks/{task_id}")
async def delete_task(request: Request, task_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            DELETE FROM tasks WHERE id = :tid2 AND TRIM(tenant_id) = :tid
        """), {"tid": tid, "tid2": int(task_id)})
        await session.commit()
    return {"id": task_id, "status": "deleted"}


@router.post("/calendar/tasks/{task_id}/snooze")
async def snooze_task(request: Request, task_id: str, days: int = 3):
    tid = _tid(request)
    snooze_until = datetime.now() + timedelta(days=days)
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            UPDATE tasks SET status = 'snoozed',
                   tags = COALESCE(tags, '[]'::jsonb) || jsonb_build_object('snooze_until', :snooze),
                   updated_at = NOW()
            WHERE id = :tid2 AND TRIM(tenant_id) = :tid
        """), {"tid": tid, "tid2": int(task_id), "snooze": snooze_until.isoformat()})
        await session.commit()
    return {"id": task_id, "status": "snoozed", "until": snooze_until.isoformat()}


@router.get("/calendar/matters-search")
async def matters_search(request: Request, q: str = ""):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.id, m.matter_number, m.matter_name, c.client_name
            FROM matters m
            LEFT JOIN clients c ON c.id = m.client_id AND TRIM(c.tenant_id) = :tid
            WHERE TRIM(m.tenant_id) = :tid
              AND (m.matter_name ILIKE :q OR m.matter_number ILIKE :q OR c.client_name ILIKE :q)
            ORDER BY m.matter_name LIMIT 20
        """), {"tid": tid, "q": f"%{q}%"})
        rows = [dict(row) for row in r.mappings()]
    return {"matters": [{"id": str(r["id"]), "number": r["matter_number"],
                         "name": r["matter_name"], "client": r["client_name"]} for r in rows]}
