"""0003 bill run engine

Revision ID: 0003_bill_run_engine
Revises: 0002_permissions_phase1
Create Date: 2026-05-01

Adds bill_runs, bill_run_matters, prebill_adjustments,
invoice_delivery_log tables.
Adds bill_run_id, billing_attorney_id, writeoff_total to invoices.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0003_bill_run_engine"
down_revision = "0002_permissions_phase1"
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. bill_runs — tracks batch billing operations ────────────────────
    op.create_table(
        "bill_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("run_name", sa.String(200), nullable=False),
        sa.Column("billing_period_start", sa.Date(), nullable=False),
        sa.Column("billing_period_end", sa.Date(), nullable=False),
        sa.Column(
            "status", sa.String(50),
            server_default="draft", nullable=False,
        ),
        # draft → reviewing → finalized → delivered
        sa.Column("total_matters", sa.Integer(), server_default="0"),
        sa.Column("total_fees", sa.Numeric(), server_default="0"),
        sa.Column("total_costs", sa.Numeric(), server_default="0"),
        sa.Column("total_writeoffs", sa.Numeric(), server_default="0"),
        sa.Column("total_invoiced", sa.Numeric(), server_default="0"),
        sa.Column("created_by_id", sa.BigInteger(), nullable=False),
        sa.Column("finalized_by_id", sa.BigInteger(), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index("ix_bill_runs_tenant", "bill_runs", ["tenant_id", "status"])

    # ── 2. bill_run_matters — matters included in a bill run ──────────────
    op.create_table(
        "bill_run_matters",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("bill_run_id", sa.BigInteger(), nullable=False),
        sa.Column("matter_id", UUID(as_uuid=True), nullable=True),
        # ts_client_id: for Timeslips-sourced matters (no canonical matter row)
        sa.Column("ts_client_id", sa.String(100), nullable=True),
        sa.Column("client_id", UUID(as_uuid=True), nullable=True),
        sa.Column("client_name", sa.String(500), nullable=True),
        sa.Column("matter_name", sa.String(500), nullable=True),
        sa.Column("matter_number", sa.String(100), nullable=True),
        sa.Column(
            "status", sa.String(50),
            server_default="pending", nullable=False,
        ),
        # pending → approved → invoiced | skipped
        sa.Column("fee_total", sa.Numeric(), server_default="0"),
        sa.Column("cost_total", sa.Numeric(), server_default="0"),
        sa.Column("writeoff_total", sa.Numeric(), server_default="0"),
        sa.Column("net_total", sa.Numeric(), server_default="0"),
        sa.Column("prior_balance", sa.Numeric(), server_default="0"),
        sa.Column("invoice_id", UUID(as_uuid=True), nullable=True),
        sa.Column("invoice_number", sa.String(100), nullable=True),
        sa.Column("reviewer_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_by_id", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index(
        "ix_bill_run_matters_lookup",
        "bill_run_matters",
        ["tenant_id", "bill_run_id"],
    )

    # ── 3. prebill_adjustments — edits/write-offs during prebill review ───
    op.create_table(
        "prebill_adjustments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("bill_run_matter_id", sa.BigInteger(), nullable=False),
        sa.Column("time_entry_id", UUID(as_uuid=True), nullable=True),
        sa.Column("ts_slip_id", sa.String(100), nullable=True),
        sa.Column("adjustment_type", sa.String(50), nullable=False),
        # write_off, hours_edit, rate_edit, narrative_edit, add_charge, remove
        sa.Column("original_value", sa.Numeric(), nullable=True),
        sa.Column("adjusted_value", sa.Numeric(), nullable=True),
        sa.Column("original_hours", sa.Numeric(), nullable=True),
        sa.Column("adjusted_hours", sa.Numeric(), nullable=True),
        sa.Column("original_narrative", sa.Text(), nullable=True),
        sa.Column("adjusted_narrative", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("adjusted_by_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index(
        "ix_prebill_adjustments_matter",
        "prebill_adjustments",
        ["tenant_id", "bill_run_matter_id"],
    )

    # ── 4. invoice_delivery_log — tracks email/portal delivery ────────────
    op.create_table(
        "invoice_delivery_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("invoice_id", UUID(as_uuid=True), nullable=False),
        sa.Column("delivery_method", sa.String(50), nullable=False),
        # email, portal, print, ledes
        sa.Column("recipient_email", sa.String(500), nullable=True),
        sa.Column("recipient_name", sa.String(500), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("pdf_path", sa.String(1000), nullable=True),
        sa.Column(
            "status", sa.String(50),
            server_default="sent", nullable=False,
        ),
        # sent, delivered, bounced, opened, failed
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("sent_by_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("metadata", JSONB(), nullable=True),
    )
    op.create_index(
        "ix_invoice_delivery_log_invoice",
        "invoice_delivery_log",
        ["tenant_id", "invoice_id"],
    )

    # ── 5. Add bill_run columns to invoices table ─────────────────────────
    op.add_column(
        "invoices",
        sa.Column("bill_run_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "invoices",
        sa.Column("billing_attorney_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "invoices",
        sa.Column("writeoff_total", sa.Numeric(), server_default="0", nullable=True),
    )


def downgrade():
    op.drop_column("invoices", "writeoff_total")
    op.drop_column("invoices", "billing_attorney_id")
    op.drop_column("invoices", "bill_run_id")
    op.drop_table("invoice_delivery_log")
    op.drop_table("prebill_adjustments")
    op.drop_table("bill_run_matters")
    op.drop_table("bill_runs")
