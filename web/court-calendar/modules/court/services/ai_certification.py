"""
COMP 9 — AI Court Certification Compliance Module.

court_ai_rules table — pre-seed E.D. Tex. Apr 2025 + Denton County Oct 2025.
ai_contribution_log per document.
citation_verification_log per document.
AI certification form generator — auto-populate from logs.
Pre-filing compliance gate — blocks e-filing without cert (integrated in COMP 8).
Admin UI to add new court AI rules.

All AI calls via AIService only.
All DB writes via write_audit().
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from core.audit import write_audit
from core.db.tenant_session import TenantSession
from modules.court.models import (
    AICertificationForm,
    AIContributionLog,
    CitationVerificationLog,
    CourtAIRule,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Pre-seed data for court AI rules
# ──────────────────────────────────────────────────────────────

ED_TEX_RULE = {
    "jurisdiction": "federal_txed",
    "court_name": "United States District Court, Eastern District of Texas",
    "order_title": "Standing Order on Disclosure and Certification Requirements Regarding Use of Generative AI",
    "order_date": date(2025, 4, 9),
    "effective_date": date(2025, 4, 9),
    "certification_required": True,
    "certification_template_key": "ed_tex_2025",
    "specific_requirements": {
        "disclosure_required": True,
        "must_disclose_ai_used": True,
        "must_disclose_tools": True,
        "must_disclose_how_used": True,
        "must_certify_accuracy_verified": True,
        "filed_with_main_document": True,
        "applies_to": "all filings in Eastern District of Texas",
        "signing_judge": "John D. Love, United States Magistrate Judge",
    },
    "form_fields": {
        "ai_used_yes_no": "from ai_contribution_log",
        "tools_used": "from ai_api_calls.provider + model",
        "how_used": "from ai_contribution_log.module + prompt_category",
        "accuracy_verified": "from sanity_check_record + attorney_approval_timestamp",
        "format": "docx_federal_court",
    },
}

DENTON_COUNTY_RULE = {
    "jurisdiction": "TX_denton_county",
    "court_name": "District Courts of Denton County, Texas",
    "order_title": "Standing Order Regarding Use of Artificial Intelligence",
    "order_date": date(2025, 10, 20),
    "effective_date": date(2025, 10, 20),
    "certification_required": True,
    "certification_template_key": "denton_county_2025",
    "specific_requirements": {
        "exhibit_a_required": True,
        "applies_to": "every pending or hereafter filed case in Denton County District Courts",
        "certification_points": [
            "Reviewed and understand Standing Order; will comply throughout case",
            "AI-generated content verified as accurate through traditional non-AI legal sources",
            "Attorney personally responsible for all filings",
        ],
        "signing_judges": [
            "Judge Sherry Shipman, 16th",
            "Judge Jim Johnson, 431st",
            "Judge Steve Burgess, 158th",
            "Judge Tiffany Haerting, 442nd",
            "Judge Brody Shanklin, 211th",
            "Judge Lee Ann Breading, 462nd",
            "Judge Bruce McFarling, 362nd",
            "Judge Derbha Jones, 467th",
            "Judge Brent Hill, 367th",
            "Judge Michael Dickens, 477th",
            "Judge Karen Alexander, 393rd",
            "Judge Crystal Levonius, 481st",
        ],
    },
    "form_fields": {
        "cert_1_compliance": "from citation_verification_log + sanity_check",
        "cert_2_responsibility": "from attorney_approval_step",
        "format": "docx_texas_state_court",
        "exhibit_a_template": True,
    },
}

SEED_RULES = [ED_TEX_RULE, DENTON_COUNTY_RULE]


def get_seed_ai_rules() -> list[dict]:
    """Return pre-seed court AI rules for initial database population."""
    return SEED_RULES


# ──────────────────────────────────────────────────────────────
# AI Contribution tracking
# ──────────────────────────────────────────────────────────────

def log_ai_contribution(
    tenant_id: str,
    document_id: int,
    matter_id: int,
    user_id: int,
    ai_tool_name: str,
    ai_model: str,
    module: str,
    prompt_category: str,
    usage_description: str,
    ai_api_call_id: Optional[int],
    db: TenantSession,
) -> AIContributionLog:
    """Log an AI contribution to a document for certification tracking."""
    entry = AIContributionLog(
        tenant_id=tenant_id,
        document_id=document_id,
        matter_id=matter_id,
        user_id=user_id,
        ai_tool_name=ai_tool_name,
        ai_model=ai_model,
        module=module,
        prompt_category=prompt_category,
        usage_description=usage_description,
        ai_api_call_id=ai_api_call_id,
    )
    db.add(entry)
    db.flush()

    write_audit(
        tenant_id=tenant_id,
        table_name="ai_contribution_log",
        record_id=entry.id,
        action="create",
        details={"document_id": document_id, "module": module, "category": prompt_category},
    )
    return entry


def log_citation_verification(
    tenant_id: str,
    document_id: int,
    matter_id: int,
    citation_text: str,
    verification_source: str,
    verification_status: str,
    verification_details: Optional[dict],
    verified_by_user_id: Optional[int],
    db: TenantSession,
) -> CitationVerificationLog:
    """Log a citation verification result."""
    entry = CitationVerificationLog(
        tenant_id=tenant_id,
        document_id=document_id,
        matter_id=matter_id,
        citation_text=citation_text,
        verification_source=verification_source,
        verification_status=verification_status,
        verification_details=verification_details,
        verified_by_user_id=verified_by_user_id,
        verified_at=datetime.now(timezone.utc) if verified_by_user_id else None,
    )
    db.add(entry)
    db.flush()

    write_audit(
        tenant_id=tenant_id,
        table_name="citation_verification_log",
        record_id=entry.id,
        action="create",
        details={"document_id": document_id, "status": verification_status},
    )
    return entry


# ──────────────────────────────────────────────────────────────
# Certification form generation
# ──────────────────────────────────────────────────────────────

async def generate_certification_form(
    tenant_id: str,
    document_id: int,
    matter_id: int,
    court_ai_rule_id: int,
    attorney_user_id: int,
    db: TenantSession,
) -> AICertificationForm:
    """
    Generate an AI certification form auto-populated from logs.

    Reads ai_contribution_log, citation_verification_log, and ai_api_calls
    to populate the certification fields per the court's requirements.
    """
    rule = db.query_first(CourtAIRule, filters={"id": court_ai_rule_id})
    if not rule:
        raise ValueError(f"Court AI rule {court_ai_rule_id} not found")

    # Gather AI contributions for this document
    contributions = db.query_all(
        AIContributionLog,
        filters={"document_id": document_id},
    )

    # Gather citation verifications
    verifications = db.query_all(
        CitationVerificationLog,
        filters={"document_id": document_id},
    )

    # Build auto-populated data based on template key
    if rule.certification_template_key == "ed_tex_2025":
        generated_data = _build_ed_tex_form_data(contributions, verifications, attorney_user_id, db)
    elif rule.certification_template_key == "denton_county_2025":
        generated_data = _build_denton_county_form_data(contributions, verifications, attorney_user_id, db)
    else:
        generated_data = _build_generic_form_data(contributions, verifications)

    # Get matter and attorney details for case caption
    matter = db.query_first("matters", filters={"id": matter_id})
    attorney = db.query_first("users", filters={"id": attorney_user_id})

    generated_data["case_caption"] = {
        "case_number": getattr(matter, "case_number", ""),
        "case_title": getattr(matter, "title", ""),
        "court_name": rule.court_name,
    }
    generated_data["attorney_info"] = {
        "name": getattr(attorney, "full_name", ""),
        "bar_number": getattr(attorney, "bar_number", ""),
        "email": getattr(attorney, "email", ""),
        "firm_name": "",  # Loaded via BrandingService at render time
    }

    cert_form = AICertificationForm(
        tenant_id=tenant_id,
        matter_id=matter_id,
        document_id=document_id,
        court_ai_rule_id=court_ai_rule_id,
        form_template_key=rule.certification_template_key,
        generated_data=generated_data,
        status="generated",
        attorney_user_id=attorney_user_id,
    )
    db.add(cert_form)
    db.flush()

    write_audit(
        tenant_id=tenant_id,
        table_name="ai_certification_forms",
        record_id=cert_form.id,
        action="generate",
        details={
            "template": rule.certification_template_key,
            "contributions_count": len(contributions),
            "verifications_count": len(verifications),
        },
    )

    logger.info(f"Generated {rule.certification_template_key} certification for document {document_id}")
    return cert_form


def _build_ed_tex_form_data(
    contributions: list[AIContributionLog],
    verifications: list[CitationVerificationLog],
    attorney_user_id: int,
    db: TenantSession,
) -> dict:
    """Build E.D. Tex. certification form data."""
    ai_used = len(contributions) > 0

    # Summarize tools used
    tools = set()
    for c in contributions:
        tools.add(f"{c.ai_tool_name} {c.ai_model}")

    # Summarize how AI was used
    usage_summary = []
    for c in contributions:
        usage_summary.append(f"Used to {c.usage_description}")

    return {
        "ai_used": ai_used,
        "tools_used": list(tools) if ai_used else [],
        "usage_summary": usage_summary if ai_used else [],
        "accuracy_verified": all(
            v.verification_status == "verified" for v in verifications
        ) if verifications else True,
        "verification_count": len(verifications),
    }


def _build_denton_county_form_data(
    contributions: list[AIContributionLog],
    verifications: list[CitationVerificationLog],
    attorney_user_id: int,
    db: TenantSession,
) -> dict:
    """Build Denton County Exhibit A certification form data."""
    all_verified = all(
        v.verification_status == "verified" for v in verifications
    ) if verifications else True

    return {
        "cert_1_ai_content_verified": all_verified,
        "cert_1_verification_sources": list(set(v.verification_source for v in verifications)),
        "cert_2_attorney_acknowledges_responsibility": True,
        "contributions_count": len(contributions),
        "verifications_count": len(verifications),
    }


def _build_generic_form_data(
    contributions: list[AIContributionLog],
    verifications: list[CitationVerificationLog],
) -> dict:
    """Build generic certification form data for other courts."""
    return {
        "ai_used": len(contributions) > 0,
        "contributions": [
            {"module": c.module, "category": c.prompt_category, "description": c.usage_description}
            for c in contributions
        ],
        "verifications": [
            {"citation": v.citation_text, "status": v.verification_status, "source": v.verification_source}
            for v in verifications
        ],
    }


async def sign_certification_form(
    tenant_id: str,
    cert_form_id: int,
    signed_document_id: int,
    attorney_user_id: int,
    db: TenantSession,
) -> AICertificationForm:
    """Record attorney signature on a certification form."""
    cert_form = db.query_first(AICertificationForm, filters={"id": cert_form_id})
    if not cert_form:
        raise ValueError(f"Certification form {cert_form_id} not found")

    cert_form.signed_document_id = signed_document_id
    cert_form.status = "signed"
    cert_form.signed_at = datetime.now(timezone.utc)

    write_audit(
        tenant_id=tenant_id,
        table_name="ai_certification_forms",
        record_id=cert_form.id,
        action="sign",
        details={"signed_by": attorney_user_id, "signed_document_id": signed_document_id},
    )

    db.flush()
    return cert_form
