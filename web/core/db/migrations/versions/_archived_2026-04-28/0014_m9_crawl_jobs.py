"""Module 9 Component 4 — File Crawl Trigger schema

Creates two tables:
  cifs_crawl_jobs    — one row per crawl run (trigger, status, progress)
  cifs_crawl_entries — one row per file discovered during a crawl

The crawl is read-only against legacy CIFS mounts (/mnt/clients, /mnt/docsend).
It never writes to those mounts. It indexes file metadata for federated search.

Revision ID: 0014_m9_crawl_jobs
Revises: 0013_m9_user_mgmt
Create Date: 2026-03-31

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014_m9_crawl_jobs"
down_revision = "0013_m9_user_mgmt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── cifs_crawl_jobs ───────────────────────────────────────────────────────
    op.create_table(
        "cifs_crawl_jobs",
        sa.Column("id", sa.String(36), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("mount", sa.String(50), nullable=False,
                  comment="clients | docsend | praesidium"),
        sa.Column("root_path", sa.Text, nullable=False,
                  comment="Relative path passed to CIFS bridge, e.g. '' for root"),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued",
                  comment="queued | running | complete | failed | cancelled"),
        sa.Column("triggered_by", sa.BigInteger, nullable=True,
                  comment="users.id of admin who triggered the crawl"),
        sa.Column("queued_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("files_discovered", sa.Integer, nullable=False, server_default="0"),
        sa.Column("files_indexed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("files_skipped", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("rq_job_id", sa.Text, nullable=True),
        sa.Column("options", JSONB, nullable=False, server_default="{}",
                  comment="depth_limit, extensions_filter, etc."),
        sa.CheckConstraint(
            "status IN ('queued','running','complete','failed','cancelled')",
            name="ck_cifs_crawl_jobs_status"
        ),
        sa.CheckConstraint(
            "mount IN ('clients','docsend','praesidium')",
            name="ck_cifs_crawl_jobs_mount"
        ),
    )
    op.create_index("ix_cifs_crawl_jobs_tenant_id", "cifs_crawl_jobs", ["tenant_id"])
    op.create_index("ix_cifs_crawl_jobs_status", "cifs_crawl_jobs", ["status"])
    op.create_index("ix_cifs_crawl_jobs_queued_at", "cifs_crawl_jobs", ["queued_at"])

    # ── cifs_crawl_entries ────────────────────────────────────────────────────
    op.create_table(
        "cifs_crawl_entries",
        sa.Column("id", sa.String(36), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("mount", sa.String(50), nullable=False),
        sa.Column("file_path", sa.Text, nullable=False,
                  comment="Path relative to mount root"),
        sa.Column("file_name", sa.Text, nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger, nullable=True),
        sa.Column("mime_type", sa.Text, nullable=True),
        sa.Column("modified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("is_directory", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("discovered_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("matter_id", sa.String(36), nullable=True,
                  comment="Matched matter UUID if path pattern matches"),
        sa.Column("match_confidence", sa.Float, nullable=True),
        sa.ForeignKeyConstraint(
            ["job_id"], ["cifs_crawl_jobs.id"],
            ondelete="CASCADE",
            name="fk_cifs_crawl_entries_job"
        ),
    )
    op.create_index("ix_cifs_crawl_entries_job_id", "cifs_crawl_entries", ["job_id"])
    op.create_index("ix_cifs_crawl_entries_tenant_id", "cifs_crawl_entries", ["tenant_id"])
    op.create_index("ix_cifs_crawl_entries_file_path", "cifs_crawl_entries", ["file_path"])
    op.create_index("ix_cifs_crawl_entries_matter_id", "cifs_crawl_entries", ["matter_id"])

    # Unique: one entry per path per job
    op.create_unique_constraint(
        "uq_cifs_crawl_entries_job_path",
        "cifs_crawl_entries",
        ["job_id", "file_path"]
    )


def downgrade() -> None:
    op.drop_table("cifs_crawl_entries")
    op.drop_table("cifs_crawl_jobs")
