"""Email routing rules, payment instructions, billing settings, title co summary.

1. bill_email_routing_rules — multiple named email delivery profiles
2. bill_payment_instructions — multiple named payment instruction sets
3. billing_settings — firm info (broken-out address) + general config
4. Adds email_routing_rule_id + payment_instruction_id FKs to bill_runs,
   bill_run_matters, invoices
5. Adds summary_copy columns to bill_run_matters + invoices for title company
   parallel output (same matter, two rendered docs: detailed for client,
   summary for title company — no privileged communications exposed)

Revision ID: 0021_billing_settings
Revises: 0020_project_documents
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = '0021_billing_settings'
down_revision = '0020_project_documents'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. bill_email_routing_rules ────────────────────────────────────────
    op.create_table(
        'bill_email_routing_rules',
        sa.Column('id', UUID, primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id', sa.CHAR(36), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text),
        sa.Column('from_name', sa.String(200)),
        sa.Column('reply_to', sa.String(500)),
        sa.Column('cc', sa.String(1000)),
        sa.Column('bcc', sa.String(1000)),
        sa.Column('attachment_format', sa.String(20), server_default='pdf'),
        sa.Column('signature_html', sa.Text),
        sa.Column('billing_attorney_id', sa.BigInteger),
        sa.Column('client_id', UUID),
        sa.Column('matter_id', UUID),
        sa.Column('is_default', sa.Boolean, nullable=False, server_default='false'),
        sa.Column('is_active', sa.Boolean, nullable=False, server_default='true'),
        sa.Column('sort_order', sa.Integer, nullable=False, server_default='0'),
        sa.Column('created_by_id', sa.BigInteger),
        sa.Column('updated_by_id', sa.BigInteger),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
    )
    op.create_index('ix_berr_tenant', 'bill_email_routing_rules',
                    ['tenant_id', 'is_active'])
    op.create_index('ix_berr_default', 'bill_email_routing_rules',
                    ['tenant_id', 'is_default'],
                    postgresql_where=sa.text('is_default = true'))

    # ── 2. bill_payment_instructions ───────────────────────────────────────
    op.create_table(
        'bill_payment_instructions',
        sa.Column('id', UUID, primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id', sa.CHAR(36), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text),
        sa.Column('instructions_text', sa.Text, nullable=False),
        sa.Column('instructions_html', sa.Text),
        sa.Column('bank_name', sa.String(200)),
        sa.Column('routing_number', sa.String(20)),
        sa.Column('account_number', sa.String(40)),
        sa.Column('account_type', sa.String(30)),
        sa.Column('payment_url', sa.String(1000)),
        sa.Column('payment_gateway_id', UUID),
        sa.Column('is_default', sa.Boolean, nullable=False, server_default='false'),
        sa.Column('is_active', sa.Boolean, nullable=False, server_default='true'),
        sa.Column('sort_order', sa.Integer, nullable=False, server_default='0'),
        sa.Column('created_by_id', sa.BigInteger),
        sa.Column('updated_by_id', sa.BigInteger),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
    )
    op.create_index('ix_bpi_tenant', 'bill_payment_instructions',
                    ['tenant_id', 'is_active'])
    op.create_index('ix_bpi_default', 'bill_payment_instructions',
                    ['tenant_id', 'is_default'],
                    postgresql_where=sa.text('is_default = true'))

    # ── 3. billing_settings — firm info + general config ───────────────────
    op.create_table(
        'billing_settings',
        sa.Column('id', UUID, primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id', sa.CHAR(36), nullable=False, unique=True),
        sa.Column('firm_name', sa.String(500)),
        sa.Column('firm_address_line1', sa.String(500)),
        sa.Column('firm_suite', sa.String(200)),
        sa.Column('firm_city', sa.String(200)),
        sa.Column('firm_state', sa.String(50)),
        sa.Column('firm_zip', sa.String(20)),
        sa.Column('firm_phone', sa.String(50)),
        sa.Column('firm_email', sa.String(200)),
        sa.Column('firm_logo_url', sa.String(1000)),
        sa.Column('invoice_prefix', sa.String(20)),
        sa.Column('net_terms', sa.Integer, server_default='30'),
        sa.Column('next_invoice_number', sa.BigInteger, server_default='1'),
        sa.Column('config_json', JSONB, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
    )

    # ── 4. bill_runs — add FK columns ─────────────────────────────────────
    op.add_column('bill_runs',
                  sa.Column('email_routing_rule_id', UUID,
                            sa.ForeignKey('bill_email_routing_rules.id')))
    op.add_column('bill_runs',
                  sa.Column('payment_instruction_id', UUID,
                            sa.ForeignKey('bill_payment_instructions.id')))

    # ── 5. bill_run_matters — per-matter overrides + title co summary ─────
    op.add_column('bill_run_matters',
                  sa.Column('email_routing_rule_id', UUID,
                            sa.ForeignKey('bill_email_routing_rules.id')))
    op.add_column('bill_run_matters',
                  sa.Column('payment_instruction_id', UUID,
                            sa.ForeignKey('bill_payment_instructions.id')))
    op.add_column('bill_run_matters',
                  sa.Column('generate_summary_copy', sa.Boolean,
                            server_default='false'))
    op.add_column('bill_run_matters',
                  sa.Column('summary_recipient_name', sa.String(500)))
    op.add_column('bill_run_matters',
                  sa.Column('summary_recipient_email', sa.String(500)))
    op.add_column('bill_run_matters',
                  sa.Column('summary_template_id', UUID,
                            sa.ForeignKey('bill_templates.id')))
    op.add_column('bill_run_matters',
                  sa.Column('summary_invoice_id', UUID))

    # ── 6. invoices — frozen record of which profiles used ────────────────
    op.add_column('invoices',
                  sa.Column('payment_instruction_id', UUID,
                            sa.ForeignKey('bill_payment_instructions.id')))
    op.add_column('invoices',
                  sa.Column('email_routing_rule_id', UUID,
                            sa.ForeignKey('bill_email_routing_rules.id')))
    op.add_column('invoices',
                  sa.Column('invoice_format', sa.String(20),
                            server_default='detailed'))
    op.add_column('invoices',
                  sa.Column('parent_invoice_id', UUID,
                            sa.ForeignKey('invoices.id')))


def downgrade():
    op.drop_column('invoices', 'parent_invoice_id')
    op.drop_column('invoices', 'invoice_format')
    op.drop_column('invoices', 'email_routing_rule_id')
    op.drop_column('invoices', 'payment_instruction_id')
    op.drop_column('bill_run_matters', 'summary_invoice_id')
    op.drop_column('bill_run_matters', 'summary_template_id')
    op.drop_column('bill_run_matters', 'summary_recipient_email')
    op.drop_column('bill_run_matters', 'summary_recipient_name')
    op.drop_column('bill_run_matters', 'generate_summary_copy')
    op.drop_column('bill_run_matters', 'payment_instruction_id')
    op.drop_column('bill_run_matters', 'email_routing_rule_id')
    op.drop_column('bill_runs', 'payment_instruction_id')
    op.drop_column('bill_runs', 'email_routing_rule_id')
    op.drop_table('billing_settings')
    op.drop_table('bill_payment_instructions')
    op.drop_table('bill_email_routing_rules')
