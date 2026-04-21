"""ediscovery_011_matter_folders

Revision ID: ediscovery_011
Revises: ediscovery_010
Create Date: 2026-04-07

Creates matter_folders junction table to support multiple folder paths
per matter (Clients, Docsend, Bills, etc).

Backfills existing matters.folder_path rows into matter_folders.
matters.folder_path is retained for backwards compatibility but
matter_folders is the authoritative source going forward.
"""

from alembic import op
import sqlalchemy as sa

revision = "ediscovery_011"
down_revision = "ediscovery_010"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # ── Create matter_folders table ──────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS matter_folders (
            id          UUID        NOT NULL DEFAULT gen_random_uuid(),
            tenant_id   CHAR(36)    NOT NULL,
            matter_id   UUID        NOT NULL,
            folder_path TEXT        NOT NULL,
            disk_root   TEXT,
            file_count  INTEGER,
            added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            added_by    BIGINT,
            CONSTRAINT matter_folders_pkey PRIMARY KEY (id),
            CONSTRAINT matter_folders_matter_id_fkey
                FOREIGN KEY (matter_id) REFERENCES matters(id) ON DELETE CASCADE,
            CONSTRAINT matter_folders_uq_matter_path
                UNIQUE (matter_id, folder_path)
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_matter_folders_matter_id
            ON matter_folders (matter_id)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_matter_folders_tenant_id
            ON matter_folders (tenant_id)
    """))

    # ── Grant permissions ────────────────────────────────────────────────────
    conn.execute(sa.text(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON matter_folders TO praesidium_db"
    ))

    # ── Backfill existing matters.folder_path into matter_folders ────────────
    # Only backfill rows that have a folder_path set and a valid client_id
    # Use dms_folder_matches to get disk_root and file_count where available
    conn.execute(sa.text("""
        INSERT INTO matter_folders (
            tenant_id, matter_id, folder_path, disk_root, file_count, added_at
        )
        SELECT
            m.tenant_id,
            m.id,
            m.folder_path,
            fm.disk_root,
            fm.disk_file_count,
            NOW()
        FROM matters m
        LEFT JOIN dms_folder_matches fm
            ON fm.matter_id = m.id
            AND fm.best_disk_path = m.folder_path
        WHERE m.folder_path IS NOT NULL
          AND m.folder_path != ''
        ON CONFLICT (matter_id, folder_path) DO NOTHING
    """))


def downgrade():
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS matter_folders CASCADE"))
