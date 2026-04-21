"""
0022_billing_module.py
Alembic migration — M9 Billing Module

1. Creates billing_matters — client/matter pairs derived from ts_clients NICKNAME1
   (split on / or \\ — left=client_code, right=matter_code, no separator=single-matter client)
2. Creates billing_slips — native Praesidium slip entry (alongside Timeslips sync)
3. Creates billing_invoices — generated invoices
4. Creates billing_payments — payment records with client-level allocation
5. Creates billing_payment_allocations — per-matter allocation of a client payment
6. Seeds ui_nav_items for billing module

Routes are data — all billing nav routes seeded here.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision      = "0022_billing_module"
down_revision = "0021_m9_ts_invoices_payments"
branch_labels = None
depends_on    = None


def upgrade():

    # ── billing_matters ────────────────────────────────────────────────────────
    # Derived client/matter hierarchy from ts_clients NICKNAME1 split.
    # Populated by the client/matter split service on first run.
    op.create_table(
        "billing_matters",
        sa.Column("id",             sa.Text(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("tenant_id",      sa.String(36), nullable=False),
        sa.Column("ts_client_id",   sa.Text()),           # FK to ts_clients.ts_client_id
        sa.Column("client_code",    sa.Text(), nullable=False),  # left of /
        sa.Column("matter_code",    sa.Text()),                  # right of /, NULL=single-matter
        sa.Column("display_name",   sa.Text()),           # full "Client / Matter" display
        sa.Column("nickname2",      sa.Text()),           # NICKNAME2 e.g. "901234.001"
        sa.Column("attorney_prefix",sa.String(4)),        # "90"=Dennis, "70"=Mitchell
        sa.Column("status",         sa.String(32), server_default="'active'"),
        sa.Column("opened",         sa.Text()),
        sa.Column("closed",         sa.Text()),
        sa.Column("case_type",      sa.Text()),
        sa.Column("sup_attorney",   sa.Text()),
        sa.Column("billing_atty",   sa.Text()),
        sa.Column("raw_data",       JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at",     sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
        sa.Column("updated_at",     sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
    )
    op.create_unique_constraint(
        "uq_billing_matters_client_matter",
        "billing_matters",
        ["tenant_id", "client_code", "matter_code"]
    )
    op.create_index("ix_billing_matters_tenant",   "billing_matters", ["tenant_id"])
    op.create_index("ix_billing_matters_client",   "billing_matters", ["tenant_id", "client_code"])
    op.create_index("ix_billing_matters_ts_client","billing_matters", ["tenant_id", "ts_client_id"])
    op.create_index("ix_billing_matters_attorney", "billing_matters", ["tenant_id", "attorney_prefix"])

    # ── billing_slips ──────────────────────────────────────────────────────────
    op.create_table(
        "billing_slips",
        sa.Column("id",           sa.Text(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("tenant_id",    sa.String(36), nullable=False),
        sa.Column("matter_id",    sa.Text()),              # FK to billing_matters.id
        sa.Column("ts_slip_id",   sa.Text()),              # if sourced from Timeslips
        sa.Column("slip_date",    sa.Date(), nullable=False),
        sa.Column("timekeeper",   sa.Text()),              # initials or tk_id
        sa.Column("hours",        sa.Numeric(10,2), server_default="0"),
        sa.Column("rate",         sa.Numeric(14,2), server_default="0"),
        sa.Column("value",        sa.Numeric(14,2), server_default="0"),
        sa.Column("narrative",    sa.Text()),
        sa.Column("billed",       sa.Boolean(), server_default=sa.false()),
        sa.Column("invoice_id",   sa.Text()),
        sa.Column("source",       sa.String(32), server_default="'manual'"),
        sa.Column("created_by",   sa.Text()),
        sa.Column("created_at",   sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
        sa.Column("updated_at",   sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_billing_slips_tenant",  "billing_slips", ["tenant_id"])
    op.create_index("ix_billing_slips_matter",  "billing_slips", ["tenant_id", "matter_id"])
    op.create_index("ix_billing_slips_billed",  "billing_slips", ["tenant_id", "billed"])
    op.create_index("ix_billing_slips_date",    "billing_slips", ["tenant_id", "slip_date"])

    # ── billing_invoices ───────────────────────────────────────────────────────
    op.create_table(
        "billing_invoices",
        sa.Column("id",              sa.Text(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("tenant_id",       sa.String(36), nullable=False),
        sa.Column("matter_id",       sa.Text()),
        sa.Column("invoice_number",  sa.Text()),
        sa.Column("invoice_date",    sa.Date()),
        sa.Column("due_date",        sa.Date()),
        sa.Column("fee_total",       sa.Numeric(14,2), server_default="0"),
        sa.Column("cost_total",      sa.Numeric(14,2), server_default="0"),
        sa.Column("tax_total",       sa.Numeric(14,2), server_default="0"),
        sa.Column("total_due",       sa.Numeric(14,2), server_default="0"),
        sa.Column("amount_paid",     sa.Numeric(14,2), server_default="0"),
        sa.Column("balance_due",     sa.Numeric(14,2), server_default="0"),
        sa.Column("status",          sa.String(32), server_default="'draft'"),
        sa.Column("notes",           sa.Text()),
        sa.Column("ts_invoice_id",   sa.Text()),
        sa.Column("created_at",      sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
        sa.Column("updated_at",      sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_billing_invoices_tenant", "billing_invoices", ["tenant_id"])
    op.create_index("ix_billing_invoices_matter", "billing_invoices", ["tenant_id", "matter_id"])
    op.create_index("ix_billing_invoices_status", "billing_invoices", ["tenant_id", "status"])

    # ── billing_payments ───────────────────────────────────────────────────────
    op.create_table(
        "billing_payments",
        sa.Column("id",              sa.Text(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("tenant_id",       sa.String(36), nullable=False),
        sa.Column("client_code",     sa.Text(), nullable=False),
        sa.Column("payment_date",    sa.Date(), nullable=False),
        sa.Column("amount",          sa.Numeric(14,2), nullable=False),
        sa.Column("allocated_total", sa.Numeric(14,2), server_default="0"),
        sa.Column("unallocated",     sa.Numeric(14,2), server_default="0"),
        sa.Column("payment_method",  sa.String(32)),
        sa.Column("reference",       sa.Text()),
        sa.Column("notes",           sa.Text()),
        sa.Column("ts_payment_id",   sa.Text()),
        sa.Column("created_at",      sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_billing_payments_tenant", "billing_payments", ["tenant_id"])
    op.create_index("ix_billing_payments_client", "billing_payments", ["tenant_id", "client_code"])

    # ── billing_payment_allocations ───────────────────────────────────────────
    op.create_table(
        "billing_payment_allocations",
        sa.Column("id",           sa.Text(), primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("tenant_id",    sa.String(36), nullable=False),
        sa.Column("payment_id",   sa.Text(), nullable=False),
        sa.Column("invoice_id",   sa.Text()),
        sa.Column("matter_id",    sa.Text()),
        sa.Column("amount",       sa.Numeric(14,2), nullable=False),
        sa.Column("notes",        sa.Text()),
        sa.Column("created_at",   sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_billing_alloc_payment", "billing_payment_allocations",
                    ["tenant_id", "payment_id"])
    op.create_index("ix_billing_alloc_invoice", "billing_payment_allocations",
                    ["tenant_id", "invoice_id"])

    # ── ui_nav_items — seed billing routes ────────────────────────────────────
    op.execute("""
        INSERT INTO ui_nav_items
            (id, nav_key, label, url_path, icon,
             parent_key, display_order, feature_flag, is_active)
        VALUES
            (gen_random_uuid(), 'billing',
             'Billing', '/billing/matters', 'currency-dollar',
             NULL, 40, 'feature_billing', true),
            (gen_random_uuid(), 'billing_matters',
             'Matters', '/billing/matters', 'folder',
             'billing', 41, 'feature_billing', true),
            (gen_random_uuid(), 'billing_slips',
             'Time Entry', '/billing/slips/new', 'clock',
             'billing', 42, 'feature_billing', true),
            (gen_random_uuid(), 'billing_prebills',
             'Prebills', '/billing/prebills', 'document-text',
             'billing', 43, 'feature_billing', true),
            (gen_random_uuid(), 'billing_invoices',
             'Invoices', '/billing/invoices', 'receipt-refund',
             'billing', 44, 'feature_billing', true),
            (gen_random_uuid(), 'billing_payments',
             'Payments', '/billing/payments', 'banknotes',
             'billing', 45, 'feature_billing', true)
        ON CONFLICT (nav_key) DO NOTHING
    """)

    # ── Enable billing feature flag for hjmm-prod ─────────────────────────────
    # tenant_licenses uses JSONB feature_flags, not individual rows
    op.execute("""
        UPDATE tenant_licenses
        SET feature_flags = feature_flags || '{"feature_billing": true}'::jsonb
        WHERE TRIM(tenant_id) = 'hjmm-prod'
    """)


def downgrade():
    op.drop_table("billing_payment_allocations")
    op.drop_table("billing_payments")
    op.drop_table("billing_invoices")
    op.drop_table("billing_slips")
    op.drop_table("billing_matters")
    op.execute("DELETE FROM ui_nav_items WHERE nav_key LIKE 'billing%'")
