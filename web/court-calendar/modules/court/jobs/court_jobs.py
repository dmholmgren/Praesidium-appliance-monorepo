"""
Court & Calendar RQ Jobs — All background work on PROC-01.

Never blocks HTTP handlers. Enqueued via RQ from FastAPI endpoints.

Schedule:
  - tyler_email_monitor:    every 5 minutes
  - pacer_docket_poll:      every 30 minutes
  - sol_alert_check:        daily 6:00 AM
  - calendar_cross_check:   daily 7:00 AM
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# COMP 1: Tyler/Odyssey email monitor job
# ──────────────────────────────────────────────────────────────

def tyler_email_monitor_job(tenant_id: str) -> dict:
    """
    RQ job: Poll for new Tyler/Odyssey emails and process them.

    Schedule: Every 5 minutes via RQ scheduler.
    VM: PROC-01 (10.10.60.12)
    """
    from core.db.tenant_session import get_tenant_session
    from core.services import get_email_service, get_storage_service
    from modules.court.services.tyler_monitor import process_tyler_email

    import asyncio

    async def _run():
        db = get_tenant_session(tenant_id)
        email_service = get_email_service(tenant_id)
        storage_service = get_storage_service(tenant_id)

        # Fetch unread emails from monitored folder
        messages = await email_service.fetch_unread(
            tenant_id=tenant_id,
            folder="Inbox",
            max_results=50,
        )

        processed = 0
        for msg in messages:
            entry = await process_tyler_email(
                tenant_id=tenant_id,
                email_message=msg,
                db=db,
                email_service=email_service,
                storage_service=storage_service,
            )
            if entry:
                processed += 1

                # If scheduling order detected, enqueue extraction job
                if entry.entry_type == "scheduling_order":
                    from rq import Queue
                    from redis import Redis
                    import os

                    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
                    q = Queue("proc", connection=Redis.from_url(redis_url))
                    q.enqueue(
                        scheduling_order_extraction_job,
                        tenant_id=tenant_id,
                        docket_entry_id=entry.id,
                        matter_id=entry.matter_id,
                        document_id=entry.document_id,
                    )

        db.commit()
        return {"processed": processed, "total_messages": len(messages)}

    return asyncio.run(_run())


# ──────────────────────────────────────────────────────────────
# COMP 2: PACER docket poll job
# ──────────────────────────────────────────────────────────────

def pacer_docket_poll_job(tenant_id: str) -> dict:
    """
    RQ job: Poll PACER for new docket entries on all active federal matters.

    Schedule: Every 30 minutes via RQ scheduler.
    VM: PROC-01 (10.10.60.12)
    """
    from core.db.tenant_session import get_tenant_session
    from core.services import get_storage_service
    from modules.court.services.pacer_service import (
        CourtListenerClient,
        PACERClient,
        poll_federal_docket,
    )

    import asyncio
    import os

    async def _run():
        db = get_tenant_session(tenant_id)
        storage_service = get_storage_service(tenant_id)

        # Get PACER credentials from tenant credential vault
        pacer_client = PACERClient(
            base_url=os.environ.get("PACER_API_URL", ""),
            username=os.environ.get("PACER_USERNAME", ""),
            password=os.environ.get("PACER_PASSWORD", ""),
        )
        courtlistener_client = CourtListenerClient(
            api_token=os.environ.get("COURTLISTENER_TOKEN", ""),
        )

        # Get all active federal matters with case numbers
        matters = db.query_all(
            "matters",
            filters={"status": "active", "court_type": "federal"},
        )

        total_new = 0
        for matter in matters:
            if not hasattr(matter, "case_number") or not matter.case_number:
                continue

            district = getattr(matter, "federal_district", None)
            if not district:
                continue

            last_check = getattr(matter, "last_pacer_check", None)
            if not last_check:
                last_check = date.today() - timedelta(days=7)

            new_entries = await poll_federal_docket(
                tenant_id=tenant_id,
                matter_id=matter.id,
                district=district,
                case_number=matter.case_number,
                last_check_date=last_check,
                db=db,
                pacer_client=pacer_client,
                courtlistener_client=courtlistener_client,
                storage_service=storage_service,
            )
            total_new += len(new_entries)

            # Check for scheduling orders in new entries
            for entry in new_entries:
                if entry.entry_type == "scheduling_order":
                    from rq import Queue
                    from redis import Redis

                    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
                    q = Queue("proc", connection=Redis.from_url(redis_url))
                    q.enqueue(
                        scheduling_order_extraction_job,
                        tenant_id=tenant_id,
                        docket_entry_id=entry.id,
                        matter_id=entry.matter_id,
                        document_id=entry.document_id,
                    )

        db.commit()
        return {"matters_checked": len(matters), "new_entries": total_new}

    return asyncio.run(_run())


# ──────────────────────────────────────────────────────────────
# COMP 5: Scheduling order extraction job
# ──────────────────────────────────────────────────────────────

def scheduling_order_extraction_job(
    tenant_id: str,
    docket_entry_id: int,
    matter_id: int,
    document_id: int,
) -> dict:
    """
    RQ job: Extract dates from a scheduling order using AIService.

    Triggered when Tyler monitor or PACER poll detects a scheduling order.
    VM: PROC-01 (10.10.60.12)
    """
    from core.db.tenant_session import get_tenant_session
    from core.services import get_ai_service, get_storage_service
    from modules.court.services.scheduling_order_processor import (
        extract_scheduling_order_dates,
    )

    import asyncio

    async def _run():
        db = get_tenant_session(tenant_id)
        ai_service = get_ai_service(tenant_id)
        storage_service = get_storage_service(tenant_id)

        # Retrieve document text
        doc_content = await storage_service.retrieve(
            tenant_id=tenant_id,
            document_id=document_id,
        )
        # Convert bytes to text (assumes PDF/text extraction already done)
        document_text = doc_content.decode("utf-8", errors="replace") if isinstance(doc_content, bytes) else str(doc_content)

        sched_order = await extract_scheduling_order_dates(
            tenant_id=tenant_id,
            matter_id=matter_id,
            document_id=document_id,
            document_text=document_text,
            docket_entry_id=docket_entry_id,
            db=db,
            ai_service=ai_service,
        )

        db.commit()
        return {
            "scheduling_order_id": sched_order.id,
            "dates_extracted": len(sched_order.extracted_dates_json.get("dates", [])),
        }

    return asyncio.run(_run())


# ──────────────────────────────────────────────────────────────
# COMP 4 + 6: Deadline calculation + calendar event creation job
# ──────────────────────────────────────────────────────────────

def deadline_calculation_job(
    tenant_id: str,
    scheduling_order_id: int,
) -> dict:
    """
    RQ job: Calculate deadline chains from confirmed scheduling order dates.

    Triggered after attorney confirmation. Creates deadlines and calendar events.
    VM: PROC-01 (10.10.60.12)
    """
    from core.db.tenant_session import get_tenant_session
    from core.services import get_calendar_service
    from modules.court.models import SchedulingOrder, SchedulingOrderDate
    from modules.court.services.calendar_creator import create_events_for_chain
    from modules.court.services.deadline_calculator import DeadlineCalculator

    import asyncio

    async def _run():
        db = get_tenant_session(tenant_id)
        calendar_service = get_calendar_service(tenant_id)

        sched_order = db.query_first(SchedulingOrder, filters={"id": scheduling_order_id})
        if not sched_order or sched_order.status != "attorney_confirmed":
            return {"error": "Scheduling order not confirmed"}

        # Get confirmed dates
        confirmed_dates = db.query_all(
            SchedulingOrderDate,
            filters={"scheduling_order_id": scheduling_order_id, "is_confirmed": True},
        )

        calculator = DeadlineCalculator(tenant_id=tenant_id, db=db)

        # Determine jurisdiction from matter
        matter = db.query_first("matters", filters={"id": sched_order.matter_id})
        jurisdiction = getattr(matter, "jurisdiction", "federal")

        total_deadlines = 0
        total_events = 0

        for sched_date in confirmed_dates:
            anchor = sched_date.confirmed_date
            event_type = sched_date.event_type or sched_date.date_label.lower().replace(" ", "_")

            deadlines = calculator.calculate_chain(
                matter_id=sched_order.matter_id,
                anchor_date=anchor,
                anchor_description=sched_date.date_label,
                triggering_event=event_type,
                jurisdiction=jurisdiction,
                scheduling_order_id=scheduling_order_id,
                scheduling_order_date_id=sched_date.id,
            )

            event_ids = await create_events_for_chain(
                tenant_id=tenant_id,
                deadlines=deadlines,
                db=db,
                calendar_service=calendar_service,
            )

            total_deadlines += len(deadlines)
            total_events += len(event_ids)

        sched_order.status = "deadlines_created"
        db.commit()

        return {
            "scheduling_order_id": scheduling_order_id,
            "deadlines_created": total_deadlines,
            "calendar_events_created": total_events,
        }

    return asyncio.run(_run())


# ──────────────────────────────────────────────────────────────
# COMP 7: Calendar cross-check daily job
# ──────────────────────────────────────────────────────────────

def calendar_cross_check_job(tenant_id: str) -> dict:
    """
    RQ job: Daily dual calendar cross-check.

    Schedule: Daily at 7:00 AM.
    VM: PROC-01 (10.10.60.12)
    """
    from core.db.tenant_session import get_tenant_session
    from core.services import get_calendar_service, get_email_service
    from modules.court.services.calendar_cross_check import run_cross_check

    import asyncio

    async def _run():
        db = get_tenant_session(tenant_id)
        calendar_service = get_calendar_service(tenant_id)
        email_service = get_email_service(tenant_id)

        log_entry = await run_cross_check(
            tenant_id=tenant_id,
            db=db,
            calendar_service=calendar_service,
            email_service=email_service,
        )

        db.commit()
        return {
            "discrepancies": log_entry.total_discrepancies,
            "critical": log_entry.critical_count,
        }

    return asyncio.run(_run())


# ──────────────────────────────────────────────────────────────
# COMP 10: SOL alert check daily job
# ──────────────────────────────────────────────────────────────

def sol_alert_check_job(tenant_id: str) -> dict:
    """
    RQ job: Daily SOL alert check.

    Schedule: Daily at 6:00 AM.
    VM: PROC-01 (10.10.60.12)
    """
    from core.db.tenant_session import get_tenant_session
    from core.services import get_email_service
    from modules.court.services.sol_tracker import run_sol_alert_check

    import asyncio

    async def _run():
        db = get_tenant_session(tenant_id)
        email_service = get_email_service(tenant_id)

        result = await run_sol_alert_check(
            tenant_id=tenant_id,
            db=db,
            email_service=email_service,
        )

        db.commit()
        return result

    return asyncio.run(_run())
