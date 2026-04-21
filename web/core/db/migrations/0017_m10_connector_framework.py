"""0017_m10_connector_framework

Revision ID: 0017_m10_connector_framework
Revises: 0016_m8_ssl
Create Date: 2026-03-31

Creates connector_sync_log and connector_csv_imports tables.
tenant_connectors and credentials_vault already exist (created manually, BUG-004 resolution).
"""
from alembic import op
import sqlalchemy as sa

revision = '0017_m10_connector_framework'
down_revision = '0016_m8_ssl'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # connector_sync_log — audit trail for every sync run
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS connector_sync_log (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         CHAR(36) NOT NULL,
            connector_type    VARCHAR(64) NOT NULL,
            started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at      TIMESTAMPTZ,
            records_processed INTEGER NOT NULL DEFAULT 0,
            records_skipped   INTEGER NOT NULL DEFAULT 0,
            error_count       INTEGER NOT NULL DEFAULT 0,
            last_error        TEXT,
            triggered_by      VARCHAR(32) NOT NULL DEFAULT 'scheduler',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_connector_sync_log_tenant_type
            ON connector_sync_log (tenant_id, connector_type, started_at DESC)
    """))

    # connector_csv_imports — audit trail for drag-and-drop CSV imports
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS connector_csv_imports (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         CHAR(36) NOT NULL,
            connector_type    VARCHAR(64) NOT NULL,
            filename          VARCHAR(512) NOT NULL,
            row_count         INTEGER NOT NULL DEFAULT 0,
            imported_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            imported_by       BIGINT,
            status            VARCHAR(32) NOT NULL DEFAULT 'pending',
            error_detail      TEXT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_connector_csv_imports_tenant
            ON connector_csv_imports (tenant_id, connector_type, imported_at DESC)
    """))

    # Ensure tenant_connectors has all required columns (was created manually)
    # Add any missing columns safely
    try:
        conn.execute(sa.text(
            "ALTER TABLE tenant_connectors ADD COLUMN IF NOT EXISTS last_error TEXT"
        ))
        conn.execute(sa.text(
            "ALTER TABLE tenant_connectors ADD COLUMN IF NOT EXISTS last_sync_at TIMESTAMPTZ"
        ))
        conn.execute(sa.text(
            "ALTER TABLE tenant_connectors ADD COLUMN IF NOT EXISTS sync_frequency VARCHAR(32) NOT NULL DEFAULT 'hourly'"
        ))
        conn.execute(sa.text(
            "ALTER TABLE tenant_connectors ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}'"
        ))
        conn.execute(sa.text(
            "ALTER TABLE tenant_connectors ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'unconfigured'"
        ))
    except Exception:
        pass  # columns may already exist

    # Grant privileges
    for tbl in ['connector_sync_log', 'connector_csv_imports']:
        conn.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE ON {tbl} TO praesidium_db"))


def downgrade():
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS connector_csv_imports"))
    conn.execute(sa.text("DROP TABLE IF EXISTS connector_sync_log"))
