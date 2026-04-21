"""Court & Calendar module tables

Revision ID: 003_court_calendar
Revises: 002 (assumes prior migrations for core tables)
Create Date: 2026-03-26

Tables created:
  - court_rules
  - court_holidays
  - docket_entries
  - scheduling_orders
  - scheduling_order_dates
  - deadlines
  - calendar_cross_check_log
  - filing_log
  - sol_records
  - court_ai_rules
  - ai_contribution_log
  - citation_verification_log
  - ai_certification_forms
"""

from alembic import op
import sqlalchemy as sa

revision = "003_court_calendar"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ──────────────────────────────────────────────
    # COMP 3: Rules database
    # ──────────────────────────────────────────────
    op.create_table(
        "court_rules",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("jurisdiction", sa.String(100), nullable=False),
        sa.Column("rule_set", sa.String(50), nullable=False, comment="frcp|trcp|ccp|crcp|local"),
        sa.Column("rule_number", sa.String(50), nullable=False),
        sa.Column("rule_title", sa.String(500), nullable=False),
        sa.Column("triggering_event", sa.String(200), nullable=False),
        sa.Column("deadline_description", sa.String(500), nullable=False),
        sa.Column("duration_days", sa.Integer, nullable=False),
        sa.Column("duration_type", sa.String(20), nullable=False, comment="calendar|business"),
        sa.Column("direction", sa.String(10), nullable=False, comment="before|after"),
        sa.Column("service_method_adjustments", sa.JSON, nullable=True, comment="JSON: {email: 3, mail: 7, ...}"),
        sa.Column("triggers_rules", sa.JSON, nullable=True, comment="JSON array of rule_ids triggered by this deadline"),
        sa.Column("is_delta", sa.Boolean, default=False, comment="True if this overrides a federal rule"),
        sa.Column("overrides_rule_id", sa.BigInteger, nullable=True, comment="FK to federal rule being overridden"),
        sa.Column("court_district", sa.String(200), nullable=True, comment="For local rules: specific district/county"),
        sa.Column("effective_date", sa.Date, nullable=True),
        sa.Column("superseded_date", sa.Date, nullable=True),
        sa.Column("version", sa.Integer, default=1),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.Index("idx_court_rules_tenant_jurisdiction", "tenant_id", "jurisdiction"),
        sa.Index("idx_court_rules_tenant_ruleset", "tenant_id", "rule_set"),
        sa.Index("idx_court_rules_trigger", "tenant_id", "triggering_event"),
    )

    op.create_table(
        "court_holidays",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("jurisdiction", sa.String(100), nullable=False, comment="federal|TX|CA|CO|..."),
        sa.Column("court_district", sa.String(200), nullable=True, comment="Specific court if court-specific closure"),
        sa.Column("holiday_date", sa.Date, nullable=False),
        sa.Column("holiday_name", sa.String(200), nullable=False),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Index("idx_holidays_tenant_jurisdiction_date", "tenant_id", "jurisdiction", "holiday_date"),
        sa.Index("idx_holidays_tenant_year", "tenant_id", "year"),
    )

    # ──────────────────────────────────────────────
    # COMP 1 & 2: Docket entries
    # ──────────────────────────────────────────────
    op.create_table(
        "docket_entries",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", sa.BigInteger, nullable=False),
        sa.Column("source", sa.String(50), nullable=False, comment="tyler|pacer|courtlistener|manual"),
        sa.Column("court_system", sa.String(100), nullable=False),
        sa.Column("case_number", sa.String(100), nullable=True),
        sa.Column("docket_number", sa.String(100), nullable=True),
        sa.Column("entry_date", sa.Date, nullable=False),
        sa.Column("entry_text", sa.Text, nullable=False),
        sa.Column("entry_type", sa.String(100), nullable=True, comment="filing|order|notice|service|scheduling_order"),
        sa.Column("document_id", sa.BigInteger, nullable=True, comment="FK to documents table after DMS download"),
        sa.Column("raw_data", sa.JSON, nullable=True),
        sa.Column("processed", sa.Boolean, default=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Index("idx_docket_tenant_matter", "tenant_id", "matter_id"),
        sa.Index("idx_docket_tenant_source", "tenant_id", "source"),
        sa.Index("idx_docket_tenant_type", "tenant_id", "entry_type"),
    )

    # ──────────────────────────────────────────────
    # COMP 5: Scheduling orders
    # ──────────────────────────────────────────────
    op.create_table(
        "scheduling_orders",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", sa.BigInteger, nullable=False),
        sa.Column("docket_entry_id", sa.BigInteger, nullable=True),
        sa.Column("document_id", sa.BigInteger, nullable=True),
        sa.Column("status", sa.String(50), nullable=False, default="pending_review",
                  comment="pending_review|dates_extracted|attorney_confirmed|deadlines_created"),
        sa.Column("extracted_dates_json", sa.JSON, nullable=True, comment="AI-extracted dates before confirmation"),
        sa.Column("confirmed_by_user_id", sa.BigInteger, nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ai_extraction_job_id", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.Index("idx_sched_tenant_matter", "tenant_id", "matter_id"),
        sa.Index("idx_sched_tenant_status", "tenant_id", "status"),
    )

    op.create_table(
        "scheduling_order_dates",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("scheduling_order_id", sa.BigInteger, nullable=False),
        sa.Column("date_label", sa.String(500), nullable=False, comment="e.g. Discovery Cutoff, Dispositive Motion Deadline"),
        sa.Column("extracted_date", sa.Date, nullable=False),
        sa.Column("confirmed_date", sa.Date, nullable=True),
        sa.Column("is_confirmed", sa.Boolean, default=False),
        sa.Column("confirmed_by_user_id", sa.BigInteger, nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_type", sa.String(100), nullable=True, comment="Maps to court_rules.triggering_event"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Index("idx_sched_dates_tenant_order", "tenant_id", "scheduling_order_id"),
    )

    # ──────────────────────────────────────────────
    # COMP 4 & 6: Deadlines (computed from rules engine)
    # ──────────────────────────────────────────────
    op.create_table(
        "deadlines",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", sa.BigInteger, nullable=False),
        sa.Column("rule_id", sa.BigInteger, nullable=True, comment="FK to court_rules"),
        sa.Column("anchor_date", sa.Date, nullable=False),
        sa.Column("anchor_description", sa.String(500), nullable=True),
        sa.Column("deadline_date", sa.Date, nullable=False),
        sa.Column("deadline_description", sa.String(500), nullable=False),
        sa.Column("derivation_path", sa.JSON, nullable=True, comment="Chain: anchor→rule→deadline"),
        sa.Column("priority", sa.String(20), default="normal", comment="low|normal|high|critical"),
        sa.Column("status", sa.String(50), default="active", comment="active|completed|vacated|extended"),
        sa.Column("calendar_event_id", sa.String(200), nullable=True, comment="CalendarService event ID"),
        sa.Column("scheduling_order_id", sa.BigInteger, nullable=True),
        sa.Column("scheduling_order_date_id", sa.BigInteger, nullable=True),
        sa.Column("parent_deadline_id", sa.BigInteger, nullable=True, comment="For derivative chains"),
        sa.Column("service_method", sa.String(50), nullable=True),
        sa.Column("service_adjustment_days", sa.Integer, default=0),
        sa.Column("confirmed_by_user_id", sa.BigInteger, nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.Index("idx_deadlines_tenant_matter", "tenant_id", "matter_id"),
        sa.Index("idx_deadlines_tenant_date", "tenant_id", "deadline_date"),
        sa.Index("idx_deadlines_tenant_status", "tenant_id", "status"),
    )

    # ──────────────────────────────────────────────
    # COMP 7: Calendar cross-check log
    # ──────────────────────────────────────────────
    op.create_table(
        "calendar_cross_check_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("check_date", sa.Date, nullable=False),
        sa.Column("window_start", sa.Date, nullable=False),
        sa.Column("window_end", sa.Date, nullable=False),
        sa.Column("system_event_count", sa.Integer, default=0),
        sa.Column("admin_event_count", sa.Integer, default=0),
        sa.Column("missing_in_admin", sa.JSON, nullable=True),
        sa.Column("missing_in_system", sa.JSON, nullable=True),
        sa.Column("date_conflicts", sa.JSON, nullable=True),
        sa.Column("total_discrepancies", sa.Integer, default=0),
        sa.Column("critical_count", sa.Integer, default=0),
        sa.Column("digest_sent", sa.Boolean, default=False),
        sa.Column("digest_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Index("idx_crosscheck_tenant_date", "tenant_id", "check_date"),
    )

    # ──────────────────────────────────────────────
    # COMP 8: Filing log
    # ──────────────────────────────────────────────
    op.create_table(
        "filing_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", sa.BigInteger, nullable=False),
        sa.Column("document_id", sa.BigInteger, nullable=False),
        sa.Column("court_system", sa.String(100), nullable=False, comment="tyler|cmecf|fileserve|onelegal"),
        sa.Column("court_name", sa.String(200), nullable=False),
        sa.Column("case_number", sa.String(100), nullable=True),
        sa.Column("filing_type", sa.String(100), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, comment="prepared|submitted|accepted|rejected|error"),
        sa.Column("prepared_by_user_id", sa.BigInteger, nullable=True),
        sa.Column("submitted_by_user_id", sa.BigInteger, nullable=True),
        sa.Column("attorney_user_id", sa.BigInteger, nullable=False),
        sa.Column("certification_form_id", sa.BigInteger, nullable=True, comment="FK to ai_certification_forms"),
        sa.Column("submission_payload", sa.JSON, nullable=True),
        sa.Column("response_data", sa.JSON, nullable=True),
        sa.Column("confirmation_number", sa.String(200), nullable=True),
        sa.Column("rejection_reason", sa.Text, nullable=True),
        sa.Column("receipt_document_id", sa.BigInteger, nullable=True, comment="DMS doc ID for filing receipt"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.Index("idx_filing_tenant_matter", "tenant_id", "matter_id"),
        sa.Index("idx_filing_tenant_status", "tenant_id", "status"),
    )

    # ──────────────────────────────────────────────
    # COMP 9: AI Court Certification Compliance
    # ──────────────────────────────────────────────
    op.create_table(
        "court_ai_rules",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("jurisdiction", sa.String(100), nullable=False),
        sa.Column("court_name", sa.String(300), nullable=False),
        sa.Column("order_title", sa.String(500), nullable=False),
        sa.Column("order_date", sa.Date, nullable=False),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column("certification_required", sa.Boolean, default=True),
        sa.Column("certification_template_key", sa.String(100), nullable=False,
                  comment="Key to identify which form generator to use: ed_tex_2025|denton_county_2025"),
        sa.Column("specific_requirements", sa.JSON, nullable=True,
                  comment="JSON with court-specific fields and requirements"),
        sa.Column("form_fields", sa.JSON, nullable=True,
                  comment="JSON defining auto-population field mappings"),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.Index("idx_ai_rules_tenant_jurisdiction", "tenant_id", "jurisdiction"),
        sa.Index("idx_ai_rules_tenant_court", "tenant_id", "court_name"),
    )

    op.create_table(
        "ai_contribution_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("document_id", sa.BigInteger, nullable=False),
        sa.Column("matter_id", sa.BigInteger, nullable=False),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("ai_tool_name", sa.String(200), nullable=False, comment="e.g. Anthropic Claude"),
        sa.Column("ai_model", sa.String(200), nullable=False, comment="e.g. claude-sonnet-4-5"),
        sa.Column("module", sa.String(100), nullable=False, comment="Which platform module made the call"),
        sa.Column("prompt_category", sa.String(200), nullable=False,
                  comment="e.g. draft_argument, citation_check, sanity_check, structural_analysis"),
        sa.Column("usage_description", sa.Text, nullable=False,
                  comment="Human-readable description of how AI was used"),
        sa.Column("ai_api_call_id", sa.BigInteger, nullable=True, comment="FK to ai_api_calls"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Index("idx_ai_contrib_tenant_doc", "tenant_id", "document_id"),
        sa.Index("idx_ai_contrib_tenant_matter", "tenant_id", "matter_id"),
    )

    op.create_table(
        "citation_verification_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("document_id", sa.BigInteger, nullable=False),
        sa.Column("matter_id", sa.BigInteger, nullable=False),
        sa.Column("citation_text", sa.String(500), nullable=False),
        sa.Column("verification_source", sa.String(100), nullable=False, comment="shepards|keycite|manual"),
        sa.Column("verification_status", sa.String(50), nullable=False, comment="verified|warning|overruled|not_found"),
        sa.Column("verification_details", sa.JSON, nullable=True),
        sa.Column("verified_by_user_id", sa.BigInteger, nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Index("idx_cite_verify_tenant_doc", "tenant_id", "document_id"),
    )

    op.create_table(
        "ai_certification_forms",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", sa.BigInteger, nullable=False),
        sa.Column("document_id", sa.BigInteger, nullable=False, comment="The document being filed"),
        sa.Column("court_ai_rule_id", sa.BigInteger, nullable=False, comment="FK to court_ai_rules"),
        sa.Column("form_template_key", sa.String(100), nullable=False),
        sa.Column("generated_data", sa.JSON, nullable=False, comment="Auto-populated form field values"),
        sa.Column("generated_document_id", sa.BigInteger, nullable=True, comment="DMS doc ID of generated .docx"),
        sa.Column("signed_document_id", sa.BigInteger, nullable=True, comment="DMS doc ID of signed version"),
        sa.Column("status", sa.String(50), nullable=False, default="generated",
                  comment="generated|attorney_review|signed|filed"),
        sa.Column("attorney_user_id", sa.BigInteger, nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.Index("idx_cert_forms_tenant_matter", "tenant_id", "matter_id"),
        sa.Index("idx_cert_forms_tenant_doc", "tenant_id", "document_id"),
    )

    # ──────────────────────────────────────────────
    # COMP 10: SOL tracker
    # ──────────────────────────────────────────────
    op.create_table(
        "sol_records",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", sa.BigInteger, nullable=False),
        sa.Column("claim_description", sa.Text, nullable=False),
        sa.Column("cause_of_action", sa.String(300), nullable=False),
        sa.Column("jurisdiction", sa.String(100), nullable=False),
        sa.Column("limitation_period_days", sa.Integer, nullable=False),
        sa.Column("accrual_date", sa.Date, nullable=False),
        sa.Column("computed_expiration_date", sa.Date, nullable=False),
        sa.Column("is_tolled", sa.Boolean, default=False),
        sa.Column("toll_reason", sa.Text, nullable=True),
        sa.Column("toll_start", sa.Date, nullable=True),
        sa.Column("toll_end", sa.Date, nullable=True),
        sa.Column("tolled_days", sa.Integer, default=0),
        sa.Column("status", sa.String(50), nullable=False, default="active",
                  comment="active|expired|resolved|deactivated"),
        sa.Column("deactivated_by_user_id", sa.BigInteger, nullable=True),
        sa.Column("deactivation_reason", sa.Text, nullable=True),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_alert_level", sa.Integer, nullable=True, comment="Days-before level of last alert sent"),
        sa.Column("last_alert_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("calendar_event_id", sa.String(200), nullable=True),
        sa.Column("created_by_user_id", sa.BigInteger, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.Index("idx_sol_tenant_matter", "tenant_id", "matter_id"),
        sa.Index("idx_sol_tenant_status", "tenant_id", "status"),
        sa.Index("idx_sol_tenant_expiration", "tenant_id", "computed_expiration_date"),
    )


def downgrade() -> None:
    op.drop_table("sol_records")
    op.drop_table("ai_certification_forms")
    op.drop_table("citation_verification_log")
    op.drop_table("ai_contribution_log")
    op.drop_table("court_ai_rules")
    op.drop_table("filing_log")
    op.drop_table("calendar_cross_check_log")
    op.drop_table("deadlines")
    op.drop_table("scheduling_order_dates")
    op.drop_table("scheduling_orders")
    op.drop_table("docket_entries")
    op.drop_table("court_holidays")
    op.drop_table("court_rules")
