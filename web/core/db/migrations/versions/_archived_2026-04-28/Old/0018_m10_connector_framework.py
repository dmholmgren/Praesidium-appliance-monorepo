"""0017_m10_connector_framework

Revision ID: 0017_m10_connector_framework
Revises: 0016_m8_ssl
Create Date: 2026-03-31

Creates connector_sync_log and connector_csv_imports tables.
tenant_connectors and credentials_vault already exist (created manually, BUG-004 resolution).
"""
from alembic import op
import sqlalchemy as sa

revision = '0018_m10_connector_framework'
down_revision = '0017_dms_scan_queues'
branch_labels = None
depends_on = None


def upgrade():
    import os
    import psycopg2

    # Use direct psycopg2 with autocommit — same pattern as other manual migrations
    db_url = os.environ.get("DATABASE_URL", "")
    # Parse: postgresql+asyncpg://user:pass@host:port/dbname
    # Strip dialect prefix
    raw = db_url.replace("postgresql+asyncpg://", "").replace("postgresql://", "")
    at = raw.rfind("@")
    userpass = raw[:at]
    hostdb = raw[at + 1:]
    colon = userpass.find(":")
    user = userpass[:colon]
    password = userpass[colon + 1:]
    slash = hostdb.rfind("/")
    hostport = hostdb[:slash]
    dbname = hostdb[slash + 1:].split("?")[0]
    hp = hostport.split(":")
    host = hp[0]
    port = int(hp[1]) if len(hp) > 1 else 5432

    # Connect directly to PostgreSQL (not PgBouncer)
    pgport = 5432
    conn = psycopg2.connect(host=host, port=pgport, dbname=dbname, user=user, password=password)
    conn.autocommit = True
    cur = conn.cursor()

    # connector_sync_log
    cur.execute("""
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
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_connector_sync_log_tenant_type
            ON connector_sync_log (tenant_id, connector_type, started_at DESC)
    """)

    # connector_csv_imports
    cur.execute("""
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
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_connector_csv_imports_tenant
            ON connector_csv_imports (tenant_id, connector_type, imported_at DESC)
    """)

    # Grants
    for tbl in ['connector_sync_log', 'connector_csv_imports']:
        cur.execute(f"GRANT SELECT, INSERT, UPDATE ON {tbl} TO praesidium_db")

    cur.close()
    conn.close()


def downgrade():
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS connector_csv_imports"))
    conn.execute(sa.text("DROP TABLE IF EXISTS connector_sync_log"))
