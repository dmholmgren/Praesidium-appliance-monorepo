"""Bill templates, template assignments, and invoice delivery columns.

1. Creates bill_templates table (HTML templates with merge fields)
2. Creates bill_template_assignments table (client/matter/tk -> template)
3. Adds template_id to bill_runs (default for run)
4. Adds template_id to bill_run_matters (per-matter override)
5. Adds html_content, template_id, email_body, email_sent_at, email_recipient to invoices

Revision ID: 0018_bill_templates
Revises: 0017_ts_slips_dedupe_promote
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = '0018_bill_templates'
down_revision = '0017_ts_slips_dedupe_promote'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. bill_templates ──────────────────────────────────────────────────
    op.create_table(
        'bill_templates',
        sa.Column('id', UUID, primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id', sa.CHAR(36), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text),
        sa.Column('category', sa.String(50), nullable=False, server_default='standard'),
        sa.Column('html_content', sa.Text, nullable=False),
        sa.Column('email_subject', sa.String(500)),
        sa.Column('email_body', sa.Text),
        sa.Column('css_content', sa.Text),
        sa.Column('merge_fields', JSONB, server_default='[]'),
        sa.Column('is_default', sa.Boolean, nullable=False, server_default='false'),
        sa.Column('is_active', sa.Boolean, nullable=False, server_default='true'),
        sa.Column('thumbnail_path', sa.String(1000)),
        sa.Column('sort_order', sa.Integer, nullable=False, server_default='0'),
        sa.Column('created_by_id', sa.BigInteger),
        sa.Column('updated_by_id', sa.BigInteger),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
    )
    op.create_index('ix_bill_templates_tenant', 'bill_templates',
                    ['tenant_id', 'is_active', 'category'])
    op.create_index('ix_bill_templates_default', 'bill_templates',
                    ['tenant_id', 'is_default'],
                    postgresql_where=sa.text('is_default = true'))

    # ── 2. bill_template_assignments ───────────────────────────────────────
    op.create_table(
        'bill_template_assignments',
        sa.Column('id', UUID, primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id', sa.CHAR(36), nullable=False),
        sa.Column('template_id', UUID, sa.ForeignKey('bill_templates.id', ondelete='CASCADE'), nullable=False),
        sa.Column('entity_type', sa.String(30), nullable=False),
        sa.Column('entity_id', UUID, nullable=False),
        sa.Column('notes', sa.Text),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.UniqueConstraint('tenant_id', 'entity_type', 'entity_id',
                            name='uq_bill_template_assign'),
    )
    op.create_index('ix_bill_template_assign_lookup', 'bill_template_assignments',
                    ['tenant_id', 'entity_type', 'entity_id'])

    # ── 3. bill_runs.template_id ───────────────────────────────────────────
    op.add_column('bill_runs',
                  sa.Column('template_id', UUID,
                            sa.ForeignKey('bill_templates.id')))

    # ── 4. bill_run_matters.template_id ────────────────────────────────────
    op.add_column('bill_run_matters',
                  sa.Column('template_id', UUID,
                            sa.ForeignKey('bill_templates.id')))

    # ── 5. invoices — rendered content + email delivery ────────────────────
    op.add_column('invoices', sa.Column('html_content', sa.Text))
    op.add_column('invoices',
                  sa.Column('template_id', UUID,
                            sa.ForeignKey('bill_templates.id')))
    op.add_column('invoices', sa.Column('email_body', sa.Text))
    op.add_column('invoices',
                  sa.Column('email_sent_at', sa.DateTime(timezone=True)))
    op.add_column('invoices', sa.Column('email_recipient', sa.String(500)))


def downgrade():
    op.drop_column('invoices', 'email_recipient')
    op.drop_column('invoices', 'email_sent_at')
    op.drop_column('invoices', 'email_body')
    op.drop_column('invoices', 'template_id')
    op.drop_column('invoices', 'html_content')
    op.drop_column('bill_run_matters', 'template_id')
    op.drop_column('bill_runs', 'template_id')
    op.drop_table('bill_template_assignments')
    op.drop_table('bill_templates')
