"""
COMP 6 — Calendar Event Creator.

Creates events in the system-generated shared calendar via CalendarService.
Calendar IDs from tenant config table — never hardcoded.

System-generated calendar is read-only for all human users.
Written exclusively by this module.

All external calls via CalendarService interface.
All DB writes via write_audit().
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from core.audit import write_audit
from core.db.base import TenantSession
from core.services.calendar import CalendarService
from modules.court.models import Deadline

logger = logging.getLogger(__name__)

# Default reminder intervals (minutes before event)
DEFAULT_REMINDERS = [
    7 * 24 * 60,   # 7 days
    3 * 24 * 60,   # 3 days
    1 * 24 * 60,   # 1 day
    60,             # 1 hour
]

# Priority-specific reminder overrides
PRIORITY_REMINDERS = {
    "critical": [14 * 24 * 60, 7 * 24 * 60, 3 * 24 * 60, 1 * 24 * 60, 2 * 60, 60],
    "high": [7 * 24 * 60, 3 * 24 * 60, 1 * 24 * 60, 2 * 60],
    "normal": DEFAULT_REMINDERS,
    "low": [3 * 24 * 60, 1 * 24 * 60],
}

# Priority color mapping (used if calendar supports it)
PRIORITY_COLORS = {
    "critical": "#FF0000",
    "high": "#FF8C00",
    "normal": "#0078D4",
    "low": "#808080",
}


def _get_system_calendar_id(tenant_id: str, db: TenantSession) -> str:
    """
    Get the system-generated calendar ID from tenant config.

    Calendar IDs are stored in the tenant configuration table,
    never hardcoded in application code.
    """
    config = db.query_first(
        "tenant_config",
        filters={"config_key": "system_calendar_id"},
    )
    if not config:
        raise ValueError(
            f"System calendar ID not configured for tenant {tenant_id}. "
            "Set 'system_calendar_id' in tenant_config table."
        )
    return config.config_value


def _get_admin_calendar_id(tenant_id: str, db: TenantSession) -> str:
    """Get the admin-controlled calendar ID from tenant config."""
    config = db.query_first(
        "tenant_config",
        filters={"config_key": "admin_calendar_id"},
    )
    if not config:
        raise ValueError(
            f"Admin calendar ID not configured for tenant {tenant_id}. "
            "Set 'admin_calendar_id' in tenant_config table."
        )
    return config.config_value


def _build_event_body(deadline: Deadline) -> str:
    """Build a detailed event description from a deadline record."""
    lines = [
        f"Deadline: {deadline.deadline_description}",
        f"Rule Basis: {deadline.derivation_path.get('rule_number', 'N/A') if deadline.derivation_path else 'Manual'}",
        f"Anchor Date: {deadline.anchor_date.isoformat()} — {deadline.anchor_description or 'N/A'}",
    ]

    if deadline.derivation_path:
        path = deadline.derivation_path
        lines.append(f"Duration: {path.get('duration_days', '?')} {path.get('duration_type', 'calendar')} days {path.get('direction', 'after')} anchor")
        if deadline.service_method:
            lines.append(f"Service Method: {deadline.service_method} (+{deadline.service_adjustment_days} days)")
        if path.get("depth", 0) > 0:
            lines.append(f"Derivative chain depth: {path['depth']}")

    lines.append(f"\nStatus: {deadline.status}")
    lines.append(f"Priority: {deadline.priority}")

    return "\n".join(lines)


async def create_deadline_calendar_event(
    tenant_id: str,
    deadline: Deadline,
    db: TenantSession,
    calendar_service: CalendarService,
) -> str:
    """
    Create a calendar event for a deadline in the system-generated calendar.

    Returns the calendar event ID.
    """
    calendar_id = _get_system_calendar_id(tenant_id, db)

    # Build event payload
    reminders = PRIORITY_REMINDERS.get(deadline.priority, DEFAULT_REMINDERS)

    event = {
        "subject": f"[{deadline.priority.upper()}] {deadline.deadline_description}",
        "body": _build_event_body(deadline),
        "start": datetime.combine(deadline.deadline_date, time(9, 0), tzinfo=timezone.utc).isoformat(),
        "end": datetime.combine(deadline.deadline_date, time(17, 0), tzinfo=timezone.utc).isoformat(),
        "is_all_day": True,
        "categories": [f"Priority: {deadline.priority}", "Court Deadline"],
        "reminders": [{"minutes": m} for m in reminders],
        "is_read_only": True,
        "metadata": {
            "deadline_id": deadline.id,
            "matter_id": deadline.matter_id,
            "rule_id": deadline.rule_id,
            "source": "deadline_calculator",
        },
    }

    # Optional color
    color = PRIORITY_COLORS.get(deadline.priority)
    if color:
        event["color"] = color

    # Create via CalendarService interface (Graph/Google adapter)
    event_id = await calendar_service.create_event(
        tenant_id=tenant_id,
        calendar_id=calendar_id,
        event=event,
    )

    # Update deadline record with calendar event ID
    deadline.calendar_event_id = event_id
    db.flush()

    write_audit(
        tenant_id=tenant_id,
        table_name="deadlines",
        record_id=deadline.id,
        action="calendar_event_created",
        details={"calendar_event_id": event_id, "calendar_id": calendar_id},
    )

    logger.info(f"Created calendar event {event_id} for deadline {deadline.id}")
    return event_id


async def create_events_for_chain(
    tenant_id: str,
    deadlines: list[Deadline],
    db: TenantSession,
    calendar_service: CalendarService,
) -> list[str]:
    """Create calendar events for an entire deadline chain."""
    event_ids = []
    for deadline in deadlines:
        try:
            event_id = await create_deadline_calendar_event(
                tenant_id=tenant_id,
                deadline=deadline,
                db=db,
                calendar_service=calendar_service,
            )
            event_ids.append(event_id)
        except Exception as e:
            logger.error(f"Failed to create calendar event for deadline {deadline.id}: {e}")
    return event_ids


async def update_deadline_calendar_event(
    tenant_id: str,
    deadline: Deadline,
    db: TenantSession,
    calendar_service: CalendarService,
) -> None:
    """Update an existing calendar event when a deadline changes."""
    if not deadline.calendar_event_id:
        return

    calendar_id = _get_system_calendar_id(tenant_id, db)

    updates = {
        "subject": f"[{deadline.priority.upper()}] {deadline.deadline_description}",
        "body": _build_event_body(deadline),
        "start": datetime.combine(deadline.deadline_date, time(9, 0), tzinfo=timezone.utc).isoformat(),
        "end": datetime.combine(deadline.deadline_date, time(17, 0), tzinfo=timezone.utc).isoformat(),
    }

    if deadline.status in ("completed", "vacated"):
        updates["subject"] = f"[{deadline.status.upper()}] {deadline.deadline_description}"

    await calendar_service.update_event(
        tenant_id=tenant_id,
        calendar_id=calendar_id,
        event_id=deadline.calendar_event_id,
        updates=updates,
    )

    write_audit(
        tenant_id=tenant_id,
        table_name="deadlines",
        record_id=deadline.id,
        action="calendar_event_updated",
        details={"calendar_event_id": deadline.calendar_event_id},
    )
