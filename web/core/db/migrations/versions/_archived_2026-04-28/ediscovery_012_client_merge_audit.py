"""ediscovery_012_client_merge_audit

Revision ID: ediscovery_012
Revises: ediscovery_011
Create Date: 2026-04-07

Adds merge audit columns to clients table:
  - merged_into_client_id: UUID of the client this record was merged into
  - merged_at: timestamp of merge
  - merged_by: user_id who performed the merge

When a client is merged, is_active is set to FALSE and merged_into_client_id
is set to the keep client's ID. The record is never deleted.
"""

from alembic import op
import sqlalchemy as sa

revision = "ediscovery_012"
down_revision = "ediscovery_011"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    conn.execute(sa.text("""
        ALTER TABLE clients
            ADD COLUMN IF NOT EXISTS merged_into_client_id UUID,
            ADD COLUMN IF NOT EXISTS merged_at              TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS merged_by              BIGINT
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_clients_merged_into_client_id
            ON clients (merged_into_client_id)
        WHERE merged_into_client_id IS NOT NULL
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_clients_is_active_tenant
            ON clients (tenant_id, is_active)
    """))


def downgrade():
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_clients_merged_into_client_id"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_clients_is_active_tenant"))
    conn.execute(sa.text("""
        ALTER TABLE clients
            DROP COLUMN IF EXISTS merged_into_client_id,
            DROP COLUMN IF EXISTS merged_at,
            DROP COLUMN IF EXISTS merged_by
    """))
