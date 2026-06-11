"""ediscovery_s20_base - Drop Series 1.x tables, create all Series 2.0 eDiscovery schema

Consolidates Components 1-6 schema into a single migration.
Chains from 0010_m8_platform_control.

Revision ID: ediscovery_s20_base
Revises: 0010_m8_platform_control
Create Date: 2026-03-29
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy import text

revision = "ediscovery_s20_base"
down_revision = "0010_m8_platform_control"
branch_labels = None
depends_on = None


def upgrade():

    # _DEFENSIVE_DROP_BLOCK_BEGIN
    # Patched at appliance bring-up: this migration was written for a
    # legacy DB cutover (Series 1.x → 2.0). On a fresh DB, some tables
    # below were already created by 0001_s20_initial.py (post-MariaDB
    # regenerated initial absorbed some eDiscovery tables). Drop them
    # first to allow CREATE TABLE to succeed. Idempotent: harmless on
    # both fresh and legacy DBs.
    # CASCADE handles FK dependencies cleanly.
    # TODO(repo): canonicalize ownership of these tables — pick one
    # branch per table, remove duplicate definition.
    op.execute("DROP TABLE IF EXISTS ediscovery_documents CASCADE")
    op.execute("DROP TABLE IF EXISTS ediscovery_email_threads CASCADE")
    op.execute("DROP TABLE IF EXISTS ediscovery_email_messages CASCADE")
    op.execute("DROP TABLE IF EXISTS ediscovery_issue_maps CASCADE")
    op.execute("DROP TABLE IF EXISTS ai_review_passes CASCADE")
    op.execute("DROP TABLE IF EXISTS document_review_scores CASCADE")
    op.execute("DROP TABLE IF EXISTS document_review_tags CASCADE")
    op.execute("DROP TABLE IF EXISTS document_review_decisions CASCADE")
    # _DEFENSIVE_DROP_BLOCK_END

    # ── Drop Series 1.x tables (incompatible with S2.0 schema) ───────────────
    # These used integer PKs and MySQL-style types.
    # S2.0 uses UUID PKs and PostgreSQL-native types throughout.
    op.execute(text("DROP TABLE IF EXISTS ediscovery_documents CASCADE"))
    op.execute(text("DROP TABLE IF EXISTS ediscovery_collections CASCADE"))
    # Clean up S1 enum types if they exist
    op.execute(text("DROP TYPE IF EXISTS source_type_enum CASCADE"))
    op.execute(text("DROP TYPE IF EXISTS hold_status_enum CASCADE"))
    op.execute(text("DROP TYPE IF EXISTS ack_status_enum CASCADE"))
    op.execute(text("DROP TYPE IF EXISTS term_tier_enum CASCADE"))
    op.execute(text("DROP TYPE IF EXISTS production_format_enum CASCADE"))
    op.execute(text("DROP TYPE IF EXISTS privilege_basis_enum CASCADE"))
    op.execute(text("DROP TYPE IF EXISTS production_status_enum CASCADE"))

    # ── Component 1: Core document ingestion tables ───────────────────────────

    op.create_table(
        "ediscovery_documents",
        sa.Column("id", sa.String(36), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", sa.String(36), nullable=True),
        sa.Column("collection_id", sa.String(36), nullable=True),
        sa.Column("original_filename", sa.Text, nullable=False),
        sa.Column("file_path", sa.Text, nullable=True),
        sa.Column("file_hash", sa.CHAR(64), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger, nullable=True),
        sa.Column("mime_type", sa.Text, nullable=True),
        sa.Column("doc_type", sa.Text, nullable=True),
        sa.Column("source", sa.Text, nullable=True),  # collection_upload, production_import, etc.
        sa.Column("extracted_text", sa.Text, nullable=True),
        sa.Column("page_count", sa.Integer, nullable=True),
        sa.Column("processing_status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("processing_error", sa.Text, nullable=True),
        sa.Column("is_duplicate", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("dupe_of_id", sa.String(36), nullable=True),
        sa.Column("custodian", sa.Text, nullable=True),
        sa.Column("doc_date", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        sa.Column("es_indexed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("metadata", JSONB, nullable=True),
        sa.CheckConstraint(
            "processing_status IN ('pending','processing','complete','failed')",
            name="ck_ediscovery_documents_status"
        ),
        sa.ForeignKeyConstraint(["dupe_of_id"], ["ediscovery_documents.id"],
                                ondelete="SET NULL", name="fk_ediscdoc_dupe_of"),
    )
    op.create_index("ix_ediscovery_documents_tenant", "ediscovery_documents", ["tenant_id"])
    op.create_index("ix_ediscovery_documents_matter", "ediscovery_documents", ["matter_id"])
    op.create_index("ix_ediscovery_documents_hash",
                    "ediscovery_documents", ["tenant_id", "file_hash"])
    op.create_index("ix_ediscovery_documents_status",
                    "ediscovery_documents", ["processing_status"])

    # ── Component 2: Email threading ─────────────────────────────────────────

    op.create_table(
        "ediscovery_email_threads",
        sa.Column("id", sa.String(36), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", sa.String(36), nullable=True),
        sa.Column("thread_id", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=True),
        sa.Column("participant_count", sa.Integer, nullable=True),
        sa.Column("message_count", sa.Integer, nullable=True),
        sa.Column("date_range_start", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("date_range_end", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_email_threads_tenant_matter",
                    "ediscovery_email_threads", ["tenant_id", "matter_id"])
    op.create_index("ix_email_threads_thread_id",
                    "ediscovery_email_threads", ["tenant_id", "thread_id"])

    # Email message metadata (per-document email fields)
    op.create_table(
        "ediscovery_email_messages",
        sa.Column("id", sa.String(36), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=True),
        sa.Column("message_id", sa.Text, nullable=True),
        sa.Column("in_reply_to", sa.Text, nullable=True),
        sa.Column("email_from", sa.Text, nullable=True),
        sa.Column("email_to", sa.Text, nullable=True),
        sa.Column("email_cc", sa.Text, nullable=True),
        sa.Column("email_subject", sa.Text, nullable=True),
        sa.Column("email_date", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("email_references", sa.Text, nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["ediscovery_documents.id"],
                                ondelete="CASCADE", name="fk_emailmsg_document"),
        sa.ForeignKeyConstraint(["thread_id"], ["ediscovery_email_threads.id"],
                                ondelete="SET NULL", name="fk_emailmsg_thread"),
    )
    op.create_index("ix_email_messages_document",
                    "ediscovery_email_messages", ["document_id"])
    op.create_index("ix_email_messages_thread",
                    "ediscovery_email_messages", ["thread_id"])

    # ── Component 3: Near-duplicate detection ─────────────────────────────────

    op.add_column(
        "ediscovery_documents",
        sa.Column("embedding", sa.Text, nullable=True),  # JSON-serialized vector
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("near_dupe_cluster_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("near_dupe_score", sa.Float, nullable=True),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("is_near_duplicate", sa.Boolean, nullable=False, server_default="false"),
    )

    # ── Component 4: Pleading analysis & issue map ────────────────────────────

    op.create_table(
        "ediscovery_issue_maps",
        sa.Column("id", sa.String(36), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("issue_map_json", JSONB, nullable=True),
        sa.Column("claims", JSONB, nullable=True),
        sa.Column("defenses", JSONB, nullable=True),
        sa.Column("key_facts", JSONB, nullable=True),
        sa.Column("parties", JSONB, nullable=True),
        sa.Column("key_dates", JSONB, nullable=True),
        sa.Column("rq_job_id", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','processing','complete','failed')",
            name="ck_issue_maps_status"
        ),
    )
    op.create_index("ix_issue_maps_tenant_matter",
                    "ediscovery_issue_maps", ["tenant_id", "matter_id"])

    # ── Component 5: Predictive coding ───────────────────────────────────────

    op.create_table(
        "ai_review_passes",
        sa.Column("id", sa.String(36), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", sa.String(36), nullable=False),
        sa.Column("pass_number", sa.Integer, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("model_used", sa.Text, nullable=True),
        sa.Column("issue_map_id", sa.String(36), nullable=True),
        sa.Column("documents_scored", sa.Integer, nullable=True),
        sa.Column("rq_job_id", sa.Text, nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint(
            "status IN ('pending','running','complete','failed')",
            name="ck_ai_review_passes_status"
        ),
        sa.ForeignKeyConstraint(["issue_map_id"], ["ediscovery_issue_maps.id"],
                                ondelete="SET NULL", name="fk_aipass_issue_map"),
    )
    op.create_index("ix_ai_review_passes_tenant_matter",
                    "ai_review_passes", ["tenant_id", "matter_id"])

    op.create_table(
        "document_review_scores",
        sa.Column("id", sa.String(36), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("pass_id", sa.String(36), nullable=False),
        sa.Column("relevance_score", sa.Float, nullable=True),
        sa.Column("privilege_score", sa.Float, nullable=True),
        sa.Column("responsiveness_score", sa.Float, nullable=True),
        sa.Column("predicted_tags", JSONB, nullable=True),
        sa.Column("ai_reasoning", sa.Text, nullable=True),
        sa.Column("scored_at", sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["document_id"], ["ediscovery_documents.id"],
                                ondelete="CASCADE", name="fk_reviewscore_document"),
        sa.ForeignKeyConstraint(["pass_id"], ["ai_review_passes.id"],
                                ondelete="CASCADE", name="fk_reviewscore_pass"),
    )
    op.create_index("ix_document_review_scores_document",
                    "document_review_scores", ["document_id"])
    op.create_index("ix_document_review_scores_pass",
                    "document_review_scores", ["pass_id"])
    op.create_index("ix_document_review_scores_tenant",
                    "document_review_scores", ["tenant_id"])

    # ── Component 6: Document review UI support ───────────────────────────────

    op.create_table(
        "document_review_tags",
        sa.Column("id", sa.String(36), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("tag", sa.Text, nullable=False),
        sa.Column("tag_source", sa.Text, nullable=False, server_default="user"),
        sa.Column("tagged_by", sa.String(36), nullable=True),
        sa.Column("tagged_at", sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        sa.Column("notes", sa.Text, nullable=True),
        sa.CheckConstraint(
            "tag_source IN ('user','ai','system')",
            name="ck_review_tags_source"
        ),
        sa.ForeignKeyConstraint(["document_id"], ["ediscovery_documents.id"],
                                ondelete="CASCADE", name="fk_reviewtag_document"),
    )
    op.create_index("ix_document_review_tags_document",
                    "document_review_tags", ["document_id"])
    op.create_index("ix_document_review_tags_tenant",
                    "document_review_tags", ["tenant_id"])

    op.create_table(
        "document_review_decisions",
        sa.Column("id", sa.String(36), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column("privilege_basis", sa.Text, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("decided_by", sa.String(36), nullable=True),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint(
            "decision IN ('responsive','non_responsive','privileged','needs_review','redact')",
            name="ck_review_decisions_decision"
        ),
        sa.ForeignKeyConstraint(["document_id"], ["ediscovery_documents.id"],
                                ondelete="CASCADE", name="fk_reviewdecision_document"),
    )
    op.create_index("ix_document_review_decisions_document",
                    "document_review_decisions", ["document_id"])
    op.create_index("ix_document_review_decisions_tenant",
                    "document_review_decisions", ["tenant_id"])


def downgrade():
    op.drop_table("document_review_decisions")
    op.drop_table("document_review_tags")
    op.drop_table("document_review_scores")
    op.drop_table("ai_review_passes")
    op.drop_table("ediscovery_issue_maps")
    op.drop_table("ediscovery_email_messages")
    op.drop_table("ediscovery_email_threads")
    op.drop_table("ediscovery_documents")
