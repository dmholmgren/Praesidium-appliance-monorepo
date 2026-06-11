"""
0010_data_sources — Source-agnostic data migration support

Adds source_id FK to dms_folder_matches and file_inventory.
Seeds QNAP client and docsend shares as connector_sources rows.
Backfills source_id on existing rows.

Revision: 0010_data_sources
Revises: 0009_m_desk_c1
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "0010_data_sources"
down_revision = "0009_m_desk_c1"
branch_labels = None
depends_on = None

# Appliance tenant
TENANT_ID = "986c0fee-1390-43bb-ad28-8cd1db6de53f"


def upgrade():
    conn = op.get_bind()

    # ── 1. Add source_id columns ──────────────────────────────────────────

    op.add_column(
        "dms_folder_matches",
        sa.Column("source_id", sa.dialects.postgresql.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_dms_folder_matches_source_id",
        "dms_folder_matches",
        "connector_sources",
        ["source_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "file_inventory",
        sa.Column("source_id", sa.dialects.postgresql.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_file_inventory_source_id",
        "file_inventory",
        "connector_sources",
        ["source_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Index for filtering by source
    op.create_index(
        "ix_dms_folder_matches_source_id",
        "dms_folder_matches",
        ["source_id"],
    )
    op.create_index(
        "ix_file_inventory_source_id",
        "file_inventory",
        ["source_id"],
    )

    # ── 2. Add mount_path and read_only to connector_sources ──────────────
    # These could live in source_config JSONB, but top-level columns
    # make queries simpler and the browse endpoint faster.

    op.add_column(
        "connector_sources",
        sa.Column("mount_path", sa.Text(), nullable=True),
    )
    op.add_column(
        "connector_sources",
        sa.Column("read_only", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column(
        "connector_sources",
        sa.Column("description", sa.Text(), nullable=True),
    )

    # ── 3. Seed QNAP shares ──────────────────────────────────────────────

    conn.execute(text("""
        INSERT INTO connector_sources
            (tenant_id, connector_type, source_name, source_config,
             mount_path, read_only, description, status)
        VALUES
            (:tid, 'file_source', 'QNAP Clients',
             '{"share_type": "cifs", "unc_path": "//10.10.0.10/Clients"}'::jsonb,
             '/mnt/clients', true, 'Legacy QNAP client file shares (read-only)',
             'active'),
            (:tid, 'file_source', 'QNAP Docsend',
             '{"share_type": "cifs", "unc_path": "//10.10.0.10/Docsend"}'::jsonb,
             '/mnt/docsend', true, 'Legacy QNAP Docsend shares (read-only)',
             'active')
        ON CONFLICT (tenant_id, connector_type, source_name) DO NOTHING
    """), {"tid": TENANT_ID})

    # ── 4. Backfill source_id on existing dms_folder_matches ──────────────
    # The existing folder_path values are share-relative (e.g. "Smith, John/...")
    # and best_disk_path has the full path. Use best_disk_path prefix to
    # determine which source each match came from.

    # Get the seeded source IDs
    clients_row = conn.execute(text("""
        SELECT id FROM connector_sources
        WHERE TRIM(tenant_id) = :tid
          AND source_name = 'QNAP Clients'
    """), {"tid": TENANT_ID}).first()

    docsend_row = conn.execute(text("""
        SELECT id FROM connector_sources
        WHERE TRIM(tenant_id) = :tid
          AND source_name = 'QNAP Docsend'
    """), {"tid": TENANT_ID}).first()

    if clients_row:
        clients_id = str(clients_row[0])
        # Default all existing matches to clients source (primary share)
        conn.execute(text("""
            UPDATE dms_folder_matches
            SET source_id = CAST(:sid AS uuid)
            WHERE TRIM(tenant_id) = :tid
              AND source_id IS NULL
              AND (best_disk_path IS NULL
                   OR best_disk_path NOT LIKE '/mnt/docsend%')
        """), {"sid": clients_id, "tid": TENANT_ID})

    if docsend_row:
        docsend_id = str(docsend_row[0])
        conn.execute(text("""
            UPDATE dms_folder_matches
            SET source_id = CAST(:sid AS uuid)
            WHERE TRIM(tenant_id) = :tid
              AND source_id IS NULL
              AND best_disk_path LIKE '/mnt/docsend%'
        """), {"sid": docsend_id, "tid": TENANT_ID})

    # ── 5. Backfill source_id on file_inventory ───────────────────────────
    # file_inventory.full_path starts with the mount path

    if clients_row:
        conn.execute(text("""
            UPDATE file_inventory
            SET source_id = CAST(:sid AS uuid)
            WHERE TRIM(tenant_id) = :tid
              AND source_id IS NULL
              AND full_path LIKE '/mnt/clients%'
        """), {"sid": clients_id, "tid": TENANT_ID})

    if docsend_row:
        conn.execute(text("""
            UPDATE file_inventory
            SET source_id = CAST(:sid AS uuid)
            WHERE TRIM(tenant_id) = :tid
              AND source_id IS NULL
              AND full_path LIKE '/mnt/docsend%'
        """), {"sid": docsend_id, "tid": TENANT_ID})


def downgrade():
    op.drop_constraint("fk_file_inventory_source_id", "file_inventory", type_="foreignkey")
    op.drop_index("ix_file_inventory_source_id", "file_inventory")
    op.drop_column("file_inventory", "source_id")

    op.drop_constraint("fk_dms_folder_matches_source_id", "dms_folder_matches", type_="foreignkey")
    op.drop_index("ix_dms_folder_matches_source_id", "dms_folder_matches")
    op.drop_column("dms_folder_matches", "source_id")

    op.drop_column("connector_sources", "description")
    op.drop_column("connector_sources", "read_only")
    op.drop_column("connector_sources", "mount_path")

    # Don't delete seed rows — leave them for manual cleanup
