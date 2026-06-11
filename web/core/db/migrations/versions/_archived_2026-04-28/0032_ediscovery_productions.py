"""0032_ediscovery_productions

Adds:
- ediscovery_productions table — production sets with Bates numbering
- ediscovery_production_documents — document → production mapping
- Widget registry entries for ingest_status and production_status widgets

Revision ID: 0032_ediscovery_productions
Revises: 0031_exchange_credential_fields
Create Date: 2026-04-14
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import TIMESTAMP

revision = '0032_ediscovery_productions'
down_revision = '0031_exchange_credential_fields'
branch_labels = None
depends_on = None


def upgrade():

    # ── ediscovery_productions ────────────────────────────────────────────────
    op.create_table('ediscovery_productions',
        sa.Column('id',               sa.UUID(),      nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id',        sa.CHAR(36),    nullable=False),
        sa.Column('matter_id',        sa.UUID(),      nullable=False),
        sa.Column('collection_id',    sa.UUID(),      nullable=True),   # source collection
        sa.Column('production_name',  sa.Text(),      nullable=False),
        sa.Column('status',           sa.String(32),  nullable=False, server_default='pending'),
        # pending | processing | packaging | complete | produced | error
        sa.Column('doc_count',        sa.Integer(),   nullable=True),
        sa.Column('bates_prefix',     sa.String(32),  nullable=True),
        sa.Column('bates_start',      sa.Integer(),   nullable=True),
        sa.Column('bates_end',        sa.Integer(),   nullable=True),
        sa.Column('output_format',    sa.String(32),  nullable=False, server_default='pdf'),
        # pdf | native | tiff
        sa.Column('include_metadata', sa.Boolean(),   nullable=False, server_default='true'),
        sa.Column('include_extracted_text', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('output_path',      sa.Text(),      nullable=True),   # /mnt/praesidium/productions/{id}/
        sa.Column('notes',            sa.Text(),      nullable=True),
        sa.Column('produced_by',      sa.BigInteger(), nullable=True),  # → users.id
        sa.Column('produced_at',      TIMESTAMP(),    nullable=True),
        sa.Column('created_by',       sa.BigInteger(), nullable=True),
        sa.Column('created_at',       TIMESTAMP(),    nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at',       TIMESTAMP(),    nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ep_tenant_matter',  'ediscovery_productions', ['tenant_id', 'matter_id'])
    op.create_index('ix_ep_tenant_status',  'ediscovery_productions', ['tenant_id', 'status'])
    op.create_index('ix_ep_created_at',     'ediscovery_productions', ['tenant_id', 'created_at'])

    # ── ediscovery_production_documents ──────────────────────────────────────
    # Maps documents to productions with Bates numbers assigned
    op.create_table('ediscovery_production_documents',
        sa.Column('id',             sa.UUID(),      nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('production_id',  sa.UUID(),      nullable=False),
        sa.Column('document_id',    sa.UUID(),      nullable=False),   # → ediscovery_documents.id
        sa.Column('bates_number',   sa.String(64),  nullable=True),    # e.g. ENRON000001
        sa.Column('bates_sequence', sa.Integer(),   nullable=True),    # sort order
        sa.Column('output_path',    sa.Text(),      nullable=True),    # rendered output file
        sa.Column('created_at',     TIMESTAMP(),    nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_epd_production',  'ediscovery_production_documents', ['production_id'])
    op.create_index('ix_epd_document',    'ediscovery_production_documents', ['document_id'])
    op.create_unique_constraint('uq_epd_prod_doc',
        'ediscovery_production_documents', ['production_id', 'document_id'])

    # ── Widget registry entries ───────────────────────────────────────────────
    # Ingest status widget
    op.execute("""
        INSERT INTO widget_registry
            (widget_slug, widget_name, category, widget_type,
             data_source, render_template,
             default_size, permission_level, is_platform_standard)
        VALUES
            ('ediscovery_ingest_status',
             'Ingest Queue',
             'ediscovery',
             'data_panel',
             'modules.ediscovery.services.widget_service.get_ingest_status',
             'ediscovery/widgets/ediscovery_ingest_status.html',
             'small', 'attorney', true)
        ON CONFLICT (widget_slug, COALESCE(tenant_id, '')) DO UPDATE SET
            widget_name     = EXCLUDED.widget_name,
            data_source     = EXCLUDED.data_source,
            render_template = EXCLUDED.render_template
    """)

    # Production status widget
    op.execute("""
        INSERT INTO widget_registry
            (widget_slug, widget_name, category, widget_type,
             data_source, render_template,
             default_size, permission_level, is_platform_standard)
        VALUES
            ('ediscovery_production_status',
             'Productions',
             'ediscovery',
             'data_panel',
             'modules.ediscovery.services.widget_service.get_production_status',
             'ediscovery/widgets/ediscovery_production_status.html',
             'small', 'attorney', true)
        ON CONFLICT (widget_slug, COALESCE(tenant_id, '')) DO UPDATE SET
            widget_name     = EXCLUDED.widget_name,
            data_source     = EXCLUDED.data_source,
            render_template = EXCLUDED.render_template
    """)


def downgrade():
    op.execute("DELETE FROM widget_registry WHERE widget_slug IN ('ediscovery_ingest_status', 'ediscovery_production_status')")
    op.drop_table('ediscovery_production_documents')
    op.drop_table('ediscovery_productions')
