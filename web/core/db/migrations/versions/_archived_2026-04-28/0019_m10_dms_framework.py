"""0019_m10_dms_framework

Revision ID: 0019_m10_dms_framework
Revises: 0018_m10_connector_framework
Create Date: 2026-04-03

Creates the DMS indexing tables for the Windows file agent ingest pipeline:
  - dms_documents       — indexed document registry (path, content, hash, ocr_status)
  - dms_ocr_queue       — OCR work queue (priority, status, attempt_count)
  - dms_excluded_paths  — production folders excluded by agent policy
  - connector_sources   — per-tenant connector source registry

connector_sync_log already exists from 0018_m10_connector_framework.
"""
from alembic import op
import sqlalchemy as sa

revision = '0019_m10_dms_framework'
down_revision = '0018_m10_connector_framework'
branch_labels = None
depends_on = None


def upgrade():
    import os
    import psycopg2

    db_url = os.environ.get("DATABASE_URL", "")
    raw      = db_url.replace("postgresql+asyncpg://", "").replace("postgresql://", "")
    at       = raw.rfind("@")
    userpass = raw[:at]
    hostdb   = raw[at + 1:]
    colon    = userpass.find(":")
    user     = userpass[:colon]
    password = userpass[colon + 1:]
    slash    = hostdb.rfind("/")
    hostport = hostdb[:slash]
    dbname   = hostdb[slash + 1:].split("?")[0]
    hp       = hostport.split(":")
    host     = hp[0]

    conn = psycopg2.connect(host=host, port=5432, dbname=dbname,
                            user=user, password=password)
    conn.autocommit = True
    cur = conn.cursor()

    # ── dms_documents ─────────────────────────────────────────────────────────
    # Central registry for all indexed legacy share files.
    # content_text holds extracted text (up to 50K chars per file).
    # ocr_status: text_native | ocr_pending | ocr_complete | not_applicable | error
    # source: windows_agent | cifs_crawler | manual
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dms_documents (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       CHAR(36) NOT NULL,
            file_path       TEXT NOT NULL,
            folder_root     TEXT,
            file_hash       VARCHAR(16),
            file_size_bytes BIGINT,
            modified_at     TIMESTAMPTZ,
            content_text    TEXT,
            ocr_status      VARCHAR(32) NOT NULL DEFAULT 'not_applicable',
            extraction_status VARCHAR(32) NOT NULL DEFAULT 'pending',
            source          VARCHAR(32) NOT NULL DEFAULT 'windows_agent',
            agent_version   VARCHAR(16),
            indexed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_dms_documents_tenant_path UNIQUE (tenant_id, file_path)
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_dms_documents_tenant
            ON dms_documents (tenant_id)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_dms_documents_tenant_ocr
            ON dms_documents (tenant_id, ocr_status)
            WHERE ocr_status IN ('ocr_pending', 'error')
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_dms_documents_tenant_source
            ON dms_documents (tenant_id, source, indexed_at DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_dms_documents_hash
            ON dms_documents (tenant_id, file_hash)
            WHERE file_hash IS NOT NULL
    """)

    # ── dms_ocr_queue ─────────────────────────────────────────────────────────
    # Scanned PDFs and images flagged by agent for server-side Tesseract OCR.
    # priority: 10 = open matter (high), 50 = legacy share (normal)
    # status: pending | processing | complete | failed | skipped
    # FBRG-01 retrieves file bytes via CIFS bridge for PROC-01 to process.
    # Original files are NEVER modified.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dms_ocr_queue (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       CHAR(36) NOT NULL,
            document_id     UUID NOT NULL REFERENCES dms_documents(id) ON DELETE CASCADE,
            file_path       TEXT NOT NULL,
            priority        INTEGER NOT NULL DEFAULT 50,
            status          VARCHAR(32) NOT NULL DEFAULT 'pending',
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            last_attempted  TIMESTAMPTZ,
            last_error      TEXT,
            enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at    TIMESTAMPTZ,
            CONSTRAINT uq_dms_ocr_queue_document UNIQUE (document_id)
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_dms_ocr_queue_pending
            ON dms_ocr_queue (tenant_id, priority, enqueued_at)
            WHERE status = 'pending'
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_dms_ocr_queue_tenant_status
            ON dms_ocr_queue (tenant_id, status)
    """)

    # ── dms_excluded_paths ────────────────────────────────────────────────────
    # Production folders that the Windows agent skipped.
    # These are NOT indexed — they belong in eDiscovery (Module 5).
    # Stored here so the platform knows they exist and can surface them
    # in the admin console for explicit eDiscovery routing.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dms_excluded_paths (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       CHAR(36) NOT NULL,
            path            TEXT NOT NULL,
            folder_root     TEXT,
            matched_term    VARCHAR(256),
            detected_at     TIMESTAMPTZ,
            reported_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_dms_excluded_paths_tenant_path UNIQUE (tenant_id, path)
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_dms_excluded_paths_tenant
            ON dms_excluded_paths (tenant_id, reported_at DESC)
    """)

    # ── connector_sources ─────────────────────────────────────────────────────
    # Per-tenant registry of active data sources.
    # One row per source per tenant. Mirrors billing_import_sources pattern.
    # status: active | paused | error | archived
    cur.execute("""
        CREATE TABLE IF NOT EXISTS connector_sources (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       CHAR(36) NOT NULL,
            connector_type  VARCHAR(64) NOT NULL,
            source_name     VARCHAR(256) NOT NULL,
            source_config   JSONB NOT NULL DEFAULT '{}',
            status          VARCHAR(32) NOT NULL DEFAULT 'active',
            last_sync_at    TIMESTAMPTZ,
            last_sync_count INTEGER,
            last_error      TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_connector_sources_tenant_type_name
                UNIQUE (tenant_id, connector_type, source_name)
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_connector_sources_tenant
            ON connector_sources (tenant_id, connector_type)
    """)

    # ── Grants ────────────────────────────────────────────────────────────────
    for tbl in ['dms_documents', 'dms_ocr_queue',
                'dms_excluded_paths', 'connector_sources']:
        cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO praesidium_db")

    cur.close()
    conn.close()


def downgrade():
    conn = op.get_bind()
    for tbl in ['dms_ocr_queue', 'dms_excluded_paths',
                'dms_documents', 'connector_sources']:
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {tbl} CASCADE"))
