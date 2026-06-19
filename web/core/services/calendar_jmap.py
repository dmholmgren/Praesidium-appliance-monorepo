"""
calendar_jmap.py - live JMAP CalendarEvent reads for the dashboard calendar.

Firm Stalwart v1.0.0 advertises urn:ietf:params:jmap:calendars and answers
CalendarEvent/query+get. Maps JSCalendar (RFC 8984) events -> the event dict
the calendar frontend already consumes. Admin-auth, accountId resolution.

Used additively alongside the legacy calendar_events table so the visible
calendar never blanks while events migrate into Stalwart.
"""
from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

from core.services import stalwart_mailbox as sm
from core.services.matter_mail_folders import _account_id

CAL_USING = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:calendars"]

_DUR = re.compile(
    r"P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?")


def _add_duration(start_iso: Optional[str], dur: Optional[str]) -> Optional[str]:
    if not start_iso:
        return None
    base = start_iso
    try:
        dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except Exception:
        return base
    if not dur:
        return start_iso
    m = _DUR.fullmatch(dur)
    if not m:
        return start_iso
    g = {k: int(v) if v else 0 for k, v in m.groupdict().items()}
    dt = dt + timedelta(days=g["d"], hours=g["h"], minutes=g["m"], seconds=g["s"])
    return dt.isoformat()


def _organizer(participants: dict):
    if not participants:
        return None, None
    for p in participants.values():
        roles = p.get("roles") or {}
        if roles.get("owner") or roles.get("organizer"):
            return (p.get("name"), p.get("email"))
    # fall back to first participant
    first = next(iter(participants.values()), {})
    return first.get("name"), first.get("email")


def _location(locations: dict):
    if not locations:
        return None
    for loc in locations.values():
        if loc.get("name"):
            return loc.get("name")
    return None


def event_to_row(ev: dict, *, mailbox: str) -> dict:
    name, email = _organizer(ev.get("participants") or {})
    attendees = [p.get("name") or p.get("email")
                 for p in (ev.get("participants") or {}).values()
                 if p.get("name") or p.get("email")]
    start = ev.get("start")
    return {
        "id": ev.get("id"),
        "title": ev.get("title") or "(No Subject)",
        "start": start,
        "end": _add_duration(start, ev.get("duration")),
        "all_day": bool(ev.get("showWithoutTime")),
        "location": _location(ev.get("locations") or {}),
        "mailbox": mailbox,
        "organizer": name or email,
        "attendees": attendees,
        "body_preview": (ev.get("description") or "")[:200],
        "recurring": bool(ev.get("recurrenceRules")),
        "source": "stalwart",
        "source_calendar": "stalwart",
        "matter_id": None,
        "matter_name": None,
        "client_name": None,
        "calendar_type": "stalwart",
    }


async def events_for(user_email: str, start_iso: str, end_iso: str, *,
                     auth=None, account_id=None) -> list[dict]:
    """Live JMAP events for one account within [start, end)."""
    acct = account_id or await _account_id(user_email)
    if not acct:
        return []
    filt = {"operator": "AND", "conditions": [
        {"after": start_iso}, {"before": end_iso}]}
    try:
        r = await sm._jmap([
            ["CalendarEvent/query",
             {"accountId": acct, "filter": filt, "limit": 1000}, "q"],
            ["CalendarEvent/get",
             {"accountId": acct,
              "#ids": {"resultOf": "q", "name": "CalendarEvent/query",
                       "path": "/ids/*"}}, "g"],
        ], using=CAL_USING, auth=auth)
    except Exception:
        return []
    lst = sm._resp(r, 1).get("list", [])
    return [event_to_row(ev, mailbox=user_email) for ev in lst]
