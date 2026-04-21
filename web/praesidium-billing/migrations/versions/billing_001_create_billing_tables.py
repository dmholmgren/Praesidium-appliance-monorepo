"""Create billing module tables
Revision ID: billing_001
Revises: 0004_crawl_exclusion_rules
Create Date: 2026-03-27
All PKs: BigInteger autoincrement. All FKs: BigInteger. tenant_id: String(36).
"""
from alembic import op
import sqlalchemy as sa

revision = "billing_001"
down_revision = "0004_crawl_exclusion_rules"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("rate_cards",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("scope", sa.Enum("firm","timekeeper","client","matter", name="rate_scope_enum"), nullable=False),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("client_id", sa.BigInteger, sa.ForeignKey("clients.id"), nullable=True),
        sa.Column("matter_id", sa.BigInteger, sa.ForeignKey("matters.id"), nullable=True),
        sa.Column("hourly_rate", sa.Numeric(10,2), nullable=False),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()))
    op.create_index("ix_rate_cards_tenant", "rate_cards", ["tenant_id"])

    op.create_table("rate_change_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("rate_card_id", sa.BigInteger, sa.ForeignKey("rate_cards.id"), nullable=False),
        sa.Column("old_rate", sa.Numeric(10,2), nullable=False),
        sa.Column("new_rate", sa.Numeric(10,2), nullable=False),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column("scope", sa.String(50), nullable=False),
        sa.Column("changed_by_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("apply_to_unbilled_wip", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()))
    op.create_index("ix_rate_change_log_tenant", "rate_change_log", ["tenant_id"])

    op.create_table("time_entry_sources",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("time_entry_id", sa.BigInteger, sa.ForeignKey("time_entries.id"), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("source_data", sa.Text, nullable=True),
        sa.Column("captured_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("duration_seconds", sa.Integer, nullable=True),
        sa.Column("matter_confidence", sa.Numeric(5,4), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()))
    op.create_index("ix_time_entry_sources_tenant", "time_entry_sources", ["tenant_id"])
    op.create_index("ix_time_entry_sources_entry", "time_entry_sources", ["time_entry_id"])

    op.create_table("invoice_line_items",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("invoice_id", sa.BigInteger, sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("time_entry_id", sa.BigInteger, sa.ForeignKey("time_entries.id"), nullable=True),
        sa.Column("matter_id", sa.BigInteger, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("line_date", sa.Date, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("timekeeper_name", sa.String(255), nullable=True),
        sa.Column("hours", sa.Numeric(6,2), nullable=True),
        sa.Column("rate", sa.Numeric(10,2), nullable=True),
        sa.Column("amount", sa.Numeric(12,2), nullable=False),
        sa.Column("utbms_task_code", sa.String(20), nullable=True),
        sa.Column("line_type", sa.Enum("time","expense","flat_fee","retainer_draw","adjustment", name="line_type_enum"), nullable=False, server_default="time"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()))
    op.create_index("ix_invoice_line_items_tenant", "invoice_line_items", ["tenant_id"])

    op.create_table("trust_ledger",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("client_id", sa.BigInteger, sa.ForeignKey("clients.id"), nullable=False, unique=True),
        sa.Column("balance", sa.Numeric(14,2), nullable=False, server_default="0"),
        sa.Column("last_reconciled_at", sa.DateTime, nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()))
    op.create_index("ix_trust_ledger_tenant", "trust_ledger", ["tenant_id"])

    op.create_table("trust_transactions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("trust_ledger_id", sa.BigInteger, sa.ForeignKey("trust_ledger.id"), nullable=False),
        sa.Column("client_id", sa.BigInteger, sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("matter_id", sa.BigInteger, sa.ForeignKey("matters.id"), nullable=True),
        sa.Column("transaction_type", sa.Enum("deposit","disbursement","interest","adjustment", name="trust_txn_type_enum"), nullable=False),
        sa.Column("amount", sa.Numeric(14,2), nullable=False),
        sa.Column("balance_after", sa.Numeric(14,2), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("reference_number", sa.String(255), nullable=True),
        sa.Column("payment_id", sa.BigInteger, sa.ForeignKey("payments.id"), nullable=True),
        sa.Column("transaction_date", sa.Date, nullable=False),
        sa.Column("created_by_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()))
    op.create_index("ix_trust_transactions_tenant", "trust_transactions", ["tenant_id"])

    op.create_table("billing_qc_results",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("time_entry_id", sa.BigInteger, sa.ForeignKey("time_entries.id"), nullable=False),
        sa.Column("qc_run_id", sa.String(36), nullable=False),
        sa.Column("check_type", sa.String(50), nullable=False),
        sa.Column("severity", sa.Enum("info","warning","error", name="qc_severity_enum"), nullable=False, server_default="warning"),
        sa.Column("confidence", sa.Numeric(5,4), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("suggested_fix", sa.Text, nullable=True),
        sa.Column("is_resolved", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("resolved_by_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("resolved_at", sa.DateTime, nullable=True),
        sa.Column("resolution_action", sa.String(20), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()))
    op.create_index("ix_billing_qc_results_tenant", "billing_qc_results", ["tenant_id"])

    op.create_table("qbo_sync_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("object_type", sa.String(50), nullable=False),
        sa.Column("object_id", sa.BigInteger, nullable=False),
        sa.Column("direction", sa.Enum("push","pull", name="sync_direction_enum"), nullable=False),
        sa.Column("qbo_id", sa.String(100), nullable=True),
        sa.Column("status", sa.Enum("pending","success","error","retrying", name="sync_status_enum"), nullable=False, server_default="pending"),
        sa.Column("request_payload", sa.Text, nullable=True),
        sa.Column("response_payload", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()))
    op.create_index("ix_qbo_sync_log_tenant", "qbo_sync_log", ["tenant_id"])

    op.create_table("qbo_mappings",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("mapping_type", sa.String(50), nullable=False),
        sa.Column("platform_code", sa.String(100), nullable=False),
        sa.Column("platform_label", sa.String(255), nullable=True),
        sa.Column("qbo_id", sa.String(100), nullable=False),
        sa.Column("qbo_name", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()))
    op.create_index("ix_qbo_mappings_tenant", "qbo_mappings", ["tenant_id"])

    op.create_table("report_definitions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("sql_template", sa.Text, nullable=False),
        sa.Column("default_filters", sa.Text, nullable=True),
        sa.Column("column_definitions", sa.Text, nullable=True),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_system", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()))
    op.create_index("ix_report_definitions_tenant", "report_definitions", ["tenant_id"])
    op.create_index("ix_report_definitions_slug", "report_definitions", ["slug"])

def downgrade():
    for t in ["report_definitions","qbo_mappings","qbo_sync_log","billing_qc_results","trust_transactions","trust_ledger","invoice_line_items","time_entry_sources","rate_change_log","rate_cards"]:
        op.drop_table(t)
