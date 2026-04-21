"""0035_exchange_connector

Add entity_type to connector_entity_map.
Seed connector_sources row for HJMM Exchange.

Revision ID: 0035_exchange_connector
Revises: 0034_collection_staging_path
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa

revision = '0035_exchange_connector'
down_revision = '0034_collection_staging_path'
branch_labels = None
depends_on = None


def upgrade():
    # Add entity_type to connector_entity_map
    op.add_column(
        'connector_entity_map',
        sa.Column(
            'entity_type',
            sa.String(32),
            nullable=False,
            server_default='mailbox',
        )
    )
    op.create_index(
        'ix_cem_entity_type',
        'connector_entity_map',
        ['tenant_id', 'connector_type', 'entity_type']
    )

    # Seed connector_sources row for HJMM Exchange
    op.execute("""
        INSERT INTO connector_sources (
            id, tenant_id, connector_type, source_name,
            source_config, status, created_at, updated_at
        )
        SELECT
            gen_random_uuid(),
            tc.tenant_id,
            'exchange',
            'Microsoft Exchange — HJMM',
            jsonb_build_object(
                'ews_url',                tc.config->>'ews_url',
                'sync_email',             (tc.config->>'sync_email')::boolean,
                'sync_calendar',          (tc.config->>'sync_calendar')::boolean,
                'email_lookback_days',    (tc.config->>'email_lookback_days')::int,
                'calendar_lookback_days', (tc.config->>'calendar_lookback_days')::int,
                'calendar_lookahead_days',(tc.config->>'calendar_lookahead_days')::int
            ),
            'active',
            NOW(),
            NOW()
        FROM tenant_connectors tc
        WHERE trim(tc.tenant_id) = 'hjmm-prod'
          AND tc.connector = 'exchange'
        ON CONFLICT DO NOTHING
    """)

    # Flip tenant_connectors status to active
    op.execute("""
        UPDATE tenant_connectors
        SET status = 'active', updated_at = NOW()
        WHERE trim(tenant_id) = 'hjmm-prod'
          AND connector = 'exchange'
    """)


def downgrade():
    op.drop_index('ix_cem_entity_type', table_name='connector_entity_map')
    op.drop_column('connector_entity_map', 'entity_type')
