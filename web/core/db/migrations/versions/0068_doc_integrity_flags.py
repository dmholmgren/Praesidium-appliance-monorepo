"""doc integrity scans + flags — adversarial-document defense substrate.

Two tables:
  doc_integrity_scans — one row per (corpus, doc_id): proves a scan happened,
    records scanner version and outcome. Clean scans are first-class records.
  doc_integrity_flags — zero or more findings per scanned document: hidden
    render text, invisible Unicode, prompt-injection instruction patterns,
    off-page text, microscopic fonts. Evidence carries char offsets into the
    canonical string (§0 compliant) where locatable — the hidden text is
    PRESERVED in canonical and flagged, never stripped: a produced document
    containing hidden AI-manipulation text is itself evidence.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0068_doc_integrity_flags"
down_revision = "0067_ai_call_document_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "doc_integrity_scans",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("corpus", sa.Text, nullable=False),
        sa.Column("doc_id", UUID(as_uuid=True), nullable=False),
        sa.Column("scanner_version", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),  # clean|flagged|error|file_not_found|unsupported
        sa.Column("flags_found", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("scanned_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("corpus", "doc_id", name="uq_integrity_scan_doc"),
    )
    op.create_index("ix_integrity_scans_tenant", "doc_integrity_scans",
                    ["tenant_id", "status"])

    op.create_table(
        "doc_integrity_flags",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("corpus", sa.Text, nullable=False),
        sa.Column("doc_id", UUID(as_uuid=True), nullable=False),
        sa.Column("flag_type", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False),  # info|warning|critical
        sa.Column("page_number", sa.Integer, nullable=True),
        sa.Column("char_start", sa.Integer, nullable=True),
        sa.Column("char_end", sa.Integer, nullable=True),
        sa.Column("evidence", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("scanner_version", sa.Text, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_integrity_flags_doc", "doc_integrity_flags",
                    ["tenant_id", "corpus", "doc_id"])
    op.create_index("ix_integrity_flags_type", "doc_integrity_flags",
                    ["flag_type", "severity"])


def downgrade():
    op.drop_index("ix_integrity_flags_type")
    op.drop_index("ix_integrity_flags_doc")
    op.drop_table("doc_integrity_flags")
    op.drop_index("ix_integrity_scans_tenant")
    op.drop_table("doc_integrity_scans")
