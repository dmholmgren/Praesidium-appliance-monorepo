"""
Court & Calendar module — FastAPI routes.

Backend: Python/FastAPI. Frontend: HTMX + Tailwind. No React.
All background work via RQ jobs — never blocks HTTP handlers.
All DB access via TenantSession.
All branding via BrandingService.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/court", tags=["court"])
ui_router = APIRouter(prefix="/court", tags=["court-ui"])


# ──────────────────────────────────────────────────────────────
# Pydantic request/response models
# ──────────────────────────────────────────────────────────────

class DeadlineCalculateRequest(BaseModel):
    matter_id: int
    anchor_date: str  # YYYY-MM-DD
    anchor_description: str
    triggering_event: str
    jurisdiction: str
    service_method: str | None = None

class SchedulingOrderConfirmRequest(BaseModel):
    scheduling_order_id: int
    confirmed_dates: list[dict]  # [{"id": int, "confirmed_date": "YYYY-MM-DD"}]

class EFilePrepareRequest(BaseModel):
    document_id: int
    matter_id: int
    court_system: str
    court_name: str
    jurisdiction: str
    filing_type: str
    attorney_user_id: int

class EFileSubmitRequest(BaseModel):
    filing_id: int

class SOLCreateRequest(BaseModel):
    matter_id: int
    claim_description: str
    cause_of_action: str
    jurisdiction: str
    limitation_period_days: int
    accrual_date: str  # YYYY-MM-DD

class SOLTollingRequest(BaseModel):
    sol_record_id: int
    is_tolled: bool
    toll_reason: str | None = None
    toll_start: str | None = None
    toll_end: str | None = None

class SOLDeactivateRequest(BaseModel):
    sol_record_id: int
    deactivation_reason: str

class CertFormGenerateRequest(BaseModel):
    document_id: int
    matter_id: int
    court_ai_rule_id: int
    attorney_user_id: int

class CertFormSignRequest(BaseModel):
    cert_form_id: int
    signed_document_id: int
    attorney_user_id: int

class CourtAIRuleCreateRequest(BaseModel):
    jurisdiction: str
    court_name: str
    order_title: str
    order_date: str
    effective_date: str
    certification_required: bool = True
    certification_template_key: str
    specific_requirements: dict | None = None
    form_fields: dict | None = None


# ──────────────────────────────────────────────────────────────
# COMP 4: Deadline calculation
# ──────────────────────────────────────────────────────────────

@router.post("/deadlines/calculate")
async def calculate_deadlines(req: DeadlineCalculateRequest, request: Request):
    """Enqueue deadline calculation as RQ job on PROC-01."""
    tenant_id = request.state.tenant_id
    from redis import Redis
    from rq import Queue

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    q = Queue("proc", connection=Redis.from_url(redis_url))

    from modules.court.jobs.court_jobs import deadline_calculation_job
    job = q.enqueue(
        deadline_calculation_job,
        tenant_id=tenant_id,
        scheduling_order_id=req.matter_id,  # Will be mapped properly
    )
    return {"job_id": job.id, "status": "enqueued"}


# ──────────────────────────────────────────────────────────────
# COMP 5: Scheduling order confirmation
# ──────────────────────────────────────────────────────────────

@router.post("/scheduling-orders/confirm")
async def confirm_scheduling_order(req: SchedulingOrderConfirmRequest, request: Request):
    """Confirm extracted dates and trigger deadline calculation."""
    tenant_id = request.state.tenant_id
    user_id = request.state.user_id

    from core.db.tenant_session import get_tenant_session
    from modules.court.services.scheduling_order_processor import confirm_scheduling_order_dates

    db = get_tenant_session(tenant_id)

    sched_order = await confirm_scheduling_order_dates(
        tenant_id=tenant_id,
        scheduling_order_id=req.scheduling_order_id,
        confirmed_dates=req.confirmed_dates,
        user_id=user_id,
        db=db,
    )

    # Enqueue deadline calculation job on PROC-01
    from redis import Redis
    from rq import Queue

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    q = Queue("proc", connection=Redis.from_url(redis_url))

    from modules.court.jobs.court_jobs import deadline_calculation_job
    job = q.enqueue(
        deadline_calculation_job,
        tenant_id=tenant_id,
        scheduling_order_id=sched_order.id,
    )

    db.commit()
    return {
        "scheduling_order_id": sched_order.id,
        "status": "confirmed",
        "deadline_job_id": job.id,
    }


@router.get("/scheduling-orders/{order_id}")
async def get_scheduling_order(order_id: int, request: Request):
    """Get a scheduling order with extracted/confirmed dates."""
    tenant_id = request.state.tenant_id
    from core.db.tenant_session import get_tenant_session
    from modules.court.models import SchedulingOrder, SchedulingOrderDate

    db = get_tenant_session(tenant_id)
    sched_order = db.query_first(SchedulingOrder, filters={"id": order_id})
    if not sched_order:
        raise HTTPException(status_code=404, detail="Scheduling order not found")

    dates = db.query_all(SchedulingOrderDate, filters={"scheduling_order_id": order_id})

    return {
        "id": sched_order.id,
        "matter_id": sched_order.matter_id,
        "status": sched_order.status,
        "dates": [
            {
                "id": d.id,
                "date_label": d.date_label,
                "extracted_date": d.extracted_date.isoformat(),
                "confirmed_date": d.confirmed_date.isoformat() if d.confirmed_date else None,
                "is_confirmed": d.is_confirmed,
                "event_type": d.event_type,
            }
            for d in dates
        ],
    }


# ──────────────────────────────────────────────────────────────
# COMP 8: E-Filing
# ──────────────────────────────────────────────────────────────

@router.post("/efile/prepare")
async def prepare_efile(req: EFilePrepareRequest, request: Request):
    """Prepare a document for e-filing. Returns preview for admin review."""
    tenant_id = request.state.tenant_id

    from core.db.tenant_session import get_tenant_session
    from core.services import get_storage_service
    from modules.court.services.efiling_service import prepare_filing

    db = get_tenant_session(tenant_id)
    storage_service = get_storage_service(tenant_id)

    try:
        filing = await prepare_filing(
            tenant_id=tenant_id,
            document_id=req.document_id,
            matter_id=req.matter_id,
            court_system=req.court_system,
            court_name=req.court_name,
            jurisdiction=req.jurisdiction,
            filing_type=req.filing_type,
            attorney_user_id=req.attorney_user_id,
            db=db,
            storage_service=storage_service,
        )
        db.commit()
        return {"filing_id": filing.id, "status": filing.status}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/efile/submit")
async def submit_efile(req: EFileSubmitRequest, request: Request):
    """Admin submits after reviewing preview."""
    tenant_id = request.state.tenant_id
    user_id = request.state.user_id

    from core.db.tenant_session import get_tenant_session
    from core.services import get_email_service, get_storage_service
    from modules.court.services.efiling_service import submit_filing

    db = get_tenant_session(tenant_id)
    storage_service = get_storage_service(tenant_id)
    email_service = get_email_service(tenant_id)

    filing = await submit_filing(
        tenant_id=tenant_id,
        filing_id=req.filing_id,
        submitted_by_user_id=user_id,
        db=db,
        storage_service=storage_service,
        email_service=email_service,
    )
    db.commit()
    return {
        "filing_id": filing.id,
        "status": filing.status,
        "confirmation_number": filing.confirmation_number,
    }


# ──────────────────────────────────────────────────────────────
# COMP 9: AI Certification Compliance
# ──────────────────────────────────────────────────────────────

@router.post("/ai-certification/generate")
async def generate_cert_form(req: CertFormGenerateRequest, request: Request):
    """Generate an AI certification form auto-populated from logs."""
    tenant_id = request.state.tenant_id

    from core.db.tenant_session import get_tenant_session
    from modules.court.services.ai_certification import generate_certification_form

    db = get_tenant_session(tenant_id)

    cert_form = await generate_certification_form(
        tenant_id=tenant_id,
        document_id=req.document_id,
        matter_id=req.matter_id,
        court_ai_rule_id=req.court_ai_rule_id,
        attorney_user_id=req.attorney_user_id,
        db=db,
    )
    db.commit()
    return {"cert_form_id": cert_form.id, "status": cert_form.status}


@router.post("/ai-certification/sign")
async def sign_cert_form(req: CertFormSignRequest, request: Request):
    """Record attorney signature on certification form."""
    tenant_id = request.state.tenant_id

    from core.db.tenant_session import get_tenant_session
    from modules.court.services.ai_certification import sign_certification_form

    db = get_tenant_session(tenant_id)

    cert_form = await sign_certification_form(
        tenant_id=tenant_id,
        cert_form_id=req.cert_form_id,
        signed_document_id=req.signed_document_id,
        attorney_user_id=req.attorney_user_id,
        db=db,
    )
    db.commit()
    return {"cert_form_id": cert_form.id, "status": cert_form.status}


@router.get("/ai-rules")
async def list_ai_rules(request: Request):
    """List all court AI rules."""
    tenant_id = request.state.tenant_id
    from core.db.tenant_session import get_tenant_session
    from modules.court.models import CourtAIRule

    db = get_tenant_session(tenant_id)
    rules = db.query_all(CourtAIRule, filters={"is_active": True})
    return [
        {
            "id": r.id,
            "jurisdiction": r.jurisdiction,
            "court_name": r.court_name,
            "order_title": r.order_title,
            "effective_date": r.effective_date.isoformat(),
            "certification_required": r.certification_required,
            "template_key": r.certification_template_key,
        }
        for r in rules
    ]


@router.post("/ai-rules")
async def create_ai_rule(req: CourtAIRuleCreateRequest, request: Request):
    """Admin endpoint to add new court AI rules."""
    tenant_id = request.state.tenant_id
    from core.audit import write_audit
    from core.db.tenant_session import get_tenant_session
    from modules.court.models import CourtAIRule

    db = get_tenant_session(tenant_id)
    rule = CourtAIRule(
        tenant_id=tenant_id,
        jurisdiction=req.jurisdiction,
        court_name=req.court_name,
        order_title=req.order_title,
        order_date=date.fromisoformat(req.order_date),
        effective_date=date.fromisoformat(req.effective_date),
        certification_required=req.certification_required,
        certification_template_key=req.certification_template_key,
        specific_requirements=req.specific_requirements,
        form_fields=req.form_fields,
    )
    db.add(rule)
    db.flush()
    write_audit(tenant_id=tenant_id, table_name="court_ai_rules", record_id=rule.id, action="create", details={"court": req.court_name})
    db.commit()
    return {"rule_id": rule.id}


# ──────────────────────────────────────────────────────────────
# COMP 10: SOL Tracker
# ──────────────────────────────────────────────────────────────

@router.post("/sol")
async def create_sol(req: SOLCreateRequest, request: Request):
    """Create a new SOL record. Required — cannot be skipped."""
    tenant_id = request.state.tenant_id
    user_id = request.state.user_id

    from core.db.tenant_session import get_tenant_session
    from core.services import get_calendar_service
    from modules.court.services.sol_tracker import create_sol_record

    db = get_tenant_session(tenant_id)
    calendar_service = get_calendar_service(tenant_id)

    record = await create_sol_record(
        tenant_id=tenant_id,
        matter_id=req.matter_id,
        claim_description=req.claim_description,
        cause_of_action=req.cause_of_action,
        jurisdiction=req.jurisdiction,
        limitation_period_days=req.limitation_period_days,
        accrual_date=date.fromisoformat(req.accrual_date),
        created_by_user_id=user_id,
        db=db,
        calendar_service=calendar_service,
    )
    db.commit()
    return {
        "sol_id": record.id,
        "expiration_date": record.computed_expiration_date.isoformat(),
    }


@router.put("/sol/tolling")
async def update_sol_tolling(req: SOLTollingRequest, request: Request):
    """Update tolling on an SOL record."""
    tenant_id = request.state.tenant_id
    user_id = request.state.user_id

    from core.db.tenant_session import get_tenant_session
    from modules.court.services.sol_tracker import update_tolling

    db = get_tenant_session(tenant_id)

    record = await update_tolling(
        tenant_id=tenant_id,
        sol_record_id=req.sol_record_id,
        is_tolled=req.is_tolled,
        toll_reason=req.toll_reason,
        toll_start=date.fromisoformat(req.toll_start) if req.toll_start else None,
        toll_end=date.fromisoformat(req.toll_end) if req.toll_end else None,
        user_id=user_id,
        db=db,
    )
    db.commit()
    return {"sol_id": record.id, "new_expiration": record.computed_expiration_date.isoformat()}


@router.delete("/sol/{sol_id}")
async def deactivate_sol(sol_id: int, request: Request):
    """Deactivate an SOL record — super admin only."""
    tenant_id = request.state.tenant_id
    user_id = request.state.user_id
    user_role = request.state.user_role

    from core.db.tenant_session import get_tenant_session
    from modules.court.services.sol_tracker import deactivate_sol_record

    db = get_tenant_session(tenant_id)

    # Deactivation reason from query param
    reason = request.query_params.get("reason", "")

    try:
        record = await deactivate_sol_record(
            tenant_id=tenant_id,
            sol_record_id=sol_id,
            deactivation_reason=reason,
            user_id=user_id,
            user_role=user_role,
            db=db,
        )
        db.commit()
        return {"sol_id": record.id, "status": "deactivated"}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
