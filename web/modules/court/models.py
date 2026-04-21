"""
Court & Calendar module — SQLAlchemy ORM models.

Every table has tenant_id CHAR(36) NOT NULL as the first non-PK column.
All access through TenantSession only.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CHAR, Column, Date, DateTime, Index, Integer,
    JSON, String, Text, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared base — in production this is imported from core.models.base."""
    pass


# ──────────────────────────────────────────────────────────────
# COMP 3 — Rules database
# ──────────────────────────────────────────────────────────────

class CourtRule(Base):
    __tablename__ = "court_rules"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(100), nullable=False)
    rule_set: Mapped[str] = mapped_column(String(50), nullable=False)
    rule_number: Mapped[str] = mapped_column(String(50), nullable=False)
    rule_title: Mapped[str] = mapped_column(String(500), nullable=False)
    triggering_event: Mapped[str] = mapped_column(String(200), nullable=False)
    deadline_description: Mapped[str] = mapped_column(String(500), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_type: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    service_method_adjustments: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    triggers_rules: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    is_delta: Mapped[bool] = mapped_column(Boolean, default=False)
    overrides_rule_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    court_district: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    effective_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    superseded_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_court_rules_tenant_jurisdiction", "tenant_id", "jurisdiction"),
        Index("idx_court_rules_tenant_ruleset", "tenant_id", "rule_set"),
        Index("idx_court_rules_trigger", "tenant_id", "triggering_event"),
    )


class CourtHoliday(Base):
    __tablename__ = "court_holidays"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(100), nullable=False)
    court_district: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    holiday_name: Mapped[str] = mapped_column(String(200), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_holidays_tenant_jurisdiction_date", "tenant_id", "jurisdiction", "holiday_date"),
        Index("idx_holidays_tenant_year", "tenant_id", "year"),
    )


# ──────────────────────────────────────────────────────────────
# COMP 1 & 2 — Docket entries
# ──────────────────────────────────────────────────────────────

class DocketEntry(Base):
    __tablename__ = "docket_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    matter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    court_system: Mapped[str] = mapped_column(String(100), nullable=False)
    case_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    docket_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    entry_text: Mapped[str] = mapped_column(Text, nullable=False)
    entry_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    document_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    raw_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_docket_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_docket_tenant_source", "tenant_id", "source"),
        Index("idx_docket_tenant_type", "tenant_id", "entry_type"),
    )


# ──────────────────────────────────────────────────────────────
# COMP 5 — Scheduling orders
# ──────────────────────────────────────────────────────────────

class SchedulingOrder(Base):
    __tablename__ = "scheduling_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    matter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    docket_entry_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    document_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending_review")
    extracted_dates_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    confirmed_by_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ai_extraction_job_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_sched_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_sched_tenant_status", "tenant_id", "status"),
    )


class SchedulingOrderDate(Base):
    __tablename__ = "scheduling_order_dates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    scheduling_order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    date_label: Mapped[str] = mapped_column(String(500), nullable=False)
    extracted_date: Mapped[date] = mapped_column(Date, nullable=False)
    confirmed_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_by_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    event_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_sched_dates_tenant_order", "tenant_id", "scheduling_order_id"),
    )


# ──────────────────────────────────────────────────────────────
# COMP 4 & 6 — Deadlines
# ──────────────────────────────────────────────────────────────

class Deadline(Base):
    __tablename__ = "deadlines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    matter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rule_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    anchor_date: Mapped[date] = mapped_column(Date, nullable=False)
    anchor_description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    deadline_date: Mapped[date] = mapped_column(Date, nullable=False)
    deadline_description: Mapped[str] = mapped_column(String(500), nullable=False)
    derivation_path: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    status: Mapped[str] = mapped_column(String(50), default="active")
    calendar_event_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    scheduling_order_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    scheduling_order_date_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    parent_deadline_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    service_method: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    service_adjustment_days: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_by_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_deadlines_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_deadlines_tenant_date", "tenant_id", "deadline_date"),
        Index("idx_deadlines_tenant_status", "tenant_id", "status"),
    )


# ──────────────────────────────────────────────────────────────
# COMP 7 — Calendar cross-check log
# ──────────────────────────────────────────────────────────────

class CalendarCrossCheckLog(Base):
    __tablename__ = "calendar_cross_check_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    check_date: Mapped[date] = mapped_column(Date, nullable=False)
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)
    system_event_count: Mapped[int] = mapped_column(Integer, default=0)
    admin_event_count: Mapped[int] = mapped_column(Integer, default=0)
    missing_in_admin: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    missing_in_system: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    date_conflicts: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    total_discrepancies: Mapped[int] = mapped_column(Integer, default=0)
    critical_count: Mapped[int] = mapped_column(Integer, default=0)
    digest_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    digest_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_crosscheck_tenant_date", "tenant_id", "check_date"),
    )


# ──────────────────────────────────────────────────────────────
# COMP 8 — Filing log
# ──────────────────────────────────────────────────────────────

class FilingLog(Base):
    __tablename__ = "filing_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    matter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    court_system: Mapped[str] = mapped_column(String(100), nullable=False)
    court_name: Mapped[str] = mapped_column(String(200), nullable=False)
    case_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    filing_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    prepared_by_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    submitted_by_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    attorney_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    certification_form_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    submission_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    response_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    confirmation_number: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    receipt_document_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    response_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_filing_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_filing_tenant_status", "tenant_id", "status"),
    )


# ──────────────────────────────────────────────────────────────
# COMP 9 — AI Certification Compliance
# ──────────────────────────────────────────────────────────────

class CourtAIRule(Base):
    __tablename__ = "court_ai_rules"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(100), nullable=False)
    court_name: Mapped[str] = mapped_column(String(300), nullable=False)
    order_title: Mapped[str] = mapped_column(String(500), nullable=False)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    certification_required: Mapped[bool] = mapped_column(Boolean, default=True)
    certification_template_key: Mapped[str] = mapped_column(String(100), nullable=False)
    specific_requirements: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    form_fields: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_ai_rules_tenant_jurisdiction", "tenant_id", "jurisdiction"),
        Index("idx_ai_rules_tenant_court", "tenant_id", "court_name"),
    )


class AIContributionLog(Base):
    __tablename__ = "ai_contribution_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    matter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ai_tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    ai_model: Mapped[str] = mapped_column(String(200), nullable=False)
    module: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_category: Mapped[str] = mapped_column(String(200), nullable=False)
    usage_description: Mapped[str] = mapped_column(Text, nullable=False)
    ai_api_call_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_ai_contrib_tenant_doc", "tenant_id", "document_id"),
        Index("idx_ai_contrib_tenant_matter", "tenant_id", "matter_id"),
    )


class CitationVerificationLog(Base):
    __tablename__ = "citation_verification_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    matter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    citation_text: Mapped[str] = mapped_column(String(500), nullable=False)
    verification_source: Mapped[str] = mapped_column(String(100), nullable=False)
    verification_status: Mapped[str] = mapped_column(String(50), nullable=False)
    verification_details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    verified_by_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_cite_verify_tenant_doc", "tenant_id", "document_id"),
    )


class AICertificationForm(Base):
    __tablename__ = "ai_certification_forms"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    matter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    court_ai_rule_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    form_template_key: Mapped[str] = mapped_column(String(100), nullable=False)
    generated_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    generated_document_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    signed_document_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="generated")
    attorney_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_cert_forms_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_cert_forms_tenant_doc", "tenant_id", "document_id"),
    )


# ──────────────────────────────────────────────────────────────
# COMP 10 — SOL tracker
# ──────────────────────────────────────────────────────────────

class SOLRecord(Base):
    __tablename__ = "sol_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    matter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    claim_description: Mapped[str] = mapped_column(Text, nullable=False)
    cause_of_action: Mapped[str] = mapped_column(String(300), nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(100), nullable=False)
    limitation_period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    accrual_date: Mapped[date] = mapped_column(Date, nullable=False)
    computed_expiration_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_tolled: Mapped[bool] = mapped_column(Boolean, default=False)
    toll_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    toll_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    toll_end: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    tolled_days: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    deactivated_by_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    deactivation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    deactivated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_alert_level: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_alert_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    calendar_event_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_sol_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_sol_tenant_status", "tenant_id", "status"),
        Index("idx_sol_tenant_expiration", "tenant_id", "computed_expiration_date"),
    )
