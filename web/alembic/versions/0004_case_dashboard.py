"""Chat 4 — Case Dashboard tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "billing_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── COMP 5: Conflict of Interest ──────────────────────────
    op.create_table(
        "conflict_checks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", sa.BigInteger, sa.ForeignKey("matters.id"), nullable=True),
        sa.Column("checked_by", sa.BigInteger, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("check_type", sa.String(50), nullable=False, server_default="new_matter"),
        sa.Column("parties_checked", sa.JSON, nullable=False),
        sa.Column("results", sa.JSON, nullable=False),
        sa.Column("overall_result", sa.Enum("clear", "potential", "hard_conflict", name="conflict_result_enum"), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Index("idx_conflict_tenant_matter", "tenant_id", "matter_id"),
    )

    op.create_table(
        "conflict_waivers",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("conflict_check_id", sa.BigInteger, sa.ForeignKey("conflict_checks.id"), nullable=False),
        sa.Column("waived_by", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("documented_reason", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Index("idx_waiver_tenant", "tenant_id", "conflict_check_id"),
    )

    # ── COMP 2: Causes of Action ──────────────────────────────
    op.create_table(
        "causes_of_action",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", sa.BigInteger, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("count_number", sa.Integer, nullable=True),
        sa.Column("status", sa.Enum("established", "contested", "challenged", "not_developed", name="coa_status_enum"), nullable=False, server_default="not_developed"),
        sa.Column("ai_summary", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.Index("idx_coa_tenant_matter", "tenant_id", "matter_id"),
    )

    op.create_table(
        "coa_elements",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("cause_of_action_id", sa.BigInteger, sa.ForeignKey("causes_of_action.id"), nullable=False),
        sa.Column("element_name", sa.String(500), nullable=False),
        sa.Column("status", sa.Enum("established", "contested", "challenged", "not_developed", name="coa_element_status_enum"), nullable=False, server_default="not_developed"),
        sa.Column("supporting_evidence", sa.JSON, nullable=True),
        sa.Column("undermining_evidence", sa.JSON, nullable=True),
        sa.Column("discovery_gaps", sa.JSON, nullable=True),
        sa.Column("pending_motions", sa.JSON, nullable=True),
        sa.Column("attorney_override_status", sa.String(50), nullable=True),
        sa.Column("attorney_override_note", sa.Text, nullable=True),
        sa.Column("overridden_by", sa.BigInteger, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("overridden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.Index("idx_coa_elem_tenant", "tenant_id", "cause_of_action_id"),
    )

    # ── COMP 2: AI Case Summaries ─────────────────────────────
    op.create_table(
        "matter_summaries",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", sa.BigInteger, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("overview_paragraph", sa.Text, nullable=True),
        sa.Column("critical_issues_paragraph", sa.Text, nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("doc_count_at_generation", sa.Integer, nullable=False, server_default="0"),
        sa.Column("job_id", sa.String(255), nullable=True),
        sa.Column("status", sa.Enum("pending", "processing", "complete", "failed", name="summary_status_enum"), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Index("idx_summary_tenant_matter", "tenant_id", "matter_id"),
    )

    # ── COMP 6: Critical Date Memos ───────────────────────────
    op.create_table(
        "critical_date_memos",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", sa.BigInteger, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("source_document_id", sa.BigInteger, sa.ForeignKey("documents.id"), nullable=True),
        sa.Column("memo_document_id", sa.BigInteger, sa.ForeignKey("documents.id"), nullable=True),
        sa.Column("dates_extracted", sa.JSON, nullable=True),
        sa.Column("status", sa.Enum("pending", "processing", "complete", "failed", name="memo_status_enum"), nullable=False, server_default="pending"),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Index("idx_cdm_tenant_matter", "tenant_id", "matter_id"),
    )

    # ── COMP 7: Title Date Comparisons ────────────────────────
    op.create_table(
        "title_date_comparisons",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", sa.BigInteger, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("memo_id", sa.BigInteger, sa.ForeignKey("critical_date_memos.id"), nullable=False),
        sa.Column("title_document_id", sa.BigInteger, sa.ForeignKey("documents.id"), nullable=True),
        sa.Column("comparison_rows", sa.JSON, nullable=True),
        sa.Column("status", sa.Enum("pending", "processing", "complete", "failed", name="comparison_status_enum"), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Index("idx_tdc_tenant_matter", "tenant_id", "matter_id"),
    )

    # ── COMP 8: Tasks ─────────────────────────────────────────
    op.create_table(
        "tasks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", sa.BigInteger, sa.ForeignKey("matters.id"), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("source", sa.Enum("manual", "ai_commitment", "conflict", "title_discrepancy", "system", name="task_source_enum"), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.String(255), nullable=True),
        sa.Column("priority", sa.Enum("critical", "high", "medium", "low", name="task_priority_enum"), nullable=False, server_default="medium"),
        sa.Column("status", sa.Enum("open", "in_progress", "review", "complete", "cancelled", name="task_status_enum"), nullable=False, server_default="open"),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.BigInteger, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.Index("idx_task_tenant_matter", "tenant_id", "matter_id"),
        sa.Index("idx_task_tenant_status", "tenant_id", "status"),
        sa.Index("idx_task_tenant_due", "tenant_id", "due_date"),
    )

    op.create_table(
        "task_assignments",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("task_id", sa.BigInteger, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("assigned_by", sa.BigInteger, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Index("idx_ta_tenant_task", "tenant_id", "task_id"),
        sa.Index("idx_ta_tenant_user", "tenant_id", "user_id"),
    )

    # ── COMP 9: Communication Log ─────────────────────────────
    op.create_table(
        "communication_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", sa.BigInteger, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("contact_id", sa.BigInteger, sa.ForeignKey("contacts.id"), nullable=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("channel", sa.Enum("email", "phone", "sms", "portal", "in_person", "letter", name="comm_channel_enum"), nullable=False),
        sa.Column("direction", sa.Enum("inbound", "outbound", name="comm_direction_enum"), nullable=False),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("external_ref", sa.String(500), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("logged_by", sa.BigInteger, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Index("idx_comm_tenant_matter", "tenant_id", "matter_id"),
        sa.Index("idx_comm_tenant_date", "tenant_id", "occurred_at"),
    )

    # ── COMP 10: Engagement Letters ───────────────────────────
    op.create_table(
        "engagement_letters",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", sa.BigInteger, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("document_id", sa.BigInteger, sa.ForeignKey("documents.id"), nullable=True),
        sa.Column("status", sa.Enum("draft", "sent", "signed", "expired", "declined", name="engagement_status_enum"), nullable=False, server_default="draft"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signer_name", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.Index("idx_el_tenant_matter", "tenant_id", "matter_id"),
        sa.Index("idx_el_tenant_status", "tenant_id", "status"),
    )

    # ── COMP 11: Settlement Tracking ──────────────────────────
    op.create_table(
        "settlements",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", sa.BigInteger, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("offer_type", sa.Enum("demand", "offer", "counteroffer", "accepted", "rejected", name="offer_type_enum"), nullable=False),
        sa.Column("amount", sa.Numeric(15, 2), nullable=True),
        sa.Column("offered_by", sa.String(255), nullable=True),
        sa.Column("terms_summary", sa.Text, nullable=True),
        sa.Column("document_id", sa.BigInteger, sa.ForeignKey("documents.id"), nullable=True),
        sa.Column("offered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Index("idx_settle_tenant_matter", "tenant_id", "matter_id"),
    )

    # ── COMP 11: Expert Witnesses ─────────────────────────────
    op.create_table(
        "expert_witnesses",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", sa.BigInteger, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("contact_id", sa.BigInteger, sa.ForeignKey("contacts.id"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("specialty", sa.String(500), nullable=True),
        sa.Column("credentials", sa.Text, nullable=True),
        sa.Column("retained_by", sa.Enum("us", "opposing", "court", name="retained_by_enum"), nullable=False, server_default="us"),
        sa.Column("hourly_rate", sa.Numeric(10, 2), nullable=True),
        sa.Column("report_due", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_filed", sa.Boolean, server_default="0"),
        sa.Column("deposition_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Enum("identified", "retained", "report_pending", "report_filed", "deposed", "withdrawn", name="expert_status_enum"), nullable=False, server_default="identified"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.Index("idx_ew_tenant_matter", "tenant_id", "matter_id"),
    )

    # ── COMP 11: Mediations ───────────────────────────────────
    op.create_table(
        "mediations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", sa.BigInteger, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("mediator_name", sa.String(255), nullable=True),
        sa.Column("mediator_contact_id", sa.BigInteger, sa.ForeignKey("contacts.id"), nullable=True),
        sa.Column("scheduled_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("location", sa.String(500), nullable=True),
        sa.Column("brief_document_id", sa.BigInteger, sa.ForeignKey("documents.id"), nullable=True),
        sa.Column("outcome", sa.Enum("pending", "settled", "impasse", "continued", "cancelled", name="mediation_outcome_enum"), nullable=False, server_default="pending"),
        sa.Column("settlement_amount", sa.Numeric(15, 2), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.Index("idx_med_tenant_matter", "tenant_id", "matter_id"),
    )

    # ── COMP 1: Intake OCR tracking ──────────────────────────
    op.create_table(
        "intake_sessions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("initiated_by", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.Enum("uploading", "extracting", "qc_review", "conflict_check", "confirmed", "cancelled", name="intake_status_enum"), nullable=False, server_default="uploading"),
        sa.Column("extracted_fields", sa.JSON, nullable=True),
        sa.Column("ocr_results", sa.JSON, nullable=True),
        sa.Column("matter_id", sa.BigInteger, sa.ForeignKey("matters.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.Index("idx_intake_tenant", "tenant_id", "status"),
    )


def downgrade() -> None:
    tables = [
        "intake_sessions", "mediations", "expert_witnesses", "settlements",
        "engagement_letters", "communication_log", "task_assignments", "tasks",
        "title_date_comparisons", "critical_date_memos", "matter_summaries",
        "coa_elements", "causes_of_action", "conflict_waivers", "conflict_checks",
    ]
    for t in tables:
        op.drop_table(t)
    for e in [
        "conflict_result_enum", "coa_status_enum", "coa_element_status_enum",
        "summary_status_enum", "memo_status_enum", "comparison_status_enum",
        "task_source_enum", "task_priority_enum", "task_status_enum",
        "comm_channel_enum", "comm_direction_enum", "engagement_status_enum",
        "offer_type_enum", "retained_by_enum", "expert_status_enum",
        "mediation_outcome_enum", "intake_status_enum",
    ]:
        op.execute(f"DROP TYPE IF EXISTS {e}")
