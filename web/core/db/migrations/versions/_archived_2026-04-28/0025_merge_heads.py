"""0025_merge_heads

Merge migration — consolidates two intentional Alembic heads into one.

    Platform branch:    0024_drafting_engine
    eDiscovery branch:  ediscovery_012

After this migration, alembic current will show a single head:
    0025_merge_heads (head)

No DDL. This is a bookkeeping-only migration.

Revision ID: 0025_merge_heads
Revises: 0024_drafting_engine, ediscovery_012
Create Date: 2026-04-08
"""

from alembic import op

# revision identifiers
revision = '0025_merge_heads'
down_revision = ('0024_drafting_engine', 'ediscovery_012')
branch_labels = None
depends_on = None


def upgrade():
    # Merge only — no DDL
    pass


def downgrade():
    # Downgrade not supported for production deployments.
    pass
