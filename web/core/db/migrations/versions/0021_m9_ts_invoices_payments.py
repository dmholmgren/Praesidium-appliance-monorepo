"""
0021_m9_ts_invoices_payments.py
Alembic migration — M9 billing import: rebuild ts_slips, add ts_invoices/ts_payments.

Drops and recreates ts_slips with the correct Firebird-sourced schema.
The 53,499 CSV seed rows are stale placeholder data — cleared here.
Live data comes from ts_sync_agent.py v2.0 via the Firebird direct connection.

Also adds ts_invoices, ts_payments, and adds missing columns to
ts_clients and ts_timekeepers.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0021_m9_ts_invoices_payments"
down_revision = "0020_page_registry"
branch_labels = None
depends_on = None


def upgrade():

    # ── Drop and rebuild ts_slips ─────────────────────────────────────────────
    op.drop_table("ts_slips")

    op.create_table(
        "ts_slips",
        sa.Column("id",              sa.Text(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("tenant_id",       sa.String(36), nullable=False),
        sa.Column("source_id",       sa.Text(), nullable=False),
        sa.Column("source_slip_id",  sa.Text(), nullable=False),
        sa.Column("content_hash",    sa.Text(), nullable=False),
        sa.Column("trans_type",      sa.Integer()),
        sa.Column("slip_date",       sa.Date()),
        sa.Column("end_date",        sa.Date()),
        sa.Column("source_client_id",sa.Text()),
        sa.Column("source_tk_id",    sa.Text()),
        sa.Column("activity_id",     sa.Text()),
        sa.Column("reference_id",    sa.Text()),
        sa.Column("hours",           sa.Numeric(10, 4), server_default="0"),
        sa.Column("hours_estimated", sa.Numeric(10, 4), server_default="0"),
        sa.Column("quantity",        sa.Numeric(10, 4), server_default="0"),
        sa.Column("price",           sa.Numeric(14, 2), server_default="0"),
        sa.Column("rate",            sa.Numeric(14, 2), server_default="0"),
        sa.Column("rate_type",       sa.Text()),
        sa.Column("value",           sa.Numeric(14, 2), server_default="0"),
        sa.Column("billed_value",    sa.Numeric(14, 2), server_default="0"),
        sa.Column("wip_value",       sa.Numeric(14, 2), server_default="0"),
        sa.Column("billed",          sa.Boolean(), server_default=sa.false()),
        sa.Column("bill_status",     sa.Integer()),
        sa.Column("on_hold",         sa.Boolean(), server_default=sa.false()),
        sa.Column("invoice_id",      sa.Text()),
        sa.Column("invoice_num",     sa.Integer()),
        sa.Column("post_period",     sa.Text()),
        sa.Column("orig_slip_id",    sa.Text()),
        sa.Column("narrative",       sa.Text()),
        sa.Column("raw_data",        JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("imported_at",     sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
    )
    op.create_unique_constraint(
        "uq_ts_slips_dedup",
        "ts_slips",
        ["tenant_id", "source_id", "content_hash"]
    )
    op.create_index("ix_ts_slips_tenant",   "ts_slips", ["tenant_id"])
    op.create_index("ix_ts_slips_client",   "ts_slips", ["tenant_id", "source_client_id"])
    op.create_index("ix_ts_slips_tk",       "ts_slips", ["tenant_id", "source_tk_id"])
    op.create_index("ix_ts_slips_date",     "ts_slips", ["tenant_id", "slip_date"])
    op.create_index("ix_ts_slips_billed",   "ts_slips", ["tenant_id", "billed"])
    op.create_index("ix_ts_slips_invoice",  "ts_slips", ["tenant_id", "invoice_num"])

    # ── ts_invoices ───────────────────────────────────────────────────────────
    op.create_table(
        "ts_invoices",
        sa.Column("id",               sa.Text(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("tenant_id",        sa.String(36), nullable=False),
        sa.Column("source_id",        sa.Text(), nullable=False),
        sa.Column("source_invoice_id",sa.Text(), nullable=False),
        sa.Column("invoice_num",      sa.Integer()),
        sa.Column("charge_fees",      sa.Numeric(14, 2), server_default="0"),
        sa.Column("charge_costs",     sa.Numeric(14, 2), server_default="0"),
        sa.Column("net_due",          sa.Numeric(14, 2), server_default="0"),
        sa.Column("paid_in_full",     sa.Boolean(), server_default=sa.false()),
        sa.Column("invoice_status",   sa.Integer()),
        sa.Column("slip_start",       sa.Date()),
        sa.Column("slip_end",         sa.Date()),
        sa.Column("raw_data",         JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at",       sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
        sa.Column("updated_at",       sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
    )
    op.create_unique_constraint(
        "uq_ts_invoices_tenant_src_id",
        "ts_invoices",
        ["tenant_id", "source_id", "source_invoice_id"]
    )
    op.create_index("ix_ts_invoices_tenant",      "ts_invoices", ["tenant_id"])
    op.create_index("ix_ts_invoices_invoice_num", "ts_invoices", ["tenant_id", "invoice_num"])

    # ── ts_payments ───────────────────────────────────────────────────────────
    op.create_table(
        "ts_payments",
        sa.Column("id",                sa.Text(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("tenant_id",         sa.String(36), nullable=False),
        sa.Column("source_id",         sa.Text(), nullable=False),
        sa.Column("source_payment_id", sa.Text(), nullable=False),
        sa.Column("date_entered",      sa.Date()),
        sa.Column("source_client_id",  sa.Text()),
        sa.Column("amount",            sa.Numeric(14, 2), server_default="0"),
        sa.Column("description",       sa.Text()),
        sa.Column("invoice_num",       sa.Integer()),
        sa.Column("source_invoice_id", sa.Text()),
        sa.Column("post_period",       sa.Text()),
        sa.Column("raw_data",          JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at",        sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
    )
    op.create_unique_constraint(
        "uq_ts_payments_tenant_src_id",
        "ts_payments",
        ["tenant_id", "source_id", "source_payment_id"]
    )
    op.create_index("ix_ts_payments_tenant", "ts_payments", ["tenant_id"])
    op.create_index("ix_ts_payments_client", "ts_payments", ["tenant_id", "source_client_id"])

    # ── ts_clients — add missing columns ─────────────────────────────────────
    for col, col_type in [
        ("client_code",     "TEXT"),
        ("matter_code",     "TEXT"),
        ("address1",        "TEXT"),
        ("address2",        "TEXT"),
        ("city",            "TEXT"),
        ("state",           "TEXT"),
        ("zip",             "TEXT"),
        ("phone1",          "TEXT"),
        ("email",           "TEXT"),
        ("notes",           "TEXT"),
        ("case_type",       "TEXT"),
        ("client_status",   "TEXT"),
        ("sup_attorney",    "TEXT"),
        ("billing_atty",    "TEXT"),
        ("opened",          "TEXT"),
        ("closed",          "TEXT"),
        ("referred_by",     "TEXT"),
        ("opp_counsel",     "TEXT"),
        ("paralegal",       "TEXT"),
        ("associate",       "TEXT"),
        ("est_billings",    "NUMERIC(14,2) DEFAULT 0"),
        ("master_client_id","TEXT"),
        ("raw_data",        "JSONB DEFAULT '{}'::jsonb"),
        ("updated_at",      "TIMESTAMP WITH TIME ZONE DEFAULT now()"),
    ]:
        op.execute(f"ALTER TABLE ts_clients ADD COLUMN IF NOT EXISTS {col} {col_type}")

    # ── ts_timekeepers — add missing columns ──────────────────────────────────
    for col, col_type in [
        ("initials",    "TEXT"),
        ("email",       "TEXT"),
        ("title_level", "TEXT"),
        ("raw_data",    "JSONB DEFAULT '{}'::jsonb"),
        ("updated_at",  "TIMESTAMP WITH TIME ZONE DEFAULT now()"),
    ]:
        op.execute(f"ALTER TABLE ts_timekeepers ADD COLUMN IF NOT EXISTS {col} {col_type}")


def downgrade():
    op.drop_table("ts_invoices")
    op.drop_table("ts_payments")
    # Note: downgrade does not restore the old ts_slips CSV schema
