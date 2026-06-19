"""calendar_lifecycle.py — calendar event lifecycle (Court/Hearing build).

The calendar is a temporal rendition of the firm's tasks/issues and the entry
point to each issue's workspace. This module makes calendar changes provenance-
true: nothing is silently dropped.

  * remove_event(reason)  — soft by default: the event stays CROSSED OUT
    (lifecycle_state) so "what was supposed to happen but didn't" is still
    visible. hard=True actually deletes the row, but the change log keeps a
    snapshot so the audit trail survives.
  * reschedule_event       — the old row is marked 'rescheduled' (crossed out,
    superseded_by_event_id -> new) and a new active row is created; the
    reconciliation resolver then clusters both into one hearing + reschedule.
  * reinstate_event        — undo a soft removal.
  * link_document / waive_documents — the "upload now / will add later / no docs"
    prompt outcomes.
  * create_task_from_event / spawn_task — the event<->task spine (events are
    renditions of tasks); "just create a task" path.
  * find_matches           — on add, search existing hearings/events to prompt
    "is this a reschedule of X?" (new stuff goes through the pipeline).
  * resolve_workspace      — §2 dispatch (data-driven): event_type -> workspace.

Every mutation writes an append-only calendar_event_changes row with a reason.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _to_date(v):
    if v is None or isinstance(v, dt.date) and not isinstance(v, dt.datetime):
        return v
    if isinstance(v, dt.datetime):
        return v.date()
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


async def _event(s, tid, event_id):
    return (await s.execute(sa_text(
        "SELECT id::text, subject, start_at, end_at, matter_id::text, event_type, "
        "       lifecycle_state, task_id, docs_status "
        "FROM calendar_events WHERE id=CAST(:e AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
        {"e": event_id, "t": tid})).mappings().fetchone()


async def _log(s, tid, event_id, change_type, *, reason_code=None, reason_note=None,
               from_dt=None, to_dt=None, actor=None, source="ui", detail=None):
    await s.execute(sa_text(
        "INSERT INTO calendar_event_changes "
        "(tenant_id, event_id, change_type, reason_code, reason_note, "
        " from_start_at, to_start_at, actor_user_id, source, detail) "
        "VALUES (:t, CAST(:e AS uuid), :ct, :rc, :rn, :fd, :td, "
        " CAST(:a AS bigint), :src, CAST(:d AS jsonb))"),
        {"t": tid, "e": event_id, "ct": change_type, "rc": reason_code,
         "rn": reason_note, "fd": from_dt, "td": to_dt, "a": actor, "src": source,
         "d": json.dumps(detail or {})})


async def _reason_state(s, tid, reason_code, default="removed"):
    """Map a guided reason code to its lifecycle result_state."""
    if not reason_code:
        return default
    r = (await s.execute(sa_text(
        "SELECT result_state FROM calendar_change_reasons "
        "WHERE code=:c AND is_active AND (tenant_id IS NULL OR TRIM(tenant_id)=TRIM(:t)) "
        "ORDER BY tenant_id NULLS LAST LIMIT 1"),
        {"c": reason_code, "t": tid})).scalar()
    return r or default


# --------------------------------------------------------------------------- #
#  Remove / delete (soft default, crossed out)                                #
# --------------------------------------------------------------------------- #
async def remove_event(tid, event_id, reason_code, reason_note=None, *,
                       hard=False, actor=None, source="ui"):
    """Soft-remove (crossed out, logged) by default; hard=True deletes the row
    but logs a snapshot first so the audit trail and 'what was supposed to
    happen' survive."""
    async with AsyncSessionLocal() as s:
        ev = await _event(s, tid, event_id)
        if not ev:
            return {"error": "not found"}
        snapshot = {"subject": ev["subject"],
                    "start_at": ev["start_at"].isoformat() if ev["start_at"] else None,
                    "matter_id": ev["matter_id"], "event_type": ev["event_type"],
                    "hard": hard}
        if hard:
            await _log(s, tid, event_id, "deleted", reason_code=reason_code,
                       reason_note=reason_note, actor=actor, source=source,
                       detail=snapshot)
            # null the FK reference so the change row (snapshot) survives the delete
            await s.execute(sa_text(
                "UPDATE calendar_event_changes SET event_id=NULL "
                "WHERE event_id=CAST(:e AS uuid)"), {"e": event_id})
            await s.execute(sa_text(
                "DELETE FROM calendar_events WHERE id=CAST(:e AS uuid)"),
                {"e": event_id})
            await s.commit()
            return {"event_id": event_id, "lifecycle_state": "deleted", "hard": True}

        state = await _reason_state(s, tid, reason_code, "removed")
        await s.execute(sa_text(
            "UPDATE calendar_events SET lifecycle_state=:st, "
            " lifecycle_reason_code=:rc, lifecycle_reason_note=:rn, "
            " lifecycle_changed_at=now(), lifecycle_changed_by=CAST(:a AS bigint) "
            "WHERE id=CAST(:e AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
            {"st": state, "rc": reason_code, "rn": reason_note, "a": actor,
             "e": event_id, "t": tid})
        await _log(s, tid, event_id,
                   "cancelled" if state == "cancelled" else "removed",
                   reason_code=reason_code, reason_note=reason_note, actor=actor,
                   source=source, detail=snapshot)
        await s.commit()
        return {"event_id": event_id, "lifecycle_state": state, "hard": False}


async def reinstate_event(tid, event_id, *, actor=None, source="ui"):
    async with AsyncSessionLocal() as s:
        await s.execute(sa_text(
            "UPDATE calendar_events SET lifecycle_state='active', "
            " lifecycle_reason_code=NULL, lifecycle_reason_note=NULL, "
            " lifecycle_changed_at=now(), lifecycle_changed_by=CAST(:a AS bigint) "
            "WHERE id=CAST(:e AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
            {"a": actor, "e": event_id, "t": tid})
        await _log(s, tid, event_id, "reinstated", actor=actor, source=source)
        await s.commit()
        return {"event_id": event_id, "lifecycle_state": "active"}


# --------------------------------------------------------------------------- #
#  Reschedule (supersede old, create new active row)                          #
# --------------------------------------------------------------------------- #
async def reschedule_event(tid, event_id, new_start_at, new_end_at=None,
                           reason_code="reset", reason_note=None, *, actor=None,
                           source="ui"):
    async with AsyncSessionLocal() as s:
        ev = await _event(s, tid, event_id)
        if not ev:
            return {"error": "not found"}
        new_start = dt.datetime.fromisoformat(new_start_at) if isinstance(
            new_start_at, str) else new_start_at
        new_end = (dt.datetime.fromisoformat(new_end_at)
                   if isinstance(new_end_at, str) and new_end_at else new_end_at)
        # new active row carrying the same identity fields
        new_id = (await s.execute(sa_text(
            "INSERT INTO calendar_events "
            "(tenant_id, user_id, mailbox, subject, start_at, end_at, is_all_day, "
            " location, organizer_email, organizer_name, attendees, body_preview, "
            " matter_id, event_type, source_calendar, source, event_type_source, "
            " event_type_confidence, task_id, lifecycle_state) "
            "SELECT tenant_id, user_id, mailbox, subject, CAST(:ns AS timestamptz), "
            " COALESCE(CAST(:ne AS timestamptz), end_at), is_all_day, location, "
            " organizer_email, organizer_name, attendees, body_preview, matter_id, "
            " event_type, source_calendar, 'manual', event_type_source, "
            " event_type_confidence, task_id, 'active' "
            "FROM calendar_events WHERE id=CAST(:e AS uuid) RETURNING id::text"),
            {"ns": new_start, "ne": new_end, "e": event_id})).scalar()
        # old row -> rescheduled (crossed out), pointing at the new row
        await s.execute(sa_text(
            "UPDATE calendar_events SET lifecycle_state='rescheduled', "
            " lifecycle_reason_code=:rc, lifecycle_reason_note=:rn, "
            " superseded_by_event_id=CAST(:n AS uuid), lifecycle_changed_at=now(), "
            " lifecycle_changed_by=CAST(:a AS bigint) "
            "WHERE id=CAST(:e AS uuid)"),
            {"rc": reason_code, "rn": reason_note, "n": new_id, "a": actor,
             "e": event_id})
        await _log(s, tid, event_id, "rescheduled", reason_code=reason_code,
                   reason_note=reason_note, from_dt=ev["start_at"], to_dt=new_start,
                   actor=actor, source=source,
                   detail={"new_event_id": new_id})
        await _log(s, tid, new_id, "created", reason_code=reason_code,
                   from_dt=ev["start_at"], to_dt=new_start, actor=actor,
                   source=source, detail={"superseded_event_id": event_id})
        await s.commit()
        return {"old_event_id": event_id, "new_event_id": new_id,
                "from": ev["start_at"].isoformat() if ev["start_at"] else None,
                "to": new_start.isoformat()}


# --------------------------------------------------------------------------- #
#  Documents prompt outcomes                                                  #
# --------------------------------------------------------------------------- #
async def link_document(tid, event_id, document_id, role=None, *, actor=None):
    async with AsyncSessionLocal() as s:
        await s.execute(sa_text(
            "INSERT INTO calendar_event_documents "
            "(tenant_id, event_id, document_id, role, added_by) "
            "VALUES (:t, CAST(:e AS uuid), CAST(:d AS uuid), :r, CAST(:a AS bigint)) "
            "ON CONFLICT (event_id, document_id) DO NOTHING"),
            {"t": tid, "e": event_id, "d": document_id, "r": role, "a": actor})
        await s.execute(sa_text(
            "UPDATE calendar_events SET docs_status='linked' "
            "WHERE id=CAST(:e AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
            {"e": event_id, "t": tid})
        await _log(s, tid, event_id, "doc_linked", actor=actor,
                   detail={"document_id": document_id, "role": role})
        await s.commit()
        return {"event_id": event_id, "docs_status": "linked"}


async def waive_documents(tid, event_id, mode, *, actor=None):
    """mode: will_add_later | none"""
    status = "will_add_later" if mode == "will_add_later" else "none"
    async with AsyncSessionLocal() as s:
        await s.execute(sa_text(
            "UPDATE calendar_events SET docs_status=:st "
            "WHERE id=CAST(:e AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
            {"st": status, "e": event_id, "t": tid})
        await _log(s, tid, event_id, "docs_waived", actor=actor,
                   detail={"mode": status})
        await s.commit()
        return {"event_id": event_id, "docs_status": status}


# --------------------------------------------------------------------------- #
#  Event <-> task spine (calendar is a rendition of tasks)                     #
# --------------------------------------------------------------------------- #
async def spawn_task(tid, event_id, *, title=None, task_type=None, due_date=None,
                     actor=None):
    """Create a backing task for an event and link it (events render tasks)."""
    async with AsyncSessionLocal() as s:
        ev = await _event(s, tid, event_id)
        if not ev:
            return {"error": "not found"}
        ttype = task_type or (ev["event_type"] or "general")
        due = _to_date(due_date) or (ev["start_at"].date() if ev["start_at"] else None)
        if ev["task_id"]:
            return {"event_id": event_id, "task_id": ev["task_id"], "existing": True}
        task_id = (await s.execute(sa_text(
            "INSERT INTO tasks (tenant_id, matter_id, title, task_type, source, "
            " source_ref, due_date, status, created_by) "
            "VALUES (:t, CAST(:m AS uuid), :ti, :tt, 'calendar_event', :ref, "
            " CAST(:due AS timestamp), 'open', CAST(:a AS bigint)) RETURNING id"),
            {"t": tid, "m": ev["matter_id"], "ti": title or ev["subject"] or "Task",
             "tt": ttype, "ref": event_id, "due": due, "a": actor})).scalar()
        await s.execute(sa_text(
            "UPDATE calendar_events SET task_id=:tk WHERE id=CAST(:e AS uuid)"),
            {"tk": task_id, "e": event_id})
        await _log(s, tid, event_id, "task_created", actor=actor,
                   detail={"task_id": task_id, "task_type": ttype})
        await s.commit()
        return {"event_id": event_id, "task_id": task_id, "existing": False}


async def create_task_only(tid, *, title, task_type="general", due_date=None,
                           matter_id=None, actor=None):
    """The 'just create a task' shortcut (bar renewal, Dr. appt) — no calendar
    event, no workspace. Returns the task id."""
    async with AsyncSessionLocal() as s:
        task_id = (await s.execute(sa_text(
            "INSERT INTO tasks (tenant_id, matter_id, title, task_type, source, "
            " due_date, status, created_by) "
            "VALUES (:t, CAST(:m AS uuid), :ti, :tt, 'manual', "
            " CAST(:due AS timestamp), 'open', CAST(:a AS bigint)) RETURNING id"),
            {"t": tid, "m": matter_id, "ti": title, "tt": task_type,
             "due": _to_date(due_date), "a": actor})).scalar()
        await s.commit()
        return {"task_id": task_id}


# --------------------------------------------------------------------------- #
#  Match-on-add (new stuff goes through the pipeline)                          #
# --------------------------------------------------------------------------- #
async def find_matches(tid, subject, start_at=None, matter_id=None, window_days=120):
    """Candidate existing hearings/events this new event might be a reschedule of
    or duplicate. Scored by matter + type/subject overlap + date proximity."""
    import re
    toks = {w for w in re.findall(r"[a-z]{4,}", (subject or "").lower())
            if w not in {"hearing", "with", "this", "that", "conference", "call"}}
    sdate = None
    if start_at:
        sdate = (dt.datetime.fromisoformat(start_at).date()
                 if isinstance(start_at, str) else start_at)
    async with AsyncSessionLocal() as s:
        params = {"t": tid}
        where = ["TRIM(e.tenant_id)=TRIM(:t)", "e.lifecycle_state <> 'deleted'"]
        if matter_id:
            where.append("e.matter_id=CAST(:m AS uuid)")
            params["m"] = matter_id
        rows = (await s.execute(sa_text(
            "SELECT e.id::text id, e.subject, e.start_at, e.event_type, "
            "       e.lifecycle_state, m.matter_name "
            "FROM calendar_events e LEFT JOIN matters m ON m.id=e.matter_id "
            "WHERE " + " AND ".join(where) +
            " ORDER BY e.start_at DESC NULLS LAST LIMIT 400"),
            params)).mappings().fetchall()
        # also reconciled hearings for the matter
        hrows = []
        if matter_id:
            hrows = (await s.execute(sa_text(
                "SELECT h.id::text id, h.hearing_type, h.current_start_at, "
                "       h.notice_status FROM hearings h "
                "WHERE h.matter_id=CAST(:m AS uuid) AND TRIM(h.tenant_id)=TRIM(:t)"),
                {"m": matter_id, "t": tid})).mappings().fetchall()
    cands = []
    for r in rows:
        rt = {w for w in re.findall(r"[a-z]{4,}", (r["subject"] or "").lower())}
        overlap = len(toks & rt)
        if overlap == 0:
            continue
        score = min(0.5, 0.18 * overlap)
        if sdate and r["start_at"]:
            dd = abs((r["start_at"].date() - sdate).days)
            if dd <= window_days:
                score += 0.4 * (1 - dd / window_days)
        if score >= 0.3:
            cands.append({"kind": "event", "id": r["id"], "subject": r["subject"],
                          "start_at": r["start_at"], "event_type": r["event_type"],
                          "lifecycle_state": r["lifecycle_state"],
                          "matter_name": r["matter_name"], "score": round(score, 2)})
    for h in hrows:
        ht = {w for w in re.findall(r"[a-z]{4,}", (h["hearing_type"] or "").lower())}
        if toks & ht:
            cands.append({"kind": "hearing", "id": h["id"],
                          "hearing_type": h["hearing_type"],
                          "current_start_at": h["current_start_at"],
                          "notice_status": h["notice_status"], "score": 0.6})
    cands.sort(key=lambda c: c["score"], reverse=True)
    return {"matches": cands[:8]}


# --------------------------------------------------------------------------- #
#  Workspace dispatch (calendar = entry point; §2 projector, data-driven)      #
# --------------------------------------------------------------------------- #
async def _ensure_meeting_workspace(s, tid, ev):
    """Idempotent meeting-workspace projection: a meeting-type calendar event
    gets exactly one meeting_workspaces row (keyed by calendar_event_id),
    mirroring the hearing projection. Returns the workspace id."""
    eid = ev["id"]
    wid = (await s.execute(sa_text(
        "SELECT id::text FROM meeting_workspaces "
        "WHERE calendar_event_id=CAST(:e AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
        "ORDER BY created_at LIMIT 1"),
        {"e": eid, "t": tid})).scalar()
    if wid:
        return wid
    title = ((ev.get("subject") or "").strip() or "Meeting")
    matter_id = ev.get("matter_id")
    matter_frag = "CAST(:m AS uuid)" if matter_id else "NULL"
    params = {"t": tid, "e": eid, "title": title, "sched": ev.get("start_at")}
    if matter_id:
        params["m"] = matter_id
    wid = (await s.execute(sa_text(
        "INSERT INTO meeting_workspaces "
        "(tenant_id, matter_id, calendar_event_id, title, workspace_type, status, scheduled_at) "
        "VALUES (TRIM(:t), " + matter_frag + ", CAST(:e AS uuid), :title, 'meeting', 'active', :sched) "
        "RETURNING id::text"), params)).scalar()
    await s.commit()
    return wid


async def resolve_workspace(tid, event_id):
    """Given an event, dispatch by event_type to the workspace its issue lives
    in (data-driven via event_workspace_dispatch). Returns the target + URL."""
    async with AsyncSessionLocal() as s:
        ev = await _event(s, tid, event_id)
        if not ev:
            return {"error": "not found"}
        disp = (await s.execute(sa_text(
            "SELECT workspace_kind, label, auto_task FROM event_workspace_dispatch "
            "WHERE event_type=:et AND is_active "
            "AND (tenant_id IS NULL OR TRIM(tenant_id)=TRIM(:t)) "
            "ORDER BY tenant_id NULLS LAST LIMIT 1"),
            {"et": ev["event_type"], "t": tid})).mappings().fetchone()
        kind = disp["workspace_kind"] if disp else "task"
        out = {"event_id": event_id, "event_type": ev["event_type"],
               "workspace_kind": kind, "label": disp["label"] if disp else "Task",
               "matter_id": ev["matter_id"], "target_id": None, "url": None}
        if kind in ("hearing", "court_dashboard") and ev["matter_id"]:
            hid = (await s.execute(sa_text(
                "SELECT id::text FROM hearings WHERE matter_id=CAST(:m AS uuid) "
                "AND TRIM(tenant_id)=TRIM(:t) AND current_event_id=CAST(:e AS uuid) "
                "LIMIT 1"), {"m": ev["matter_id"], "t": tid, "e": event_id})).scalar()
            out["target_id"] = hid
            out["url"] = (f"/api/v1/reconcile/hearing/{hid}" if hid
                          else f"/api/v1/reconcile/matter/{ev['matter_id']}/hearings")
        elif kind == "meeting":
            wid = await _ensure_meeting_workspace(s, tid, ev)
            out["target_id"] = wid
            out["url"] = f"/workspaces/{wid}"
        elif kind == "deposition" and ev["matter_id"]:
            out["url"] = f"/depositions/home/{ev['matter_id']}"
        elif kind == "task":
            out["target_id"] = ev["task_id"]
            out["url"] = "/tasks"
        return out


# --------------------------------------------------------------------------- #
#  Reads                                                                       #
# --------------------------------------------------------------------------- #
async def change_log(tid, event_id):
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(sa_text(
            "SELECT change_type, reason_code, reason_note, from_start_at, "
            "       to_start_at, actor_user_id, source, detail, created_at "
            "FROM calendar_event_changes WHERE event_id=CAST(:e AS uuid) "
            "ORDER BY created_at"), {"e": event_id})).mappings().fetchall()
        docs = (await s.execute(sa_text(
            "SELECT document_id::text, role, created_at FROM calendar_event_documents "
            "WHERE event_id=CAST(:e AS uuid) ORDER BY created_at"),
            {"e": event_id})).mappings().fetchall()
        return {"event_id": event_id, "changes": [dict(r) for r in rows],
                "documents": [dict(d) for d in docs]}


async def list_reasons(tid):
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(sa_text(
            "SELECT code, label, result_state, applies_to, sort_order "
            "FROM calendar_change_reasons WHERE is_active "
            "AND (tenant_id IS NULL OR TRIM(tenant_id)=TRIM(:t)) "
            "ORDER BY sort_order, label"), {"t": tid})).mappings().fetchall()
        # tenant override wins on duplicate code
        seen, out = set(), []
        for r in rows:
            if r["code"] in seen:
                continue
            seen.add(r["code"])
            out.append(dict(r))
        return {"reasons": out}
