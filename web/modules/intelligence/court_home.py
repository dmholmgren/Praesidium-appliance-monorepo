"""court_home.py — the Court homepage aggregator ("the information you need in
one place, in a usable format").

One call returns everything the Court surface shows, drawn from the units already
built: reconstructed hearings + reset chains (reconciler), the routing alert inbox
(routing), motion workspaces, and the crossed-out calendar events ("what was
supposed to happen but didn't"). Read-only; the page mutates via the existing
/reconcile and /routing endpoints.
"""
from __future__ import annotations

import logging

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.intelligence import routing as _routing

logger = logging.getLogger(__name__)
DEFAULT_TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"


async def court_home(tid=DEFAULT_TENANT):
    async with AsyncSessionLocal() as s:
        hearings = (await s.execute(sa_text(
            "SELECT h.id::text, h.hearing_type, h.status, h.notice_status, "
            "       h.current_start_at, h.original_start_at, h.judge, h.confidence, "
            "       m.matter_name, m.id::text matter_id, "
            "       (SELECT count(*) FROM hearing_reschedules r WHERE r.hearing_id=h.id) reset_count "
            "FROM hearings h JOIN matters m ON m.id=h.matter_id "
            "WHERE TRIM(h.tenant_id)=TRIM(:t) "
            "ORDER BY h.current_start_at NULLS LAST"),
            {"t": tid})).mappings().fetchall()

        motions = (await s.execute(sa_text(
            "SELECT mw.id::text, mw.title, mw.motion_type, mw.status, "
            "       mw.hearing_id::text, mw.response_document_id IS NOT NULL has_response, "
            "       m.matter_name, m.id::text matter_id, h.current_start_at hearing_date "
            "FROM motion_workspaces mw JOIN matters m ON m.id=mw.matter_id "
            "LEFT JOIN hearings h ON h.id=mw.hearing_id "
            "WHERE TRIM(mw.tenant_id)=TRIM(:t) ORDER BY mw.created_at DESC"),
            {"t": tid})).mappings().fetchall()

        # crossed-out events — "what was supposed to happen but didn't"
        whatif = (await s.execute(sa_text(
            "SELECT e.id::text, e.subject, e.start_at, e.event_type, "
            "       e.lifecycle_state, e.lifecycle_reason_code, e.lifecycle_reason_note, "
            "       m.matter_name "
            "FROM calendar_events e LEFT JOIN matters m ON m.id=e.matter_id "
            "WHERE TRIM(e.tenant_id)=TRIM(:t) "
            "  AND e.lifecycle_state IN ('cancelled','missed','rescheduled','removed') "
            "ORDER BY e.start_at DESC NULLS LAST LIMIT 60"),
            {"t": tid})).mappings().fetchall()

    inbox = await _routing.surface_alerts(tid)   # open + returned-from-snooze
    H = [dict(h) for h in hearings]
    M = [dict(m) for m in motions]
    W = [dict(w) for w in whatif]
    stats = {
        "hearings": len(H),
        "reset_hearings": sum(1 for h in H if (h["reset_count"] or 0) > 0),
        "total_resets": sum((h["reset_count"] or 0) for h in H),
        "open_alerts": inbox["count"],
        "motions": len(M),
        "motions_no_hearing": sum(1 for m in M if not m["hearing_id"]),
        "pending_notice": sum(1 for h in H if h["notice_status"] == "pending"),
    }
    return {"stats": stats, "hearings": H, "motions": M,
            "alerts": inbox["alerts"], "what_happened": W}
