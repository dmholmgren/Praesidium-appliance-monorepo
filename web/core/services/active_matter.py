"""core/services/active_matter.py
Shared reader for the user's sticky "active matter" (the topbar picker
selection). Single source of truth: users.user_preferences JSONB keys
active_matter_id + active_matter_set_at (also written by the desktop/VSTO
client, modules/desktop/desktop_c3_router.py).

Used by BOTH:
  - GET /api/v1/active-matter (matter_dashboard_api.web_get_active_matter)
  - core.services.nav_context.get_nav_context (synchronous shell.html seed)
so the two reads can never drift.

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

_ACTIVE_MATTER_STALE_HOURS = 12


async def read_active_matter(uid, tid):
    """Return the user's active matter as a dict, or None.

    Shape (matches GET /api/v1/active-matter -> .active_matter):
      {matter_id, matter_name, matter_number, client_name, set_at, is_stale}
    """
    tid = (tid or "").strip()
    if not tid or not uid:
        return None
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        return None

    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text("""
            SELECT u.user_preferences->>'active_matter_id'     AS matter_id,
                   u.user_preferences->>'active_matter_set_at' AS set_at,
                   m.matter_name, m.matter_number, c.client_name
            FROM users u
            LEFT JOIN matters m
              ON m.id = CAST(NULLIF(u.user_preferences->>'active_matter_id','') AS uuid)
             AND TRIM(m.tenant_id) = :tid
            LEFT JOIN clients c ON c.id = m.client_id
            WHERE u.id = :uid AND TRIM(u.tenant_id) = :tid
        """), {"uid": uid_int, "tid": tid})
        row = r.mappings().first()

    if not row or not row.get("matter_id") or not row.get("matter_name"):
        return None

    is_stale = False
    set_at = row.get("set_at")
    if set_at:
        try:
            dt = datetime.fromisoformat(set_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
            is_stale = age_h >= _ACTIVE_MATTER_STALE_HOURS
        except Exception:
            is_stale = False

    return {
        "matter_id": row["matter_id"],
        "matter_name": row.get("matter_name"),
        "matter_number": row.get("matter_number") or "",
        "client_name": row.get("client_name") or "",
        "set_at": set_at,
        "is_stale": is_stale,
    }
