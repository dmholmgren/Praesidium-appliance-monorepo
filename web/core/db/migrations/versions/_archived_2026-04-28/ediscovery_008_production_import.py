"""ediscovery_008 - productions, production_rows, evidence_assembly_items

Revision ID: ediscovery_008
Revises: ediscovery_007
Create Date: 2026-03-31

Types confirmed against live DB (never assume — verify before writing migrations):
  matters.id              = UUID (native PostgreSQL)
  users.id                = BIGINT
  ediscovery_documents.id = UUID (native PostgreSQL)
  tenant_id               = CHAR(36) — trailing spaces, always .strip() in Python

Tables created by this migration:
  productions             — inbound production records (one per producing party load file)
  production_rows         — one row per Bates document in the load file
  evidence_assembly_items — attorney workspace: curated document set for a matter

Post-migration GRANT required (run on DB-01 as postgres):
  GRANT SELECT, INSERT, UPDATE, DELETE
    ON productions, production_rows, evidence_assembly_items
    TO praesidium_db;
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "ediscovery_008"
down_revision = "ediscovery_007"
branch_labels = None
depends_on = None


def upgrade():
    # ── productions ───────────────────────────────────────────────────────────
    # One record per inbound production (a load file + associated images).
    # The attorney uploads a DAT/CSV/OPT load file; this record tracks the
    # import lifecycle from upload through fault-tolerant row ingestion.
    op.create_table(
        "productions",
        sa.Column(
            "id",
            sa.String(36),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()::text"),
        ),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column(
            "matter_id",
            UUID(as_uuid=False),
            nullable=False,
        ),
        sa.Column("production_name", sa.Text, nullable=False),
        sa.Column("producing_party", sa.Text, nullable=True),
        sa.Column(
            "load_file_format",
            sa.Text,
            nullable=False,
            server_default="dat",
        ),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("field_map", JSONB, nullable=True),
        sa.Column("load_file_path", sa.Text, nullable=True),
        sa.Column("load_file_name", sa.Text, nullable=True),
        sa.Column("load_file_size_bytes", sa.BigInteger, nullable=True),
        sa.Column("row_count_total", sa.Integer, nullable=True),
        sa.Column("row_count_imported", sa.Integer, nullable=False, server_default="0"),
        sa.Column("row_count_failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("row_count_skipped", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rq_job_id", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("imported_by", sa.BigInteger, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','running','complete','partial','failed')",
            name="ck_productions_status",
        ),
        sa.CheckConstraint(
            "load_file_format IN ('dat','csv','opt','lfp','dii','concordance')",
            name="ck_productions_load_file_format",
        ),
    )
    op.create_index("ix_productions_tenant_id", "productions", ["tenant_id"])
    op.create_index("ix_productions_matter_id", "productions", ["matter_id"])
    op.create_index("ix_productions_status", "productions", ["status"])
    op.create_index(
        "ix_productions_tenant_matter",
        "productions",
        ["tenant_id", "matter_id"],
    )

    # ── production_rows ───────────────────────────────────────────────────────
    # One row per Bates document in the load file.
    # Fault-tolerant: a failed row does not stop the import job.
    # document_id is nullable — set after successful match/ingestion into
    # ediscovery_documents. raw_row preserves the original load file record;
    # mapped_row is the attorney-verified field mapping result.
    op.create_table(
        "production_rows",
        sa.Column(
            "id",
            sa.String(36),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()::text"),
        ),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("production_id", sa.String(36), nullable=False),
        sa.Column("row_number", sa.Integer, nullable=False),
        sa.Column("bates_begin", sa.Text, nullable=True),
        sa.Column("bates_end", sa.Text, nullable=True),
        sa.Column("raw_row", JSONB, nullable=True),
        sa.Column("mapped_row", JSONB, nullable=True),
        sa.Column(
            "document_id",
            UUID(as_uuid=False),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','imported','failed','skipped')",
            name="ck_production_rows_status",
        ),
        sa.ForeignKeyConstraint(
            ["production_id"],
            ["productions.id"],
            ondelete="CASCADE",
            name="fk_production_rows_production_id",
        ),
    )
    op.create_index(
        "ix_production_rows_production_id",
        "production_rows",
        ["production_id"],
    )
    op.create_index(
        "ix_production_rows_tenant_id",
        "production_rows",
        ["tenant_id"],
    )
    op.create_index(
        "ix_production_rows_document_id",
        "production_rows",
        ["document_id"],
    )
    op.create_index(
        "ix_production_rows_status",
        "production_rows",
        ["status"],
    )
    op.create_index(
        "ix_production_rows_bates_begin",
        "production_rows",
        ["bates_begin"],
    )

    # ── evidence_assembly_items ───────────────────────────────────────────────
    # Attorney's curated workspace — documents selected from the corpus
    # for use in a specific matter (depositions, trial, mediation, etc.).
    # workspace_section is free-text (e.g. "Depo of Smith", "Motion for Summary
    # Judgment Exhibits", "Trial Exhibit List A").
    # sort_order is attorney-controlled drag-and-drop ordering.
    op.create_table(
        "evidence_assembly_items",
        sa.Column(
            "id",
            sa.String(36),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()::text"),
        ),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column(
            "matter_id",
            UUID(as_uuid=False),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            UUID(as_uuid=False),
            nullable=False,
        ),
        sa.Column("workspace_section", sa.Text, nullable=True),
        sa.Column("display_label", sa.Text, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("added_by", sa.BigInteger, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_evidence_assembly_tenant_matter",
        "evidence_assembly_items",
        ["tenant_id", "matter_id"],
    )
    op.create_index(
        "ix_evidence_assembly_document_id",
        "evidence_assembly_items",
        ["document_id"],
    )
    op.create_index(
        "ix_evidence_assembly_section",
        "evidence_assembly_items",
        ["workspace_section"],
    )


def downgrade():
    op.drop_table("evidence_assembly_items")
    op.drop_table("production_rows")
    op.drop_table("productions")
