"""create scan_queue and dictation_queue tables

Revision ID: 0017_dms_scan_queues
Revises: 0016_m8_ssl
Create Date: 2026-03-31

"""
from alembic import op
import sqlalchemy as sa

revision = '0017_dms_scan_queues'
down_revision = '0016_m8_ssl'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS scan_queue (
            id              VARCHAR(36)     NOT NULL,
            tenant_id       CHAR(36)        NOT NULL,
            matter_id       VARCHAR(36),
            filename        TEXT            NOT NULL,
            file_size       BIGINT,
            file_type       VARCHAR(20),
            source          VARCHAR(50)     DEFAULT 'scanner',
            doc_type        VARCHAR(100),
            notes           TEXT,
            storage_path    TEXT,
            ocr_text        TEXT,
            status          VARCHAR(30)     NOT NULL DEFAULT 'pending',
            filed_path      TEXT,
            filed_at        TIMESTAMPTZ,
            filed_by        VARCHAR(100),
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            created_by      VARCHAR(100),
            PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_scan_queue_tenant_status
        ON scan_queue (tenant_id, status)
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_scan_queue_tenant_matter
        ON scan_queue (tenant_id, matter_id)
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS dictation_queue (
            id              VARCHAR(36)     NOT NULL,
            tenant_id       CHAR(36)        NOT NULL,
            user_id         BIGINT,
            matter_id       VARCHAR(36),
            filename        TEXT            NOT NULL,
            file_size       BIGINT,
            audio_format    VARCHAR(20),
            storage_path    TEXT,
            notes           TEXT,
            transcript      TEXT,
            status          VARCHAR(30)     NOT NULL DEFAULT 'pending',
            completed_at    TIMESTAMPTZ,
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            PRIMARY KEY (id)
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_dictation_queue_tenant_status
        ON dictation_queue (tenant_id, status)
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_dictation_queue_tenant_matter
        ON dictation_queue (tenant_id, matter_id)
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS dictation_queue")
    op.execute("DROP TABLE IF EXISTS scan_queue")
