"""Pass 2 — Create all missing tables for Series 2.0

Revision ID: 0012_s20_pass2_tables
Revises: ediscovery_007
Create Date: 2026-03-31

Creates all tables that exist in ORM models but were missing from the DB.
Also adds Viaticum stub tables for Series 3 feature flag support.

FK type map (confirmed against live DB):
  clients.id              = UUID
  documents.id            = UUID
  ediscovery_collections.id = UUID
  ediscovery_documents.id = UUID
  invoices.id             = UUID
  matters.id              = UUID
  tenants.id              = CHAR(36)
  users.id                = BIGINT
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012_s20_pass2_tables"
down_revision = "ediscovery_007"
branch_labels = None
depends_on = None

# Convenience aliases matching actual DB types
UUID = postgresql.UUID(as_uuid=False)


def upgrade():
    conn = op.get_bind()

    # ── Create enum types ─────────────────────────────────────────────────────
    enums = [
        ("payment_method_enum",      ["check","wire","ach","credit_card","lawpay","cash","trust"]),
        ("distribution_status_enum", ["draft","approved","distributed"]),
        ("rate_scope_enum",          ["firm","timekeeper","client","matter"]),
        ("line_type_enum",           ["time","expense","flat_fee","retainer_draw","adjustment"]),
        ("sync_direction_enum",      ["push","pull"]),
        ("sync_status_enum",         ["pending","success","error","retrying"]),
        ("trust_txn_type_enum",      ["deposit","disbursement","interest","adjustment"]),
        ("contact_type_enum",        ["client","opposing_counsel","judge","expert","vendor","court_clerk","witness","other"]),
        ("conflict_result_enum",     ["clear","potential","hard_conflict"]),
        ("summary_status_enum",      ["pending","processing","complete","failed"]),
        ("coa_status_enum",          ["established","contested","challenged","not_developed"]),
        ("coa_element_status_enum",  ["established","contested","challenged","not_developed"]),
        ("memo_status_enum",         ["pending","processing","complete","failed"]),
        ("comparison_status_enum",   ["pending","processing","complete","failed"]),
        ("task_source_enum",         ["manual","ai_commitment","conflict","title_discrepancy","system"]),
        ("task_priority_enum",       ["critical","high","medium","low"]),
        ("task_status_enum",         ["open","in_progress","review","complete","cancelled"]),
        ("comm_channel_enum",        ["email","phone","sms","portal","in_person","letter"]),
        ("comm_direction_enum",      ["inbound","outbound"]),
        ("engagement_status_enum",   ["draft","sent","signed","expired","declined"]),
        ("offer_type_enum",          ["demand","offer","counteroffer","accepted","rejected"]),
        ("retained_by_enum",         ["us","opposing","court"]),
        ("expert_status_enum",       ["identified","retained","report_pending","report_filed","deposed","withdrawn"]),
        ("mediation_outcome_enum",   ["pending","settled","impasse","continued","cancelled"]),
        ("intake_status_enum",       ["uploading","extracting","qc_review","conflict_check","confirmed","cancelled"]),
        ("production_status_enum",   ["preparing","qc_review","produced"]),
        ("production_format_enum",   ["relativity_dat","native","tiff","pdf"]),
        ("hold_status_enum",         ["active","modified","released"]),
        ("ack_status_enum",          ["pending","acknowledged","reminded","escalated"]),
        ("privilege_basis_enum",     ["attorney_client","work_product","joint_defense","common_interest","other"]),
        ("term_tier_enum",           ["tier_1","tier_2","tier_3"]),
        ("billing_qc_enum",          ["pending","pass","fail","override"]),
        ("viaticum_session_status_enum", ["active","paused","closed","exported"]),
        ("viaticum_exhibit_status_enum", ["staged","presented","withdrawn","admitted","excluded"]),
    ]
    for name, values in enums:
        exists = conn.execute(
            sa.text("SELECT 1 FROM pg_type WHERE typname = :n"), {"n": name}
        ).fetchone()
        if not exists:
            quoted = ", ".join(f"'{v}'" for v in values)
            conn.execute(sa.text(f"CREATE TYPE {name} AS ENUM ({quoted})"))

    # ── Billing tables ────────────────────────────────────────────────────────

    op.create_table("invoice_matters",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("invoice_id", UUID, sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("subtotal", sa.Numeric(12,2), nullable=False, server_default="0"),
    )
    op.create_index("idx_im_tenant_invoice", "invoice_matters", ["tenant_id","invoice_id"])
    op.create_index("idx_im_tenant_matter",  "invoice_matters", ["tenant_id","matter_id"])

    op.create_table("payments",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("invoice_id", UUID, sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("payment_date", sa.Date, nullable=False),
        sa.Column("amount", sa.Numeric(12,2), nullable=False),
        sa.Column("method", sa.String(50), nullable=False),
        sa.Column("reference_number", sa.String(255)),
        sa.Column("lawpay_payment_id", sa.String(255)),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_pay_tenant_invoice", "payments", ["tenant_id","invoice_id"])
    op.create_index("idx_pay_tenant_date",    "payments", ["tenant_id","payment_date"])

    op.create_table("distributions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("collected_amount", sa.Numeric(12,2), nullable=False, server_default="0"),
        sa.Column("origination_credit", sa.Numeric(12,2), server_default="0"),
        sa.Column("distribution_amount", sa.Numeric(12,2), nullable=False, server_default="0"),
        sa.Column("formula", sa.String(50)),
        sa.Column("status", sa.String(50), nullable=False, server_default="draft"),
        sa.Column("approved_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("approved_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_dist_tenant_period", "distributions", ["tenant_id","period"])

    op.create_table("rate_cards",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("scope", sa.String(50), nullable=False),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("client_id", UUID, sa.ForeignKey("clients.id")),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id")),
        sa.Column("hourly_rate", sa.Numeric(10,2), nullable=False),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_rc_tenant", "rate_cards", ["tenant_id"])

    op.create_table("rate_change_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("rate_card_id", sa.BigInteger, sa.ForeignKey("rate_cards.id"), nullable=False),
        sa.Column("old_rate", sa.Numeric(10,2), nullable=False),
        sa.Column("new_rate", sa.Numeric(10,2), nullable=False),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column("scope", sa.String(50), nullable=False),
        sa.Column("changed_by_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("apply_to_unbilled_wip", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_rcl_tenant", "rate_change_log", ["tenant_id"])

    op.create_table("invoice_line_items",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("invoice_id", UUID, sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("time_entry_id", UUID, sa.ForeignKey("time_entries.id")),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("line_date", sa.Date, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("timekeeper_name", sa.String(255)),
        sa.Column("hours", sa.Numeric(6,2)),
        sa.Column("rate", sa.Numeric(10,2)),
        sa.Column("amount", sa.Numeric(12,2), nullable=False),
        sa.Column("utbms_task_code", sa.String(20)),
        sa.Column("line_type", sa.String(50), nullable=False, server_default="time"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_ili_tenant_invoice", "invoice_line_items", ["tenant_id","invoice_id"])

    op.create_table("time_entry_sources",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("time_entry_id", UUID, sa.ForeignKey("time_entries.id"), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("source_data", sa.Text),
        sa.Column("captured_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("duration_seconds", sa.Integer),
        sa.Column("matter_confidence", sa.Numeric(5,4)),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_tes_tenant", "time_entry_sources", ["tenant_id"])

    op.create_table("report_definitions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("report_type", sa.String(100), nullable=False),
        sa.Column("config", postgresql.JSONB),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_rd_tenant", "report_definitions", ["tenant_id"])

    op.create_table("billing_qc_results",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("invoice_id", UUID, sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("check_type", sa.String(100), nullable=False),
        sa.Column("result", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("detail", sa.Text),
        sa.Column("reviewed_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("reviewed_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_bqc_tenant_invoice", "billing_qc_results", ["tenant_id","invoice_id"])

    op.create_table("qbo_sync_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("object_type", sa.String(50), nullable=False),
        sa.Column("object_id", sa.BigInteger, nullable=False),
        sa.Column("direction", sa.String(50), nullable=False),
        sa.Column("qbo_id", sa.String(100)),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("request_payload", sa.Text),
        sa.Column("response_payload", sa.Text),
        sa.Column("error_message", sa.Text),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_qsl_tenant", "qbo_sync_log", ["tenant_id"])

    op.create_table("qbo_mappings",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("mapping_type", sa.String(50), nullable=False),
        sa.Column("platform_code", sa.String(100), nullable=False),
        sa.Column("platform_label", sa.String(255)),
        sa.Column("qbo_id", sa.String(100), nullable=False),
        sa.Column("qbo_name", sa.String(255)),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_qm_tenant", "qbo_mappings", ["tenant_id"])

    # ── Trust tables ──────────────────────────────────────────────────────────

    op.create_table("trust_ledger",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("client_id", UUID, sa.ForeignKey("clients.id"), nullable=False, unique=True),
        sa.Column("balance", sa.Numeric(14,2), nullable=False, server_default="0"),
        sa.Column("last_reconciled_at", sa.DateTime),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_tl_tenant", "trust_ledger", ["tenant_id"])

    op.create_table("trust_transactions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("trust_ledger_id", sa.BigInteger, sa.ForeignKey("trust_ledger.id"), nullable=False),
        sa.Column("client_id", UUID, sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id")),
        sa.Column("transaction_type", sa.String(50), nullable=False),
        sa.Column("amount", sa.Numeric(14,2), nullable=False),
        sa.Column("balance_after", sa.Numeric(14,2), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("reference_number", sa.String(255)),
        sa.Column("payment_id", sa.BigInteger, sa.ForeignKey("payments.id")),
        sa.Column("transaction_date", sa.Date, nullable=False),
        sa.Column("created_by_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_tt_tenant", "trust_transactions", ["tenant_id"])

    # ── Contacts ──────────────────────────────────────────────────────────────

    op.create_table("contacts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("full_name", sa.String(500), nullable=False),
        sa.Column("company", sa.String(500)),
        sa.Column("contact_type", sa.String(50)),
        sa.Column("email", sa.String(255)),
        sa.Column("phone", sa.String(50)),
        sa.Column("address1", sa.String(255)),
        sa.Column("city", sa.String(100)),
        sa.Column("state", sa.String(50)),
        sa.Column("bar_number", sa.String(100)),
        sa.Column("firm_name", sa.String(500)),
        sa.Column("notes", sa.Text),
        sa.Column("external_id", sa.String(255)),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_con_tenant", "contacts", ["tenant_id"])
    op.create_index("idx_con_tenant_type", "contacts", ["tenant_id","contact_type"])

    op.create_table("matter_contacts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("contact_id", sa.BigInteger, sa.ForeignKey("contacts.id"), nullable=False),
        sa.Column("role", sa.String(100)),
        sa.Column("is_primary", sa.String(1), server_default="N"),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_mc_tenant_matter", "matter_contacts", ["tenant_id","matter_id"])

    op.create_table("matter_timekeepers",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(50)),
        sa.Column("rate_override", sa.Numeric(10,2)),
        sa.Column("assigned_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_mt_tenant_matter", "matter_timekeepers", ["tenant_id","matter_id"])

    # ── Intelligence tables ───────────────────────────────────────────────────

    op.create_table("learned_preferences",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger),
        sa.Column("preference_type", sa.String(100), nullable=False),
        sa.Column("module", sa.String(50), nullable=False),
        sa.Column("rule", postgresql.JSONB, nullable=False),
        sa.Column("confidence", sa.Numeric(3,2), nullable=False),
        sa.Column("sample_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_active", sa.String(1), nullable=False, server_default="Y"),
        sa.Column("last_derived_at", sa.DateTime, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_lp_tenant", "learned_preferences", ["tenant_id"])

    op.create_table("ai_api_calls",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("module", sa.String(50), nullable=False),
        sa.Column("purpose", sa.String(255), nullable=False),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("total_tokens", sa.Integer),
        sa.Column("cost_usd", sa.Numeric(8,6)),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_message", sa.Text),
        sa.Column("request_metadata", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_aac_tenant", "ai_api_calls", ["tenant_id"])
    op.create_index("idx_aac_tenant_module", "ai_api_calls", ["tenant_id","module"])
    op.create_index("idx_aac_created", "ai_api_calls", ["created_at"])

    # ── Dashboard / matter tables ─────────────────────────────────────────────

    op.create_table("conflict_checks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id")),
        sa.Column("checked_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("check_type", sa.String(50), server_default="new_matter"),
        sa.Column("parties_checked", postgresql.JSONB, nullable=False),
        sa.Column("results", postgresql.JSONB, nullable=False),
        sa.Column("overall_result", sa.String(50), nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_cc_tenant_matter", "conflict_checks", ["tenant_id","matter_id"])

    op.create_table("conflict_waivers",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("conflict_check_id", sa.BigInteger, sa.ForeignKey("conflict_checks.id"), nullable=False),
        sa.Column("waived_by", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("documented_reason", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_cw_tenant", "conflict_waivers", ["tenant_id"])

    op.create_table("matter_summaries",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("overview_paragraph", sa.Text),
        sa.Column("critical_issues_paragraph", sa.Text),
        sa.Column("generated_at", sa.DateTime, nullable=False),
        sa.Column("doc_count_at_generation", sa.Integer, server_default="0"),
        sa.Column("job_id", sa.String(255)),
        sa.Column("status", sa.String(50), server_default="pending"),
        sa.Column("error_message", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_ms_tenant_matter", "matter_summaries", ["tenant_id","matter_id"])

    op.create_table("causes_of_action",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("count_number", sa.Integer),
        sa.Column("status", sa.String(50), server_default="not_developed"),
        sa.Column("ai_summary", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_coa_tenant_matter", "causes_of_action", ["tenant_id","matter_id"])

    op.create_table("coa_elements",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("cause_of_action_id", sa.BigInteger, sa.ForeignKey("causes_of_action.id"), nullable=False),
        sa.Column("element_name", sa.String(500), nullable=False),
        sa.Column("status", sa.String(50), server_default="not_developed"),
        sa.Column("supporting_evidence", postgresql.JSONB),
        sa.Column("undermining_evidence", postgresql.JSONB),
        sa.Column("discovery_gaps", postgresql.JSONB),
        sa.Column("pending_motions", postgresql.JSONB),
        sa.Column("attorney_override_status", sa.String(50)),
        sa.Column("attorney_override_note", sa.Text),
        sa.Column("overridden_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("overridden_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_coe_tenant", "coa_elements", ["tenant_id","cause_of_action_id"])

    op.create_table("tasks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id")),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("source", sa.String(50), server_default="manual"),
        sa.Column("source_ref", sa.String(255)),
        sa.Column("priority", sa.String(50), server_default="medium"),
        sa.Column("status", sa.String(50), server_default="open"),
        sa.Column("due_date", sa.DateTime),
        sa.Column("completed_at", sa.DateTime),
        sa.Column("created_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_task_tenant_matter", "tasks", ["tenant_id","matter_id"])
    op.create_index("idx_task_tenant_status", "tasks", ["tenant_id","status"])

    op.create_table("task_assignments",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.BigInteger, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("assigned_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("assigned_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_ta_tenant_task", "task_assignments", ["tenant_id","task_id"])

    op.create_table("communication_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("contact_id", sa.BigInteger, sa.ForeignKey("contacts.id")),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("channel", sa.String(50), nullable=False),
        sa.Column("direction", sa.String(50), nullable=False),
        sa.Column("subject", sa.String(500)),
        sa.Column("summary", sa.Text),
        sa.Column("external_ref", sa.String(500)),
        sa.Column("occurred_at", sa.DateTime, nullable=False),
        sa.Column("logged_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_cl_tenant_matter", "communication_log", ["tenant_id","matter_id"])

    op.create_table("engagement_letters",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("document_id", UUID, sa.ForeignKey("documents.id")),
        sa.Column("status", sa.String(50), server_default="draft"),
        sa.Column("sent_at", sa.DateTime),
        sa.Column("signed_at", sa.DateTime),
        sa.Column("expires_at", sa.DateTime),
        sa.Column("signer_name", sa.String(255)),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_el_tenant_matter", "engagement_letters", ["tenant_id","matter_id"])

    op.create_table("settlements",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("offer_type", sa.String(50), nullable=False),
        sa.Column("amount", sa.Numeric(15,2)),
        sa.Column("offered_by", sa.String(255)),
        sa.Column("terms_summary", sa.Text),
        sa.Column("document_id", UUID, sa.ForeignKey("documents.id")),
        sa.Column("offered_at", sa.DateTime, nullable=False),
        sa.Column("expires_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_set_tenant_matter", "settlements", ["tenant_id","matter_id"])

    op.create_table("expert_witnesses",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("contact_id", sa.BigInteger, sa.ForeignKey("contacts.id")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("specialty", sa.String(500)),
        sa.Column("credentials", sa.Text),
        sa.Column("retained_by", sa.String(50), server_default="us"),
        sa.Column("hourly_rate", sa.Numeric(10,2)),
        sa.Column("report_due", sa.DateTime),
        sa.Column("report_filed", sa.Boolean, server_default="false"),
        sa.Column("deposition_date", sa.DateTime),
        sa.Column("status", sa.String(50), server_default="identified"),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_ew_tenant_matter", "expert_witnesses", ["tenant_id","matter_id"])

    op.create_table("mediations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("mediator_name", sa.String(255)),
        sa.Column("mediator_contact_id", sa.BigInteger, sa.ForeignKey("contacts.id")),
        sa.Column("scheduled_date", sa.DateTime),
        sa.Column("location", sa.String(500)),
        sa.Column("brief_document_id", UUID, sa.ForeignKey("documents.id")),
        sa.Column("outcome", sa.String(50), server_default="pending"),
        sa.Column("settlement_amount", sa.Numeric(15,2)),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_med_tenant_matter", "mediations", ["tenant_id","matter_id"])

    op.create_table("intake_sessions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("initiated_by", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(50), server_default="uploading"),
        sa.Column("extracted_fields", postgresql.JSONB),
        sa.Column("ocr_results", postgresql.JSONB),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id")),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_is_tenant_status", "intake_sessions", ["tenant_id","status"])

    op.create_table("critical_date_memos",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("source_document_id", UUID, sa.ForeignKey("documents.id")),
        sa.Column("memo_document_id", UUID, sa.ForeignKey("documents.id")),
        sa.Column("dates_extracted", postgresql.JSONB),
        sa.Column("status", sa.String(50), server_default="pending"),
        sa.Column("generated_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_cdm_tenant_matter", "critical_date_memos", ["tenant_id","matter_id"])

    op.create_table("title_date_comparisons",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("memo_id", sa.BigInteger, sa.ForeignKey("critical_date_memos.id"), nullable=False),
        sa.Column("title_document_id", UUID, sa.ForeignKey("documents.id")),
        sa.Column("comparison_rows", postgresql.JSONB),
        sa.Column("status", sa.String(50), server_default="pending"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_tdc_tenant_matter", "title_date_comparisons", ["tenant_id","matter_id"])

    op.create_table("document_time_tracking",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("document_id", UUID, sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id")),
        sa.Column("session_start", sa.DateTime, nullable=False),
        sa.Column("session_end", sa.DateTime),
        sa.Column("duration_seconds", sa.Integer),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_dtt_tenant_doc", "document_time_tracking", ["tenant_id","document_id"])

    # ── eDiscovery tables ─────────────────────────────────────────────────────

    op.create_table("productions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("collection_id", UUID, sa.ForeignKey("ediscovery_collections.id"), nullable=False),
        sa.Column("production_name", sa.String(255), nullable=False),
        sa.Column("produced_to", sa.String(500), nullable=False),
        sa.Column("production_date", sa.DateTime),
        sa.Column("format", sa.String(50), nullable=False, server_default="relativity_dat"),
        sa.Column("bates_prefix", sa.String(20), nullable=False),
        sa.Column("bates_start", sa.BigInteger, nullable=False),
        sa.Column("bates_end", sa.BigInteger),
        sa.Column("total_documents", sa.BigInteger, server_default="0"),
        sa.Column("total_pages", sa.BigInteger, server_default="0"),
        sa.Column("output_path", sa.String(2000)),
        sa.Column("output_hash", sa.String(64)),
        sa.Column("status", sa.String(50), nullable=False, server_default="preparing"),
        sa.Column("dat_file_path", sa.String(2000)),
        sa.Column("opt_file_path", sa.String(2000)),
        sa.Column("notes", sa.Text),
        sa.Column("metadata", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.BigInteger, sa.ForeignKey("users.id")),
    )
    op.create_index("idx_prod_tenant_collection", "productions", ["tenant_id","collection_id"])

    op.create_table("legal_holds",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("hold_name", sa.String(255), nullable=False),
        sa.Column("hold_scope", sa.Text, nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="active"),
        sa.Column("issued_date", sa.Date, nullable=False),
        sa.Column("issued_by", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("modified_date", sa.Date),
        sa.Column("modified_reason", sa.Text),
        sa.Column("released_date", sa.Date),
        sa.Column("released_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("released_reason", sa.Text),
        sa.Column("notice_document_path", sa.String(2000)),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_lh_tenant_matter", "legal_holds", ["tenant_id","matter_id"])

    op.create_table("hold_custodians",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("hold_id", sa.BigInteger, sa.ForeignKey("legal_holds.id"), nullable=False),
        sa.Column("custodian_name", sa.String(255), nullable=False),
        sa.Column("custodian_email", sa.String(255)),
        sa.Column("custodian_role", sa.String(255)),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("notified_at", sa.DateTime),
        sa.Column("acknowledged_at", sa.DateTime),
        sa.Column("last_reminder_at", sa.DateTime),
        sa.Column("reminder_count", sa.BigInteger, server_default="0"),
        sa.Column("escalated_at", sa.DateTime),
        sa.Column("escalated_to", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_hc_tenant_hold", "hold_custodians", ["tenant_id","hold_id"])

    op.create_table("privilege_log_entries",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("collection_id", UUID, sa.ForeignKey("ediscovery_collections.id"), nullable=False),
        sa.Column("document_id", UUID, sa.ForeignKey("ediscovery_documents.id"), nullable=False),
        sa.Column("log_number", sa.BigInteger),
        sa.Column("doc_date", sa.Date),
        sa.Column("author", sa.String(500)),
        sa.Column("recipients", sa.Text),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("privilege_basis", sa.String(50), nullable=False),
        sa.Column("privilege_detail", sa.Text),
        sa.Column("bates_begin", sa.String(50)),
        sa.Column("bates_end", sa.String(50)),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.BigInteger, sa.ForeignKey("users.id")),
    )
    op.create_index("idx_ple_tenant_collection", "privilege_log_entries", ["tenant_id","collection_id"])

    op.create_table("search_term_sets",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("collection_id", UUID, sa.ForeignKey("ediscovery_collections.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("version_label", sa.String(255)),
        sa.Column("author", sa.String(255)),
        sa.Column("notes", sa.Text),
        sa.Column("format_o365_kql", sa.Text),
        sa.Column("format_gmail_vault", sa.Text),
        sa.Column("format_relativity", sa.Text),
        sa.Column("format_generic_boolean", sa.Text),
        sa.Column("hit_count_results", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.BigInteger, sa.ForeignKey("users.id")),
    )
    op.create_index("idx_sts_tenant_collection", "search_term_sets", ["tenant_id","collection_id"])

    op.create_table("search_terms",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("term_set_id", sa.BigInteger, sa.ForeignKey("search_term_sets.id"), nullable=False),
        sa.Column("tier", sa.String(50), nullable=False),
        sa.Column("issue_element", sa.String(500)),
        sa.Column("term_text", sa.Text, nullable=False),
        sa.Column("hit_count", sa.BigInteger),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_st_tenant_termset", "search_terms", ["tenant_id","term_set_id"])

    op.create_table("esi_protocols",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("collection_id", UUID, sa.ForeignKey("ediscovery_collections.id")),
        sa.Column("version", sa.BigInteger, nullable=False, server_default="1"),
        sa.Column("version_label", sa.String(255)),
        sa.Column("protocol_data", postgresql.JSONB),
        sa.Column("document_path", sa.String(2000)),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.BigInteger, sa.ForeignKey("users.id")),
    )
    op.create_index("idx_esi_tenant_matter", "esi_protocols", ["tenant_id","matter_id"])

    # ── Deadlines ─────────────────────────────────────────────────────────────

    op.create_table("deadlines",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("deadline_date", sa.DateTime, nullable=False),
        sa.Column("deadline_type", sa.String(100)),
        sa.Column("source", sa.String(100)),
        sa.Column("notes", sa.Text),
        sa.Column("is_sol", sa.Boolean, server_default="false"),
        sa.Column("completed_at", sa.DateTime),
        sa.Column("created_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_dead_tenant_matter", "deadlines", ["tenant_id","matter_id"])
    op.create_index("idx_dead_tenant_date",   "deadlines", ["tenant_id","deadline_date"])

    # ── Viaticum stubs ────────────────────────────────────────────────────────

    op.create_table("viaticum_sessions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id")),
        sa.Column("session_name", sa.String(255), nullable=False),
        sa.Column("session_type", sa.String(50), nullable=False, server_default="deposition"),
        sa.Column("status", sa.String(50), server_default="active"),
        sa.Column("device_id", sa.String(255)),
        sa.Column("started_at", sa.DateTime),
        sa.Column("closed_at", sa.DateTime),
        sa.Column("created_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_vs_tenant_matter", "viaticum_sessions", ["tenant_id","matter_id"])

    op.create_table("viaticum_exhibits",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.BigInteger, sa.ForeignKey("viaticum_sessions.id"), nullable=False),
        sa.Column("document_id", UUID, sa.ForeignKey("documents.id")),
        sa.Column("exhibit_number", sa.String(50)),
        sa.Column("exhibit_label", sa.String(255)),
        sa.Column("status", sa.String(50), server_default="staged"),
        sa.Column("presented_at", sa.DateTime),
        sa.Column("notes", sa.Text),
        sa.Column("sort_order", sa.Integer, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_ve_tenant_session", "viaticum_exhibits", ["tenant_id","session_id"])


def downgrade():
    tables = [
        "viaticum_exhibits", "viaticum_sessions",
        "deadlines", "esi_protocols", "search_terms", "search_term_sets",
        "privilege_log_entries", "hold_custodians", "legal_holds", "productions",
        "document_time_tracking", "title_date_comparisons", "critical_date_memos",
        "intake_sessions", "mediations", "expert_witnesses", "settlements",
        "engagement_letters", "communication_log", "task_assignments", "tasks",
        "coa_elements", "causes_of_action", "matter_summaries",
        "conflict_waivers", "conflict_checks",
        "ai_api_calls", "learned_preferences",
        "matter_timekeepers", "matter_contacts", "contacts",
        "trust_transactions", "trust_ledger",
        "qbo_mappings", "qbo_sync_log",
        "billing_qc_results", "report_definitions",
        "time_entry_sources", "invoice_line_items",
        "rate_change_log", "rate_cards",
        "distributions", "payments", "invoice_matters",
    ]
    for t in tables:
        op.drop_table(t)
