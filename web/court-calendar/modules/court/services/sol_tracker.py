"""
COMP 10 — Statute of Limitations Tracker.

Malpractice prevention module — highest priority safety module.

SOL records:
  - Cannot be deleted — only super admin can deactivate with documented reason
  - Multiple records per matter (different claims)
  - Tolling support: is_tolled, toll_reason, toll_start, toll_end
  - Escalating alerts: 180, 90, 60, 30, 14, 7 days before expiration
  - At 7 days: daily URGENT email to all assigned timekeepers + super admin

RQ job on PROC-01 daily at 6:00 AM.
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
from modules.court.models import SOLRecord

logger = logging.getLogger(__name__)

# Alert thresholds in days before expiration
ALERT_THRESHOLDS = [180, 90, 60, 30, 14, 7]

# At this threshold and below, send URGENT daily alerts
URGENT_THRESHOLD = 7


def compute_expiration_date(
    accrual_date: date,
    limitation_period_days: int,
    is_tolled: bool = False,
    tolled_days: int = 0,
) -> date:
    """Compute the SOL expiration date accounting for tolling."""
    base_expiration = accrual_date + timedelta(days=limitation_period_days)
    if is_tolled and tolled_days > 0:
        base_expiration += timedelta(days=tolled_days)
    return base_expiration


async def create_sol_record(
    tenant_id: str,
    matter_id: int,
    claim_description: str,
    cause_of_action: str,
    jurisdiction: str,
    limitation_period_days: int,
    accrual_date: date,
    created_by_user_id: int,
    db: TenantSession,
    calendar_service: Optional[CalendarService] = None,
) -> SOLRecord:
    """
    Create a new SOL record for a matter.

    Multiple SOL records per matter are allowed (different claims).
    This record can never be deleted — only deactivated by super admin.
    """
    expiration = compute_expiration_date(accrual_date, limitation_period_days)

    record = SOLRecord(
        tenant_id=tenant_id,
        matter_id=matter_id,
        claim_description=claim_description,
        cause_of_action=cause_of_action,
        jurisdiction=jurisdiction,
        limitation_period_days=limitation_period_days,
        accrual_date=accrual_date,
        computed_expiration_date=expiration,
        status="active",
        created_by_user_id=created_by_user_id,
    )
    db.add(record)
    db.flush()

    write_audit(
        tenant_id=tenant_id,
        table_name="sol_records",
        record_id=record.id,
        action="create",
        details={
            "matter_id": matter_id,
            "cause_of_action": cause_of_action,
            "accrual_date": accrual_date.isoformat(),
            "expiration_date": expiration.isoformat(),
            "limitation_period_days": limitation_period_days,
        },
    )

    # Create calendar event for expiration date
    if calendar_service:
        try:
            from modules.court.services.calendar_creator import _get_system_calendar_id
            cal_id = _get_system_calendar_id(tenant_id, db)
            event_id = await calendar_service.create_event(
                tenant_id=tenant_id,
                calendar_id=cal_id,
                event={
                    "subject": f"[CRITICAL] SOL EXPIRATION: {cause_of_action}",
                    "body": f"Statute of limitations expires for: {claim_description}",
                    "start": datetime.combine(expiration, datetime.min.time(), tzinfo=timezone.utc).isoformat(),
                    "end": datetime.combine(expiration, datetime.min.time(), tzinfo=timezone.utc).isoformat(),
                    "is_all_day": True,
                    "categories": ["SOL Expiration", "Critical"],
                    "reminders": [{"minutes": d * 24 * 60} for d in ALERT_THRESHOLDS],
                },
            )
            record.calendar_event_id = event_id
            db.flush()
        except Exception as e:
            logger.error(f"Failed to create SOL calendar event: {e}")

    logger.info(f"SOL record created: {record.id} expires {expiration} for matter {matter_id}")
    return record


async def update_tolling(
    tenant_id: str,
    sol_record_id: int,
    is_tolled: bool,
    toll_reason: Optional[str],
    toll_start: Optional[date],
    toll_end: Optional[date],
    user_id: int,
    db: TenantSession,
) -> SOLRecord:
    """Update tolling status on an SOL record. Recomputes expiration."""
    record = db.query_first(SOLRecord, filters={"id": sol_record_id})
    if not record:
        raise ValueError(f"SOL record {sol_record_id} not found")
    if record.status == "deactivated":
        raise ValueError(f"SOL record {sol_record_id} is deactivated")

    record.is_tolled = is_tolled
    record.toll_reason = toll_reason
    record.toll_start = toll_start
    record.toll_end = toll_end

    # Compute tolled days
    if is_tolled and toll_start and toll_end:
        record.tolled_days = (toll_end - toll_start).days
    elif is_tolled and toll_start and not toll_end:
        record.tolled_days = (date.today() - toll_start).days
    else:
        record.tolled_days = 0

    # Recompute expiration
    record.computed_expiration_date = compute_expiration_date(
        record.accrual_date, record.limitation_period_days,
        record.is_tolled, record.tolled_days,
    )

    write_audit(
        tenant_id=tenant_id,
        table_name="sol_records",
        record_id=record.id,
        action="update_tolling",
        details={
            "is_tolled": is_tolled,
            "toll_reason": toll_reason,
            "tolled_days": record.tolled_days,
            "new_expiration": record.computed_expiration_date.isoformat(),
            "updated_by": user_id,
        },
    )

    db.flush()
    return record


async def deactivate_sol_record(
    tenant_id: str,
    sol_record_id: int,
    deactivation_reason: str,
    user_id: int,
    user_role: str,
    db: TenantSession,
) -> SOLRecord:
    """
    Deactivate an SOL record. Super admin only.

    SOL records cannot be deleted — only deactivated with documented reason.
    """
    if user_role != "super_admin":
        raise PermissionError("Only super_admin can deactivate SOL records")

    record = db.query_first(SOLRecord, filters={"id": sol_record_id})
    if not record:
        raise ValueError(f"SOL record {sol_record_id} not found")

    if not deactivation_reason or len(deactivation_reason.strip()) < 10:
        raise ValueError("Deactivation reason must be documented (minimum 10 characters)")

    record.status = "deactivated"
    record.deactivated_by_user_id = user_id
    record.deactivation_reason = deactivation_reason
    record.deactivated_at = datetime.now(timezone.utc)

    write_audit(
        tenant_id=tenant_id,
        table_name="sol_records",
        record_id=record.id,
        action="deactivate",
        details={
            "deactivated_by": user_id,
            "reason": deactivation_reason,
        },
    )

    db.flush()
    logger.info(f"SOL record {sol_record_id} deactivated by super_admin {user_id}")
    return record


async def run_sol_alert_check(
    tenant_id: str,
    db: TenantSession,
    email_service: EmailService,
) -> dict:
    """
    Daily SOL alert check — runs as RQ job at 6:00 AM on PROC-01.

    For each active SOL record:
      - Calculate days remaining
      - Send alerts at 180, 90, 60, 30, 14, 7 day thresholds
      - At 7 days: URGENT daily email to all assigned timekeepers + super admin
    """
    today = date.today()
    alerts_sent = {"total": 0, "urgent": 0, "records_checked": 0}

    active_records = db.query_all(
        SOLRecord,
        filters={"status": "active"},
    )

    for record in active_records:
        alerts_sent["records_checked"] += 1
        days_remaining = (record.computed_expiration_date - today).days

        if days_remaining < 0:
            # Already expired — send critical alert every day
            record.status = "expired"
            await _send_sol_alert(
                tenant_id=tenant_id,
                record=record,
                days_remaining=days_remaining,
                severity="EXPIRED",
                db=db,
                email_service=email_service,
            )
            alerts_sent["urgent"] += 1
            alerts_sent["total"] += 1
            continue

        # Check which threshold we've crossed
        for threshold in ALERT_THRESHOLDS:
            if days_remaining <= threshold:
                # Only send if we haven't already sent at this level
                if record.last_alert_level is None or record.last_alert_level > threshold:
                    severity = "URGENT" if threshold <= URGENT_THRESHOLD else "WARNING"
                    await _send_sol_alert(
                        tenant_id=tenant_id,
                        record=record,
                        days_remaining=days_remaining,
                        severity=severity,
                        db=db,
                        email_service=email_service,
                    )
                    record.last_alert_level = threshold
                    record.last_alert_at = datetime.now(timezone.utc)
                    alerts_sent["total"] += 1
                    if severity == "URGENT":
                        alerts_sent["urgent"] += 1
                elif days_remaining <= URGENT_THRESHOLD:
                    # At 7 days or less, send EVERY DAY regardless
                    await _send_sol_alert(
                        tenant_id=tenant_id,
                        record=record,
                        days_remaining=days_remaining,
                        severity="URGENT",
                        db=db,
                        email_service=email_service,
                    )
                    record.last_alert_at = datetime.now(timezone.utc)
                    alerts_sent["urgent"] += 1
                    alerts_sent["total"] += 1
                break  # Only send the most urgent applicable threshold

    db.flush()
    logger.info(f"SOL alert check: {alerts_sent}")
    return alerts_sent


async def _send_sol_alert(
    tenant_id: str,
    record: SOLRecord,
    days_remaining: int,
    severity: str,
    db: TenantSession,
    email_service: EmailService,
) -> None:
    """Send an SOL alert email."""
    # Get recipients: all assigned timekeepers + super admins
    recipients = _get_sol_alert_recipients(tenant_id, record.matter_id, db)

    if days_remaining < 0:
        subject = f"EXPIRED SOL: {record.cause_of_action} — Matter {record.matter_id}"
        urgency_text = f"EXPIRED {abs(days_remaining)} days ago"
    else:
        subject = f"{severity}: SOL expires in {days_remaining} days — {record.cause_of_action}"
        urgency_text = f"{days_remaining} days remaining"

    body = (
        f"Statute of Limitations Alert\n"
        f"{'=' * 40}\n"
        f"Severity: {severity}\n"
        f"Status: {urgency_text}\n\n"
        f"Matter ID: {record.matter_id}\n"
        f"Claim: {record.claim_description}\n"
        f"Cause of Action: {record.cause_of_action}\n"
        f"Jurisdiction: {record.jurisdiction}\n"
        f"Accrual Date: {record.accrual_date.isoformat()}\n"
        f"Expiration Date: {record.computed_expiration_date.isoformat()}\n"
    )

    if record.is_tolled:
        body += f"\nTolling: Active — {record.toll_reason}\n"
        body += f"Tolled Days: {record.tolled_days}\n"

    await email_service.send(
        tenant_id=tenant_id,
        user_id=0,
        to=recipients,
        subject=subject,
        body=body,
    )

    write_audit(
        tenant_id=tenant_id,
        table_name="sol_records",
        record_id=record.id,
        action="alert_sent",
        details={
            "severity": severity,
            "days_remaining": days_remaining,
            "recipients_count": len(recipients),
        },
    )


def _get_sol_alert_recipients(tenant_id: str, matter_id: int, db: TenantSession) -> list[str]:
    """Get all timekeepers + super admins for SOL alerts."""
    recipients = set()

    # Get timekeepers assigned to the matter
    timekeepers = db.query_all(
        "matter_timekeepers",
        filters={"matter_id": matter_id},
    )
    for tk in timekeepers:
        user = db.query_first("users", filters={"id": tk.user_id})
        if user and hasattr(user, "email"):
            recipients.add(user.email)

    # Always include super admins
    super_admins = db.query_all("users", filters={"role": "super_admin"})
    for sa in super_admins:
        if hasattr(sa, "email"):
            recipients.add(sa.email)

    return list(recipients)
