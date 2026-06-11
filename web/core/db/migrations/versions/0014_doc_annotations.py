"""eDiscovery document annotations — redactions, highlights, and comments

Creates the doc_annotations table to support Tier 1 annotation features:
- Text-range redactions (character offset into extracted text)
- Multi-color highlights
- Per-document comment threads with replies

Also pre-structures for Tier 2 (coordinate-based PDF/image redactions)
and Tier 3 (markup sets, audit history).

Revision ID: 0014_doc_annotations
Revises: 0013_design_tokens
Create Date: 2026-05-11
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision = "0014_doc_annotations"
down_revision = "0013_design_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ──────────────────────────────────────────────────────────────────────────
    # TABLE: doc_annotations
    #
    # Unified annotation store for eDiscovery documents.
    # Three annotation_type values:
    #   'redaction'  — text or coordinate redaction (hides content in production)
    #   'highlight'  — colored emphasis (not burned into production)
    #   'comment'    — reviewer note, optionally anchored to text range
    #
    # Text-range annotations use text_start / text_end (char offsets into
    # the document's extracted_text). Coordinate-based annotations (Tier 2)
    # use page_number / x / y / width / height.
    #
    # Soft-delete via deleted_at — annotations are never hard-deleted so
    # audit trail is preserved. All indexes exclude soft-deleted rows.
    # ──────────────────────────────────────────────────────────────────────────
    op.create_table(
        "doc_annotations",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("document_id", sa.Uuid,
                  sa.ForeignKey("ediscovery_documents.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("collection_id", sa.Uuid, nullable=False),

        # ── Type discriminator ────────────────────────────────────────────
        sa.Column("annotation_type", sa.String(32), nullable=False,
                  comment="redaction | highlight | comment"),

        # ── Text-range anchoring (Tier 1) ─────────────────────────────────
        sa.Column("text_start", sa.Integer, nullable=True,
                  comment="Start char offset into extracted_text"),
        sa.Column("text_end", sa.Integer, nullable=True,
                  comment="End char offset into extracted_text"),
        sa.Column("selected_text", sa.Text, nullable=True,
                  comment="Snapshot of selected text at creation time (audit)"),

        # ── Coordinate-based anchoring (Tier 2) ──────────────────────────
        sa.Column("page_number", sa.Integer, nullable=True),
        sa.Column("x", sa.Numeric, nullable=True),
        sa.Column("y", sa.Numeric, nullable=True),
        sa.Column("width", sa.Numeric, nullable=True),
        sa.Column("height", sa.Numeric, nullable=True),

        # ── Redaction-specific ────────────────────────────────────────────
        sa.Column("redaction_style", sa.String(32), nullable=True,
                  comment="black | cross | text | white"),
        sa.Column("redaction_label", sa.String(255), nullable=True,
                  comment="e.g. Privileged, Confidential, Redacted"),

        # ── Highlight-specific ────────────────────────────────────────────
        sa.Column("highlight_color", sa.String(32), nullable=True,
                  comment="yellow | green | blue | red | pink | orange"),

        # ── Comment-specific ──────────────────────────────────────────────
        sa.Column("comment_text", sa.Text, nullable=True),
        sa.Column("parent_id", sa.Uuid,
                  sa.ForeignKey("doc_annotations.id", ondelete="CASCADE"),
                  nullable=True,
                  comment="Reply threading — NULL = top-level comment"),

        # ── Markup set (Tier 3) ───────────────────────────────────────────
        sa.Column("markup_set_id", sa.Uuid, nullable=True),

        # ── Audit ─────────────────────────────────────────────────────────
        sa.Column("created_by", sa.Uuid, nullable=True),
        sa.Column("created_by_name", sa.String(200), nullable=True,
                  comment="Denormalized display name for UI"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("updated_by", sa.Uuid, nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True,
                  comment="Soft delete — NULL = active"),

        # ── Constraints ───────────────────────────────────────────────────
        sa.CheckConstraint(
            "annotation_type IN ('redaction', 'highlight', 'comment')",
            name="chk_annotation_type",
        ),
    )

    # ── Indexes (all exclude soft-deleted rows) ───────────────────────────
    op.create_index(
        "idx_doc_annotations_doc",
        "doc_annotations", ["document_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_doc_annotations_tenant_coll",
        "doc_annotations", ["tenant_id", "collection_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_doc_annotations_type",
        "doc_annotations", ["document_id", "annotation_type"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_doc_annotations_parent",
        "doc_annotations", ["parent_id"],
        postgresql_where=sa.text("parent_id IS NOT NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_doc_annotations_parent", table_name="doc_annotations")
    op.drop_index("idx_doc_annotations_type", table_name="doc_annotations")
    op.drop_index("idx_doc_annotations_tenant_coll", table_name="doc_annotations")
    op.drop_index("idx_doc_annotations_doc", table_name="doc_annotations")
    op.drop_table("doc_annotations")
