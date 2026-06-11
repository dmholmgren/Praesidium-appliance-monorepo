"""0033_search_term_proposals

Adds:
- search_term_proposals — attorney-reviewed search terms with full provenance chain
- legal_intelligence_chat widget registry entry

This table is the methodology record for court filings.
Every term is sourced, reviewed, and logged with approval chain.

Revision ID: 0033_search_term_proposals
Revises: 0032_ediscovery_productions
Create Date: 2026-04-14
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import TIMESTAMP

revision = '0033_search_term_proposals'
down_revision = '0032_ediscovery_productions'
branch_labels = None
depends_on = None


def upgrade():

    # ── search_term_proposals ─────────────────────────────────────────────────
    # Attorney-reviewed search terms with full provenance.
    # Source of truth for methodology reports on motions to compel.
    op.create_table('search_term_proposals',
        sa.Column('id',                sa.UUID(),      nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id',         sa.CHAR(36),    nullable=False),
        sa.Column('matter_id',         sa.UUID(),      nullable=True),   # scoped to matter
        sa.Column('collection_id',     sa.UUID(),      nullable=True),   # scoped to collection
        sa.Column('term',              sa.Text(),      nullable=False),  # the search term
        sa.Column('boolean_query',     sa.Text(),      nullable=True),   # full Boolean string
        sa.Column('source_document_id', sa.UUID(),     nullable=True),   # → ediscovery_documents.id
        sa.Column('source_paragraph',  sa.Text(),      nullable=True),   # citation to source text
        sa.Column('extraction_method', sa.String(64),  nullable=False, server_default='manual'),
        # manual | ai_chat | pleading_tar | opposing_production | court_order
        sa.Column('proposed_by',       sa.BigInteger(), nullable=True),  # → users.id
        sa.Column('proposed_at',       TIMESTAMP(),    nullable=False, server_default=sa.text('now()')),
        sa.Column('status',            sa.String(32),  nullable=False, server_default='proposed'),
        # proposed | approved | modified | rejected
        sa.Column('approved_by',       sa.BigInteger(), nullable=True),  # → users.id
        sa.Column('approved_at',       TIMESTAMP(),    nullable=True),
        sa.Column('modification_note', sa.Text(),      nullable=True),   # if modified from original
        sa.Column('original_term',     sa.Text(),      nullable=True),   # pre-modification value
        sa.Column('hit_count',         sa.Integer(),   nullable=True),   # populated after execution
        sa.Column('hit_count_by_custodian', JSONB(),   nullable=False, server_default='{}'),
        sa.Column('executed_at',       TIMESTAMP(),    nullable=True),
        sa.Column('gap_explanation',   sa.Text(),      nullable=True),   # required if hit_count = 0
        sa.Column('created_at',        TIMESTAMP(),    nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at',        TIMESTAMP(),    nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_stp_tenant_matter',   'search_term_proposals', ['tenant_id', 'matter_id'])
    op.create_index('ix_stp_tenant_status',   'search_term_proposals', ['tenant_id', 'status'])
    op.create_index('ix_stp_collection',      'search_term_proposals', ['collection_id'])
    op.create_index('ix_stp_proposed_by',     'search_term_proposals', ['proposed_by'])

    # ── legal_intelligence_chat widget registry ───────────────────────────────
    op.execute("""
        INSERT INTO widget_registry
            (widget_slug, widget_name, category, widget_type,
             render_template, default_size, permission_level, is_platform_standard)
        VALUES
            ('legal_intelligence_chat',
             'Legal Intelligence',
             'ediscovery',
             'data_panel',
             'ediscovery/widgets/legal_intelligence_chat.html',
             'medium', 'attorney', true)
        ON CONFLICT (widget_slug, COALESCE(tenant_id, '')) DO UPDATE SET
            widget_name     = EXCLUDED.widget_name,
            render_template = EXCLUDED.render_template
    """)


def downgrade():
    op.execute("DELETE FROM widget_registry WHERE widget_slug = 'legal_intelligence_chat'")
    op.drop_table('search_term_proposals')
