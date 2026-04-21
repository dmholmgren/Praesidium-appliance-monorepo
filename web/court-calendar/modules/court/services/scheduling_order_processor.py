"""
COMP 5 — Scheduling Order Processor.

AI extraction of dates from scheduling orders via AIService.
Runs as RQ job on PROC-01. Never blocks HTTP handlers.

Flow:
  1. AI classifier detects scheduling order in DMS
  2. AI extracts all explicit dates
  3. Each date presented to attorney with edit + confirm checkbox
  4. On confirmation: full derivative deadline chain calculated
  5. No deadline enters calendar without attorney confirmation

All AI calls via AIService only — never call SDK directly.
All DB writes via write_audit().
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from core.audit import write_audit
from core.db.tenant_session import TenantSession
from core.services.ai import AIService
from core.services.storage import StorageService
from modules.court.models import SchedulingOrder, SchedulingOrderDate

logger = logging.getLogger(__name__)

SCHEDULING_ORDER_EXTRACTION_PROMPT = """You are a legal document analyzer. Extract ALL dates and deadlines from this scheduling order.

For each date found, return:
- date_label: A clear description of what the date is for (e.g., "Discovery Cutoff", "Expert Report Deadline", "Dispositive Motion Deadline", "Pretrial Conference", "Trial Date")
- date_value: The date in YYYY-MM-DD format
- event_type: The closest matching event type from this list: discovery_cutoff, expert_report_deadline, expert_rebuttal_deadline, dispositive_motion_deadline, pretrial_conference, trial_date, mediation_deadline, jury_instructions_deadline, witness_list_deadline, exhibit_list_deadline, scheduling_conference, other

Return ONLY valid JSON in this exact format, no other text:
{
  "dates": [
    {"date_label": "...", "date_value": "YYYY-MM-DD", "event_type": "..."},
    ...
  ],
  "court_name": "...",
  "case_number": "...",
  "judge_name": "..."
}

DOCUMENT TEXT:
"""

SCHEDULING_ORDER_CLASSIFIER_PROMPT = """Analyze this document and determine if it is a scheduling order, docket control order, or case management order issued by a court.

Return ONLY valid JSON:
{"is_scheduling_order": true/false, "confidence": 0.0-1.0, "reason": "..."}

DOCUMENT TEXT (first 2000 characters):
"""


async def classify_document_as_scheduling_order(
    tenant_id: str,
    document_text: str,
    ai_service: AIService,
) -> tuple[bool, float]:
    """
    Use AIService to classify whether a document is a scheduling order.

    Returns (is_scheduling_order, confidence).
    """
    prompt = SCHEDULING_ORDER_CLASSIFIER_PROMPT + document_text[:2000]

    try:
        response = await ai_service.complete(
            tenant_id=tenant_id,
            prompt=prompt,
            system="You are a legal document classifier. Respond only with JSON.",
            max_tokens=200,
        )
        result = json.loads(response)
        return result.get("is_scheduling_order", False), result.get("confidence", 0.0)
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Scheduling order classification failed: {e}")
        return False, 0.0


async def extract_scheduling_order_dates(
    tenant_id: str,
    matter_id: int,
    document_id: int,
    document_text: str,
    docket_entry_id: Optional[int],
    db: TenantSession,
    ai_service: AIService,
) -> SchedulingOrder:
    """
    Extract dates from a scheduling order using AIService.

    Creates a SchedulingOrder record and associated SchedulingOrderDate
    records for attorney review. Does NOT create deadlines — that only
    happens after attorney confirmation (COMP 4 + COMP 6).

    Returns the created SchedulingOrder.
    """
    # AI extraction
    prompt = SCHEDULING_ORDER_EXTRACTION_PROMPT + document_text

    try:
        response = await ai_service.complete(
            tenant_id=tenant_id,
            prompt=prompt,
            system="You are a legal document date extractor. Return only valid JSON.",
            max_tokens=2000,
        )
        extracted = json.loads(response)
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Date extraction failed for document {document_id}: {e}")
        extracted = {"dates": [], "court_name": "", "case_number": "", "judge_name": ""}

    # Create scheduling order record
    sched_order = SchedulingOrder(
        tenant_id=tenant_id,
        matter_id=matter_id,
        docket_entry_id=docket_entry_id,
        document_id=document_id,
        status="dates_extracted",
        extracted_dates_json=extracted,
    )
    db.add(sched_order)
    db.flush()

    write_audit(
        tenant_id=tenant_id,
        table_name="scheduling_orders",
        record_id=sched_order.id,
        action="create",
        details={"document_id": document_id, "dates_found": len(extracted.get("dates", []))},
    )

    # Create individual date records for attorney confirmation
    for date_info in extracted.get("dates", []):
        from datetime import date as date_type

        try:
            extracted_date = date_type.fromisoformat(date_info["date_value"])
        except (ValueError, KeyError):
            logger.warning(f"Invalid date in extraction: {date_info}")
            continue

        sched_date = SchedulingOrderDate(
            tenant_id=tenant_id,
            scheduling_order_id=sched_order.id,
            date_label=date_info.get("date_label", "Unknown"),
            extracted_date=extracted_date,
            is_confirmed=False,
            event_type=date_info.get("event_type", "other"),
        )
        db.add(sched_date)

    db.flush()

    logger.info(
        f"Extracted {len(extracted.get('dates', []))} dates from scheduling order "
        f"for matter {matter_id}, document {document_id}"
    )
    return sched_order


async def confirm_scheduling_order_dates(
    tenant_id: str,
    scheduling_order_id: int,
    confirmed_dates: list[dict],
    user_id: int,
    db: TenantSession,
) -> SchedulingOrder:
    """
    Record attorney confirmation of scheduling order dates.

    confirmed_dates format: [{"id": date_record_id, "confirmed_date": "YYYY-MM-DD"}, ...]

    After confirmation, the scheduling order status is updated and the
    deadline calculation job is enqueued (COMP 4).
    """
    sched_order = db.query_first(
        SchedulingOrder,
        filters={"id": scheduling_order_id},
    )
    if not sched_order:
        raise ValueError(f"Scheduling order {scheduling_order_id} not found")

    now = datetime.now(timezone.utc)

    for confirmed in confirmed_dates:
        from datetime import date as date_type

        sched_date = db.query_first(
            SchedulingOrderDate,
            filters={"id": confirmed["id"], "scheduling_order_id": scheduling_order_id},
        )
        if not sched_date:
            continue

        sched_date.confirmed_date = date_type.fromisoformat(confirmed["confirmed_date"])
        sched_date.is_confirmed = True
        sched_date.confirmed_by_user_id = user_id
        sched_date.confirmed_at = now

        write_audit(
            tenant_id=tenant_id,
            table_name="scheduling_order_dates",
            record_id=sched_date.id,
            action="confirm",
            details={
                "confirmed_by": user_id,
                "extracted_date": sched_date.extracted_date.isoformat(),
                "confirmed_date": confirmed["confirmed_date"],
            },
        )

    sched_order.status = "attorney_confirmed"
    sched_order.confirmed_by_user_id = user_id
    sched_order.confirmed_at = now

    write_audit(
        tenant_id=tenant_id,
        table_name="scheduling_orders",
        record_id=sched_order.id,
        action="confirm",
        details={"confirmed_by": user_id, "dates_confirmed": len(confirmed_dates)},
    )

    db.flush()
    return sched_order
