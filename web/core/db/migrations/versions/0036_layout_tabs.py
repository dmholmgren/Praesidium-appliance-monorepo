"""0036_layout_tabs

Create layout_tabs table — data-driven tab definitions for all dashboard surfaces.
Seed practice_intelligence tabs in correct order.

Revision ID: 0036_layout_tabs
Revises: 0034_collection_staging_path
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa

revision = '0036_layout_tabs'
down_revision = '0035_exchange_connector'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'layout_tabs',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=False),
                  server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('layout_slug', sa.String(128), nullable=False),
        sa.Column('tab_slug', sa.String(128), nullable=False),
        sa.Column('display_name', sa.String(128), nullable=False),
        sa.Column('display_order', sa.Integer, nullable=False, server_default='0'),
        sa.Column('permission_level', sa.String(32),
                  nullable=False, server_default='attorney'),
        sa.Column('is_visible', sa.Boolean, nullable=False, server_default='true'),
        sa.Column('icon', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('layout_slug', 'tab_slug',
                            name='uq_layout_tabs_slug_tab'),
    )
    op.create_index('ix_layout_tabs_slug',
                    'layout_tabs', ['layout_slug', 'display_order'])

    # Seed practice_intelligence tabs
    op.execute("""
        INSERT INTO layout_tabs
            (layout_slug, tab_slug, display_name, display_order,
             permission_level, is_visible, icon)
        VALUES
            ('practice_intelligence', 'firm_view',     'Firm View',     1, 'attorney',   true, 'building'),
            ('practice_intelligence', 'attorney_view', 'Attorney View', 2, 'attorney',   true, 'user'),
            ('practice_intelligence', 'matter_view',   'Matter View',   3, 'attorney',   true, 'briefcase'),
            ('practice_intelligence', 'my_view',       'My View',       4, 'attorney',   true, 'person')
        ON CONFLICT (layout_slug, tab_slug) DO UPDATE SET
            display_name  = EXCLUDED.display_name,
            display_order = EXCLUDED.display_order,
            permission_level = EXCLUDED.permission_level
    """)


def downgrade():
    op.drop_index('ix_layout_tabs_slug', table_name='layout_tabs')
    op.drop_table('layout_tabs')
