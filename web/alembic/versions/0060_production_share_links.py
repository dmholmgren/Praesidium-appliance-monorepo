"""Add production share links and access tracking tables.

Revision ID: 0060_production_share_links
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = '0060_production_share_links'
down_revision = '0014_doc_annotations'  # will be set to current head
branch_labels = None
depends_on = None


def upgrade():
    # ── production_share_links ──
    op.create_table(
        'production_share_links',
        sa.Column('id', UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), primary_key=True),
        sa.Column('tenant_id', sa.String(36), nullable=False),
        sa.Column('production_set_id', UUID(as_uuid=True), sa.ForeignKey('production_sets.id', ondelete='CASCADE'), nullable=False),
        sa.Column('token', sa.String(128), nullable=False, unique=True),
        sa.Column('recipient_name', sa.String(255), nullable=True),
        sa.Column('recipient_email', sa.String(255), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('require_registration', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('access_password_hash', sa.String(255), nullable=True),
        sa.Column('max_downloads', sa.Integer(), nullable=True),
        sa.Column('download_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('is_revoked', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('idx_share_links_token', 'production_share_links', ['token'], unique=True)
    op.create_index('idx_share_links_production', 'production_share_links', ['production_set_id'])
    op.create_index('idx_share_links_tenant', 'production_share_links', ['tenant_id'])

    # ── production_share_access_log ──
    op.create_table(
        'production_share_access_log',
        sa.Column('id', UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), primary_key=True),
        sa.Column('tenant_id', sa.String(36), nullable=False),
        sa.Column('share_link_id', UUID(as_uuid=True), sa.ForeignKey('production_share_links.id', ondelete='CASCADE'), nullable=False),
        sa.Column('action', sa.String(50), nullable=False),  # registered, viewed, downloaded
        sa.Column('visitor_name', sa.String(255), nullable=True),
        sa.Column('visitor_email', sa.String(255), nullable=True),
        sa.Column('visitor_firm', sa.String(255), nullable=True),
        sa.Column('ip_address', sa.String(45), nullable=True),
        sa.Column('user_agent', sa.Text(), nullable=True),
        sa.Column('accessed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('metadata_json', JSONB, nullable=True),
    )
    op.create_index('idx_share_access_link', 'production_share_access_log', ['share_link_id'])
    op.create_index('idx_share_access_tenant', 'production_share_access_log', ['tenant_id'])
    op.create_index('idx_share_access_time', 'production_share_access_log', ['accessed_at'])


def downgrade():
    op.drop_table('production_share_access_log')
    op.drop_table('production_share_links')
