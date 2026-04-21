"""
COMP 8 — E-Filing Service.

E-filing via Tyler eFileTexas (Texas state) and CM/ECF PACER Next Gen (federal).
Includes pre-filing compliance gate that blocks filing without AI certification
(COMP 9 integration).

Flow:
  1. POST /api/v1/court/efile/prepare — validate, format, preview
  2. POST /api/v1/court/efile/submit — admin submits after review

All external calls via core/services/ interfaces.
All DB writes via write_audit().
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from core.audit import write_audit
from core.db.base import TenantSession
from core.services.email import EmailService
from core.services.storage import StorageService
from modules.court.models import (
    AICertificationForm,
    CourtAIRule,
    FilingLog,
)

logger = logging.getLogger(__name__)


class EFilingError(Exception):
    """Raised when e-filing preparation or submission fails."""
    pass


class ComplianceGateError(EFilingError):
    """Raised when AI certification compliance gate blocks filing."""
    pass


# ──────────────────────────────────────────────────────────────
# Pre-filing compliance gate (COMP 9 integration)
# ──────────────────────────────────────────────────────────────

async def check_compliance_gate(
    tenant_id: str,
    document_id: int,
    matter_id: int,
    court_name: str,
    jurisdiction: str,
    db: TenantSession,
) -> Optional[AICertificationForm]:
    """
    Check if AI certification is required and complete before filing.

    Returns the signed certification form if compliant, or raises
    ComplianceGateError if certification is required but missing.
    """
    # Check if this court requires AI certification
    ai_rules = db.query_all(
        CourtAIRule,
        filters={
            "court_name": court_name,
            "is_active": True,
            "certification_required": True,
        },
    )

    # Also check by jurisdiction pattern
    if not ai_rules:
        ai_rules = db.query_all(
            CourtAIRule,
            filters={
                "jurisdiction": jurisdiction,
                "is_active": True,
                "certification_required": True,
            },
        )

    if not ai_rules:
        return None  # No certification required for this court

    rule = ai_rules[0]

    # Check for existing signed certification for this document
    cert_form = db.query_first(
        AICertificationForm,
        filters={
            "document_id": document_id,
            "court_ai_rule_id": rule.id,
            "status": "signed",
        },
    )

    if not cert_form:
        raise ComplianceGateError(
            f"AI certification required by {rule.court_name} ({rule.order_title}) "
            f"but no signed certification exists for document {document_id}. "
            f"Generate and sign the certification before e-filing."
        )

    return cert_form


# ──────────────────────────────────────────────────────────────
# E-Filing preparation
# ──────────────────────────────────────────────────────────────

async def prepare_filing(
    tenant_id: str,
    document_id: int,
    matter_id: int,
    court_system: str,
    court_name: str,
    jurisdiction: str,
    filing_type: str,
    attorney_user_id: int,
    db: TenantSession,
    storage_service: StorageService,
) -> FilingLog:
    """
    Prepare a document for e-filing.

    Validates the document, checks compliance gate, formats the filing
    package per court requirements, and returns a preview record.
    """
    # Validate document status (must be approved for filing)
    document = db.query_first("documents", filters={"id": document_id})
    if not document:
        raise EFilingError(f"Document {document_id} not found")

    if getattr(document, "status", None) != "approved_for_filing":
        raise EFilingError(
            f"Document {document_id} has status '{getattr(document, 'status', 'unknown')}'. "
            f"Must be 'approved_for_filing' before e-filing."
        )

    # Check AI certification compliance gate
    cert_form = await check_compliance_gate(
        tenant_id=tenant_id,
        document_id=document_id,
        matter_id=matter_id,
        court_name=court_name,
        jurisdiction=jurisdiction,
        db=db,
    )

    # Retrieve document content from StorageService
    doc_content = await storage_service.retrieve(
        tenant_id=tenant_id,
        document_id=document_id,
    )

    # Format filing package based on court system
    submission_payload = _format_filing_package(
        court_system=court_system,
        document_content=doc_content,
        filing_type=filing_type,
        case_number=getattr(document, "case_number", None),
        cert_form=cert_form,
    )

    # Create filing log record
    filing = FilingLog(
        tenant_id=tenant_id,
        matter_id=matter_id,
        document_id=document_id,
        court_system=court_system,
        court_name=court_name,
        case_number=getattr(document, "case_number", None),
        filing_type=filing_type,
        status="prepared",
        attorney_user_id=attorney_user_id,
        certification_form_id=cert_form.id if cert_form else None,
        submission_payload=submission_payload,
    )
    db.add(filing)
    db.flush()

    write_audit(
        tenant_id=tenant_id,
        table_name="filing_log",
        record_id=filing.id,
        action="prepare",
        details={
            "court_system": court_system,
            "filing_type": filing_type,
            "certification_required": cert_form is not None,
        },
    )

    logger.info(f"Filing prepared: {filing.id} for document {document_id} → {court_system}")
    return filing


# ──────────────────────────────────────────────────────────────
# E-Filing submission
# ──────────────────────────────────────────────────────────────

async def submit_filing(
    tenant_id: str,
    filing_id: int,
    submitted_by_user_id: int,
    db: TenantSession,
    storage_service: StorageService,
    email_service: EmailService,
) -> FilingLog:
    """
    Submit a prepared filing to the appropriate e-filing system.

    Admin submits after reviewing the preview. On success, saves
    confirmation receipt to DMS and calculates response deadlines.
    """
    filing = db.query_first(FilingLog, filters={"id": filing_id})
    if not filing:
        raise EFilingError(f"Filing {filing_id} not found")
    if filing.status != "prepared":
        raise EFilingError(f"Filing {filing_id} has status '{filing.status}', expected 'prepared'")

    filing.submitted_by_user_id = submitted_by_user_id
    filing.submitted_at = datetime.now(timezone.utc)

    try:
        # Route to appropriate e-filing API
        if filing.court_system == "tyler":
            response = await _submit_tyler(filing, db)
        elif filing.court_system == "cmecf":
            response = await _submit_cmecf(filing, db)
        elif filing.court_system == "fileserve":
            response = await _submit_fileserve(filing, db)
        elif filing.court_system == "onelegal":
            response = await _submit_onelegal(filing, db)
        else:
            raise EFilingError(f"Unknown court system: {filing.court_system}")

        # Success
        filing.status = "accepted"
        filing.response_data = response
        filing.confirmation_number = response.get("confirmation_number")
        filing.response_at = datetime.now(timezone.utc)

        # Save filing receipt to DMS
        if response.get("receipt"):
            stored = await storage_service.store(
                tenant_id=tenant_id,
                path=f"court_filings/{filing.case_number}/receipt_{filing.confirmation_number}.pdf",
                content=response["receipt"],
                metadata={"source": "efiling_receipt", "filing_id": filing.id},
            )
            filing.receipt_document_id = stored.get("document_id")

        write_audit(
            tenant_id=tenant_id,
            table_name="filing_log",
            record_id=filing.id,
            action="submit_accepted",
            details={"confirmation_number": filing.confirmation_number},
        )

    except Exception as e:
        filing.status = "error"
        filing.rejection_reason = str(e)
        filing.response_at = datetime.now(timezone.utc)

        write_audit(
            tenant_id=tenant_id,
            table_name="filing_log",
            record_id=filing.id,
            action="submit_failed",
            details={"error": str(e)},
        )

        # Send immediate alert on failure
        await email_service.send(
            tenant_id=tenant_id,
            user_id=0,
            to=_get_filing_alert_recipients(tenant_id, db),
            subject=f"E-FILING FAILED: {filing.court_name} — {filing.filing_type}",
            body=f"E-filing submission failed.\n\nCourt: {filing.court_name}\nType: {filing.filing_type}\nError: {str(e)}",
        )

        logger.error(f"E-filing failed for {filing.id}: {e}")

    db.flush()
    return filing


# ──────────────────────────────────────────────────────────────
# Court-specific submission adapters
# ──────────────────────────────────────────────────────────────

async def _submit_tyler(filing: FilingLog, db: TenantSession) -> dict:
    """Submit via Tyler eFileTexas API."""
    # Credentials from tenant credential vault
    # POST to eFileTexas submission endpoint
    raise NotImplementedError("Wire to Tyler eFileTexas API")


async def _submit_cmecf(filing: FilingLog, db: TenantSession) -> dict:
    """Submit via CM/ECF PACER Next Gen API."""
    raise NotImplementedError("Wire to CM/ECF Next Gen filing API")


async def _submit_fileserve(filing: FilingLog, db: TenantSession) -> dict:
    """Submit via File & ServeXpress (California)."""
    raise NotImplementedError("Wire to File & ServeXpress API")


async def _submit_onelegal(filing: FilingLog, db: TenantSession) -> dict:
    """Submit via One Legal (California)."""
    raise NotImplementedError("Wire to One Legal API")


def _format_filing_package(
    court_system: str,
    document_content: bytes,
    filing_type: str,
    case_number: Optional[str],
    cert_form: Optional[AICertificationForm],
) -> dict:
    """Format filing package per court requirements."""
    package = {
        "court_system": court_system,
        "filing_type": filing_type,
        "case_number": case_number,
        "documents": [{"type": "main", "content_size": len(document_content) if document_content else 0}],
    }
    if cert_form:
        package["documents"].append({
            "type": "ai_certification",
            "certification_form_id": cert_form.id,
        })
    return package


def _get_filing_alert_recipients(tenant_id: str, db: TenantSession) -> list[str]:
    """Get email addresses for filing failure alerts."""
    config = db.query_first("tenant_config", filters={"config_key": "filing_alert_recipients"})
    if config and config.config_value:
        return [e.strip() for e in config.config_value.split(",") if e.strip()]
    return []
