"""
COMP 7 — Dual Calendar Cross-Check Agent.

Daily RQ job on PROC-01 at 7:00 AM. Compares system-generated calendar
vs admin-controlled calendar within a configurable future window.

Detects:
  (a) Events in system calendar but missing from admin calendar
  (b) Events in admin calendar but missing from system calendar (unusual — flagged)
  (c) Events in both but with conflicting dates/times — CRITICAL severity

Date conflicts generate immediate notifications to all assigned timekeepers
and administrative staff.

All external calls via CalendarService and EmailService interfaces.
All DB writes via write_audit().
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from core.audit import write_audit
from core.db.tenant_session import TenantSession
from core.services.calendar import CalendarService
from core.services.email import EmailService
from modules.court.models import CalendarCrossCheckLog

logger = logging.getLogger(__name__)

# Default look-ahead window in days
DEFAULT_WINDOW_DAYS = 90

# Similarity threshold for matching event titles
TITLE_SIMILARITY_THRESHOLD = 0.75


def _normalize_title(title: str) -> str:
    """Normalize event title for comparison."""
    import re
    # Remove priority tags, brackets, extra whitespace
    title = re.sub(r"\[.*?\]", "", title)
    title = re.sub(r"\s+", " ", title)
    return title.strip().lower()


def _titles_match(system_title: str, admin_title: str) -> bool:
    """Check if two event titles refer to the same deadline."""
    norm_sys = _normalize_title(system_title)
    norm_adm = _normalize_title(admin_title)

    # Exact match after normalization
    if norm_sys == norm_adm:
        return True

    # One contains the other
    if norm_sys in norm_adm or norm_adm in norm_sys:
        return True

    # Simple token overlap
    sys_tokens = set(norm_sys.split())
    adm_tokens = set(norm_adm.split())
    if not sys_tokens or not adm_tokens:
        return False
    overlap = len(sys_tokens & adm_tokens) / max(len(sys_tokens), len(adm_tokens))
    return overlap >= TITLE_SIMILARITY_THRESHOLD


async def run_cross_check(
    tenant_id: str,
    db: TenantSession,
    calendar_service: CalendarService,
    email_service: EmailService,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> CalendarCrossCheckLog:
    """
    Execute the daily dual calendar cross-check.

    Compares all events in the system-generated and admin-controlled
    calendars within the configured future window.
    """
    today = date.today()
    window_end = today + timedelta(days=window_days)

    # Get calendar IDs from tenant config
    from modules.court.services.calendar_creator import (
        _get_admin_calendar_id,
        _get_system_calendar_id,
    )

    system_cal_id = _get_system_calendar_id(tenant_id, db)
    admin_cal_id = _get_admin_calendar_id(tenant_id, db)

    # Fetch events from both calendars via CalendarService
    system_events = await calendar_service.get_events(
        tenant_id=tenant_id,
        calendar_id=system_cal_id,
        start=today,
        end=window_end,
    )

    admin_events = await calendar_service.get_events(
        tenant_id=tenant_id,
        calendar_id=admin_cal_id,
        start=today,
        end=window_end,
    )

    # Cross-check logic
    missing_in_admin = []
    missing_in_system = []
    date_conflicts = []

    # Check: system events missing from admin
    for sys_event in system_events:
        sys_title = sys_event.get("subject", "")
        sys_date = sys_event.get("start", "")[:10]  # YYYY-MM-DD

        matched = False
        for adm_event in admin_events:
            adm_title = adm_event.get("subject", "")
            adm_date = adm_event.get("start", "")[:10]

            if _titles_match(sys_title, adm_title):
                matched = True
                # Check for date conflict
                if sys_date != adm_date:
                    date_conflicts.append({
                        "system_title": sys_title,
                        "admin_title": adm_title,
                        "system_date": sys_date,
                        "admin_date": adm_date,
                        "severity": "critical",
                    })
                break

        if not matched:
            missing_in_admin.append({
                "title": sys_title,
                "date": sys_date,
                "severity": "warning",
            })

    # Check: admin events missing from system (unusual — flag)
    for adm_event in admin_events:
        adm_title = adm_event.get("subject", "")
        adm_date = adm_event.get("start", "")[:10]

        matched = any(
            _titles_match(adm_title, se.get("subject", ""))
            for se in system_events
        )
        if not matched:
            missing_in_system.append({
                "title": adm_title,
                "date": adm_date,
                "severity": "info",
            })

    total_discrepancies = len(missing_in_admin) + len(missing_in_system) + len(date_conflicts)
    critical_count = len(date_conflicts)

    # Create log record
    log_entry = CalendarCrossCheckLog(
        tenant_id=tenant_id,
        check_date=today,
        window_start=today,
        window_end=window_end,
        system_event_count=len(system_events),
        admin_event_count=len(admin_events),
        missing_in_admin=missing_in_admin if missing_in_admin else None,
        missing_in_system=missing_in_system if missing_in_system else None,
        date_conflicts=date_conflicts if date_conflicts else None,
        total_discrepancies=total_discrepancies,
        critical_count=critical_count,
    )
    db.add(log_entry)
    db.flush()

    write_audit(
        tenant_id=tenant_id,
        table_name="calendar_cross_check_log",
        record_id=log_entry.id,
        action="cross_check_completed",
        details={
            "total_discrepancies": total_discrepancies,
            "critical_count": critical_count,
            "system_events": len(system_events),
            "admin_events": len(admin_events),
        },
    )

    # Send notifications
    if total_discrepancies > 0:
        await _send_cross_check_notifications(
            tenant_id=tenant_id,
            log_entry=log_entry,
            date_conflicts=date_conflicts,
            missing_in_admin=missing_in_admin,
            missing_in_system=missing_in_system,
            db=db,
            email_service=email_service,
        )
        log_entry.digest_sent = True
        log_entry.digest_sent_at = datetime.now(timezone.utc)

    logger.info(
        f"Cross-check complete: {total_discrepancies} discrepancies "
        f"({critical_count} critical) for tenant {tenant_id}"
    )
    return log_entry


async def _send_cross_check_notifications(
    tenant_id: str,
    log_entry: CalendarCrossCheckLog,
    date_conflicts: list[dict],
    missing_in_admin: list[dict],
    missing_in_system: list[dict],
    db: TenantSession,
    email_service: EmailService,
) -> None:
    """Send notification emails for cross-check discrepancies."""
    # Get notification recipients from tenant config
    recipients = _get_notification_recipients(tenant_id, db)
    if not recipients:
        logger.warning(f"No cross-check notification recipients configured for tenant {tenant_id}")
        return

    # Build digest email
    subject_prefix = "CRITICAL: " if date_conflicts else ""
    subject = f"{subject_prefix}Calendar Cross-Check — {log_entry.total_discrepancies} Discrepancies Found"

    body_parts = [f"Calendar Cross-Check Report for {log_entry.check_date.isoformat()}",
                  f"Window: {log_entry.window_start} to {log_entry.window_end}\n"]

    if date_conflicts:
        body_parts.append("=== DATE CONFLICTS (CRITICAL) ===")
        for conflict in date_conflicts:
            body_parts.append(
                f"  Event: {conflict['system_title']}\n"
                f"  System Date: {conflict['system_date']}\n"
                f"  Admin Date: {conflict['admin_date']}\n"
            )

    if missing_in_admin:
        body_parts.append(f"\n=== Missing from Admin Calendar ({len(missing_in_admin)}) ===")
        for item in missing_in_admin:
            body_parts.append(f"  {item['date']}: {item['title']}")

    if missing_in_system:
        body_parts.append(f"\n=== In Admin Only (not in system) ({len(missing_in_system)}) ===")
        for item in missing_in_system:
            body_parts.append(f"  {item['date']}: {item['title']}")

    body = "\n".join(body_parts)

    # Send digest
    await email_service.send(
        tenant_id=tenant_id,
        user_id=0,  # System sender
        to=recipients,
        subject=subject,
        body=body,
    )

    # For critical date conflicts, also send immediate separate alerts
    if date_conflicts:
        for conflict in date_conflicts:
            await email_service.send(
                tenant_id=tenant_id,
                user_id=0,
                to=recipients,
                subject=f"CRITICAL DATE CONFLICT: {conflict['system_title']}",
                body=(
                    f"A critical date conflict has been detected.\n\n"
                    f"Event: {conflict['system_title']}\n"
                    f"System calendar date: {conflict['system_date']}\n"
                    f"Admin calendar date: {conflict['admin_date']}\n\n"
                    f"This requires immediate resolution."
                ),
            )


def _get_notification_recipients(tenant_id: str, db: TenantSession) -> list[str]:
    """Get email addresses for cross-check notifications from tenant config."""
    config = db.query_first(
        "tenant_config",
        filters={"config_key": "cross_check_recipients"},
    )
    if config and config.config_value:
        return [email.strip() for email in config.config_value.split(",") if email.strip()]
    return []
