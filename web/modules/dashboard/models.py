"""Dashboard ORM Models — corrected for PostgreSQL UUID primary keys.

matters.id = UUID
documents.id = UUID
tasks.id = BIGINT (correct — unchanged)
intake_sessions.id = BIGINT (correct — unchanged)
users.id = BIGINT (correct — unchanged)
contacts.id = BIGINT (correct — unchanged)
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer,
    JSON, Numeric, String, Text, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db.base import Base


# ── COMP 5: Conflict of Interest ─────────────────────────────

class ConflictCheck(Base):
    __tablename__ = "conflict_checks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    matter_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("matters.id"))
    checked_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"))
    check_type: Mapped[str] = mapped_column(String(50), default="new_matter")
    parties_checked: Mapped[dict] = mapped_column(JSON, nullable=False)
    results: Mapped[dict] = mapped_column(JSON, nullable=False)
    overall_result: Mapped[str] = mapped_column(
        Enum("clear", "potential", "hard_conflict", name="conflict_result_enum"), nullable=False
    )
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    waivers: Mapped[list["ConflictWaiver"]] = relationship(back_populates="conflict_check")

    __table_args__ = (Index("idx_conflict_tenant_matter", "tenant_id", "matter_id"),)


class ConflictWaiver(Base):
    __tablename__ = "conflict_waivers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    conflict_check_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conflict_checks.id"), nullable=False)
    waived_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    documented_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conflict_check: Mapped["ConflictCheck"] = relationship(back_populates="waivers")


# ── COMP 2: AI Case Summary & Causes of Action ───────────────

class MatterSummary(Base):
    __tablename__ = "matter_summaries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    matter_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("matters.id"), nullable=False)
    overview_paragraph: Mapped[Optional[str]] = mapped_column(Text)
    critical_issues_paragraph: Mapped[Optional[str]] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    doc_count_at_generation: Mapped[int] = mapped_column(Integer, default=0)
    job_id: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        Enum("pending", "processing", "complete", "failed", name="summary_status_enum"),
        default="pending",
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("idx_summary_tenant_matter", "tenant_id", "matter_id"),)


class CauseOfAction(Base):
    __tablename__ = "causes_of_action"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    matter_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("matters.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    count_number: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        Enum("established", "contested", "challenged", "not_developed", name="coa_status_enum"),
        default="not_developed",
    )
    ai_summary: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    elements: Mapped[list["CoaElement"]] = relationship(back_populates="cause_of_action", cascade="all, delete-orphan")

    __table_args__ = (Index("idx_coa_tenant_matter", "tenant_id", "matter_id"),)


class CoaElement(Base):
    __tablename__ = "coa_elements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    cause_of_action_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("causes_of_action.id"), nullable=False)
    element_name: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("established", "contested", "challenged", "not_developed", name="coa_element_status_enum"),
        default="not_developed",
    )
    supporting_evidence: Mapped[Optional[dict]] = mapped_column(JSON)
    undermining_evidence: Mapped[Optional[dict]] = mapped_column(JSON)
    discovery_gaps: Mapped[Optional[dict]] = mapped_column(JSON)
    pending_motions: Mapped[Optional[dict]] = mapped_column(JSON)
    attorney_override_status: Mapped[Optional[str]] = mapped_column(String(50))
    attorney_override_note: Mapped[Optional[str]] = mapped_column(Text)
    overridden_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"))
    overridden_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    cause_of_action: Mapped["CauseOfAction"] = relationship(back_populates="elements")

    __table_args__ = (Index("idx_coa_elem_tenant", "tenant_id", "cause_of_action_id"),)


# ── COMP 6: Critical Date Memos ──────────────────────────────

class CriticalDateMemo(Base):
    __tablename__ = "critical_date_memos"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    matter_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("matters.id"), nullable=False)
    source_document_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("documents.id"))
    memo_document_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("documents.id"))
    dates_extracted: Mapped[Optional[dict]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        Enum("pending", "processing", "complete", "failed", name="memo_status_enum"),
        default="pending",
    )
    generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    comparisons: Mapped[list["TitleDateComparison"]] = relationship(back_populates="memo")

    __table_args__ = (Index("idx_cdm_tenant_matter", "tenant_id", "matter_id"),)


# ── COMP 7: Title Date Comparisons ───────────────────────────

class TitleDateComparison(Base):
    __tablename__ = "title_date_comparisons"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    matter_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("matters.id"), nullable=False)
    memo_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("critical_date_memos.id"), nullable=False)
    title_document_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("documents.id"))
    comparison_rows: Mapped[Optional[dict]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        Enum("pending", "processing", "complete", "failed", name="comparison_status_enum"),
        default="pending",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    memo: Mapped["CriticalDateMemo"] = relationship(back_populates="comparisons")

    __table_args__ = (Index("idx_tdc_tenant_matter", "tenant_id", "matter_id"),)


# ── COMP 8: Tasks ─────────────────────────────────────────────

class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    matter_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("matters.id"))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[str] = mapped_column(
        Enum("manual", "ai_commitment", "conflict", "title_discrepancy", "system", name="task_source_enum"),
        default="manual",
    )
    source_ref: Mapped[Optional[str]] = mapped_column(String(255))
    priority: Mapped[str] = mapped_column(
        Enum("critical", "high", "medium", "low", name="task_priority_enum"),
        default="medium",
    )
    status: Mapped[str] = mapped_column(
        Enum("open", "in_progress", "review", "complete", "cancelled", name="task_status_enum"),
        default="open",
    )
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    assignments: Mapped[list["TaskAssignment"]] = relationship(back_populates="task", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_task_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_task_tenant_status", "tenant_id", "status"),
        Index("idx_task_tenant_due", "tenant_id", "due_date"),
    )


class TaskAssignment(Base):
    __tablename__ = "task_assignments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tasks.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    assigned_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped["Task"] = relationship(back_populates="assignments")

    __table_args__ = (
        Index("idx_ta_tenant_task", "tenant_id", "task_id"),
        Index("idx_ta_tenant_user", "tenant_id", "user_id"),
    )


# ── COMP 9: Communication Log ─────────────────────────────────

class CommunicationLog(Base):
    __tablename__ = "communication_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    matter_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("matters.id"), nullable=False)
    contact_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("contacts.id"))
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"))
    channel: Mapped[str] = mapped_column(
        Enum("email", "phone", "sms", "portal", "in_person", "letter", name="comm_channel_enum"),
        nullable=False,
    )
    direction: Mapped[str] = mapped_column(
        Enum("inbound", "outbound", name="comm_direction_enum"), nullable=False,
    )
    subject: Mapped[Optional[str]] = mapped_column(String(500))
    summary: Mapped[Optional[str]] = mapped_column(Text)
    external_ref: Mapped[Optional[str]] = mapped_column(String(500))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    logged_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_comm_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_comm_tenant_date", "tenant_id", "occurred_at"),
    )


# ── COMP 10: Engagement Letters ───────────────────────────────

class EngagementLetter(Base):
    __tablename__ = "engagement_letters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    matter_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("matters.id"), nullable=False)
    document_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("documents.id"))
    status: Mapped[str] = mapped_column(
        Enum("draft", "sent", "signed", "expired", "declined", name="engagement_status_enum"),
        default="draft",
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    signer_name: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_el_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_el_tenant_status", "tenant_id", "status"),
    )


# ── COMP 11: Settlements ─────────────────────────────────────

class Settlement(Base):
    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    matter_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("matters.id"), nullable=False)
    offer_type: Mapped[str] = mapped_column(
        Enum("demand", "offer", "counteroffer", "accepted", "rejected", name="offer_type_enum"),
        nullable=False,
    )
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    offered_by: Mapped[Optional[str]] = mapped_column(String(255))
    terms_summary: Mapped[Optional[str]] = mapped_column(Text)
    document_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("documents.id"))
    offered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("idx_settle_tenant_matter", "tenant_id", "matter_id"),)


# ── COMP 11: Expert Witnesses ─────────────────────────────────

class ExpertWitness(Base):
    __tablename__ = "expert_witnesses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    matter_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("matters.id"), nullable=False)
    contact_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("contacts.id"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    specialty: Mapped[Optional[str]] = mapped_column(String(500))
    credentials: Mapped[Optional[str]] = mapped_column(Text)
    retained_by: Mapped[str] = mapped_column(
        Enum("us", "opposing", "court", name="retained_by_enum"), default="us",
    )
    hourly_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    report_due: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    report_filed: Mapped[bool] = mapped_column(Boolean, default=False)
    deposition_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        Enum("identified", "retained", "report_pending", "report_filed", "deposed", "withdrawn",
             name="expert_status_enum"),
        default="identified",
    )
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("idx_ew_tenant_matter", "tenant_id", "matter_id"),)


# ── COMP 11: Mediations ──────────────────────────────────────

class Mediation(Base):
    __tablename__ = "mediations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    matter_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("matters.id"), nullable=False)
    mediator_name: Mapped[Optional[str]] = mapped_column(String(255))
    mediator_contact_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("contacts.id"))
    scheduled_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    location: Mapped[Optional[str]] = mapped_column(String(500))
    brief_document_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("documents.id"))
    outcome: Mapped[str] = mapped_column(
        Enum("pending", "settled", "impasse", "continued", "cancelled", name="mediation_outcome_enum"),
        default="pending",
    )
    settlement_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("idx_med_tenant_matter", "tenant_id", "matter_id"),)


# ── COMP 1: Intake Sessions ──────────────────────────────────

class IntakeSession(Base):
    __tablename__ = "intake_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    initiated_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("uploading", "extracting", "qc_review", "conflict_check", "confirmed", "cancelled",
             name="intake_status_enum"),
        default="uploading",
    )
    extracted_fields: Mapped[Optional[dict]] = mapped_column(JSON)
    ocr_results: Mapped[Optional[dict]] = mapped_column(JSON)
    matter_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("matters.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("idx_intake_tenant", "tenant_id", "status"),)
