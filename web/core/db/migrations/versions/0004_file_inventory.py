"""0004 file inventory

Revision ID: 0004_file_inventory
Revises: 0003_bill_run_engine
Create Date: 2026-05-03

Adds file_inventory table for legacy filesystem cataloging.
Stores every file and folder found on the QNAP mount, with
proposed client/matter matches derived from the canonical tables.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0004_file_inventory"
down_revision = "0003_bill_run_engine"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "file_inventory",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("scan_run_id", UUID(as_uuid=True), nullable=False),

        # Filesystem data
        sa.Column("entry_type", sa.String(20), nullable=False),  # 'folder' or 'file'
        sa.Column("full_path", sa.Text(), nullable=False),
        sa.Column("parent_path", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("extension", sa.String(50), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),

        # Top-level folder (the client folder name)
        sa.Column("root_folder", sa.Text(), nullable=True),

        # Match proposals — filled by the matching pass
        sa.Column("proposed_client_id", UUID(as_uuid=True), nullable=True),
        sa.Column("proposed_client_name", sa.String(500), nullable=True),
        sa.Column("proposed_matter_id", UUID(as_uuid=True), nullable=True),
        sa.Column("proposed_matter_name", sa.String(500), nullable=True),
        sa.Column("match_method", sa.String(100), nullable=True),
        sa.Column("match_confidence", sa.Numeric(), nullable=True),

        # Human review
        sa.Column("match_status", sa.String(50), server_default=sa.text("'pending'")),
        # pending → confirmed → rejected → manual
        sa.Column("confirmed_client_id", UUID(as_uuid=True), nullable=True),
        sa.Column("confirmed_matter_id", UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_by_id", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),

        # Stats (for folders)
        sa.Column("child_file_count", sa.Integer(), nullable=True),
        sa.Column("child_folder_count", sa.Integer(), nullable=True),
        sa.Column("total_size_bytes", sa.BigInteger(), nullable=True),

        # Classification flags
        sa.Column("classification", sa.String(50), server_default=sa.text("'document'")),
        # document — normal file, parse as document
        # ediscovery_archive — .pst/.ost, flag for eDiscovery ingestion
        # ediscovery_production — production zip/folder with Bates-numbered content
        # ediscovery_load_file — .dat/.opt/.lfp Relativity/Concordance load files
        # skip — noise (ISOs, VM images, shortcuts, thumbs.db)
        # unknown — needs manual triage
        sa.Column("is_active_matter", sa.Boolean(), server_default=sa.text("false")),
        # True if the matched matter has recent billing activity
        sa.Column("priority", sa.String(20), server_default=sa.text("'normal'")),
        # immediate — active matter, parseable file type
        # normal — active matter, low-priority file type or closed matter parseable
        # deferred — closed matter, non-urgent
        # skip — noise or duplicate

        # Metadata
        sa.Column("metadata", JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )

    op.create_index("ix_file_inventory_tenant_scan",
                     "file_inventory", ["tenant_id", "scan_run_id"])
    op.create_index("ix_file_inventory_root_folder",
                     "file_inventory", ["tenant_id", "root_folder"])
    op.create_index("ix_file_inventory_path",
                     "file_inventory", ["tenant_id", "full_path"],
                     unique=False)
    op.create_index("ix_file_inventory_match_status",
                     "file_inventory", ["tenant_id", "match_status"])


def downgrade():
    op.drop_table("file_inventory")
