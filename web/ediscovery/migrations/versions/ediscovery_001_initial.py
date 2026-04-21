"""eDiscovery module — add source tracking to collections, create new tables.

Revision ID: ediscovery_001
Revises: <previous_migration>
Create Date: 2026-03-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import BIGINT

revision = "ediscovery_001"
down_revision = None  # Set to actual previous migration ID
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ----------------------------------------------------------------
    # ALTER ediscovery_collections — add source tracking columns
    # ----------------------------------------------------------------
    op.add_column("ediscovery_collections", sa.Column("source_party", sa.String(500)))
    op.add_column("ediscovery_collections", sa.Column(
        "source_type",
        sa.Enum(
            "client_collection_dms", "client_collection_dedicated",
            "opposing_production", "internal_collection", "third_party_subpoena",
            name="source_type_enum",
        ),
        nullable=False,
        server_default="client_collection_dms",
    ))
    op.add_column("ediscovery_collections", sa.Column("received_date", sa.Date))
    op.add_column("ediscovery_collections", sa.Column("received_method", sa.String(255)))
    op.add_column("ediscovery_collections", sa.Column(
        "received_by", BIGINT(unsigned=True),
    ))
    op.add_column("ediscovery_collections", sa.Column("original_hash", sa.String(64)))
    op.add_column("ediscovery_collections", sa.Column("original_file_name", sa.String(500)))
    op.add_column("ediscovery_collections", sa.Column("stated_bates_range", sa.String(255)))
    op.add_column("ediscovery_collections", sa.Column("dms_source_path", sa.String(2000)))
    op.add_column("ediscovery_collections", sa.Column(
        "updated_at", sa.DateTime(fsp=3),
    ))
    op.create_foreign_key(
        "fk_collections_received_by", "ediscovery_collections",
        "users", ["received_by"], ["id"],
    )
    op.create_index(
        "idx_tenant_status", "ediscovery_collections",
        ["tenant_id", "status"],
    )

    # ----------------------------------------------------------------
    # ALTER ediscovery_documents — add original_path, working_path,
    # DMS cross-reference, email fields, near-dupe fields, embedding
    # ----------------------------------------------------------------
    op.add_column("ediscovery_documents", sa.Column("original_path", sa.String(2000)))
    op.add_column("ediscovery_documents", sa.Column("working_path", sa.String(2000)))
    op.add_column("ediscovery_documents", sa.Column("file_name", sa.String(500)))
    op.add_column("ediscovery_documents", sa.Column("file_size", BIGINT(unsigned=True)))
    op.add_column("ediscovery_documents", sa.Column("mime_type", sa.String(100)))
    op.add_column("ediscovery_documents", sa.Column(
        "dms_document_id", BIGINT(unsigned=True),
    ))
    op.add_column("ediscovery_documents", sa.Column("extracted_text", sa.Text))
    op.add_column("ediscovery_documents", sa.Column("page_count", BIGINT(unsigned=True)))
    op.add_column("ediscovery_documents", sa.Column("email_from", sa.String(500)))
    op.add_column("ediscovery_documents", sa.Column("email_to", sa.Text))
    op.add_column("ediscovery_documents", sa.Column("email_cc", sa.Text))
    op.add_column("ediscovery_documents", sa.Column("email_subject", sa.String(1000)))
    op.add_column("ediscovery_documents", sa.Column("email_date", sa.DateTime(fsp=3)))
    op.add_column("ediscovery_documents", sa.Column("email_message_id", sa.String(500)))
    op.add_column("ediscovery_documents", sa.Column("email_in_reply_to", sa.String(500)))
    op.add_column("ediscovery_documents", sa.Column("email_references", sa.Text))
    op.add_column("ediscovery_documents", sa.Column("is_near_duplicate", sa.Boolean, server_default="0"))
    op.add_column("ediscovery_documents", sa.Column("near_dupe_of_id", BIGINT(unsigned=True)))
    op.add_column("ediscovery_documents", sa.Column("near_dupe_score", sa.DECIMAL(5, 4)))
    op.add_column("ediscovery_documents", sa.Column("embedding", sa.JSON))
    op.add_column("ediscovery_documents", sa.Column("relevance_breakdown", sa.JSON))
    op.add_column("ediscovery_documents", sa.Column("coding_notes", sa.Text))
    op.add_column("ediscovery_documents", sa.Column(
        "ingested_at", sa.DateTime(fsp=3), server_default=sa.func.now(),
    ))
    op.create_foreign_key(
        "fk_docs_dms_document", "ediscovery_documents",
        "documents", ["dms_document_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_docs_near_dupe", "ediscovery_documents",
        "ediscovery_documents", ["near_dupe_of_id"], ["id"],
    )
    op.create_index(
        "idx_tenant_thread", "ediscovery_documents",
        ["tenant_id", "email_thread_id"],
    )
    op.create_index(
        "idx_tenant_review_status", "ediscovery_documents",
        ["tenant_id", "review_status"],
    )

    # ----------------------------------------------------------------
    # CREATE legal_holds
    # ----------------------------------------------------------------
    op.create_table(
        "legal_holds",
        sa.Column("id", BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", BIGINT(unsigned=True), sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("hold_name", sa.String(255), nullable=False),
        sa.Column("hold_scope", sa.Text, nullable=False),
        sa.Column("status", sa.Enum("active", "modified", "released", name="hold_status_enum"),
                  nullable=False, server_default="active"),
        sa.Column("issued_date", sa.Date, nullable=False),
        sa.Column("issued_by", BIGINT(unsigned=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("modified_date", sa.Date),
        sa.Column("modified_reason", sa.Text),
        sa.Column("released_date", sa.Date),
        sa.Column("released_by", BIGINT(unsigned=True), sa.ForeignKey("users.id")),
        sa.Column("released_reason", sa.Text),
        sa.Column("notice_document_path", sa.String(2000)),
        sa.Column("created_at", sa.DateTime(fsp=3), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_lh_tenant_matter", "legal_holds", ["tenant_id", "matter_id"])
    op.create_index("idx_lh_tenant_status", "legal_holds", ["tenant_id", "status"])

    # ----------------------------------------------------------------
    # CREATE hold_custodians
    # ----------------------------------------------------------------
    op.create_table(
        "hold_custodians",
        sa.Column("id", BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("hold_id", BIGINT(unsigned=True), sa.ForeignKey("legal_holds.id"), nullable=False),
        sa.Column("custodian_name", sa.String(255), nullable=False),
        sa.Column("custodian_email", sa.String(255)),
        sa.Column("custodian_role", sa.String(255)),
        sa.Column("status", sa.Enum("pending", "acknowledged", "reminded", "escalated",
                                    name="ack_status_enum"),
                  nullable=False, server_default="pending"),
        sa.Column("notified_at", sa.DateTime(fsp=3)),
        sa.Column("acknowledged_at", sa.DateTime(fsp=3)),
        sa.Column("last_reminder_at", sa.DateTime(fsp=3)),
        sa.Column("reminder_count", BIGINT(unsigned=True), server_default="0"),
        sa.Column("escalated_at", sa.DateTime(fsp=3)),
        sa.Column("escalated_to", BIGINT(unsigned=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(fsp=3), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_hc_tenant_hold", "hold_custodians", ["tenant_id", "hold_id"])
    op.create_index("idx_hc_tenant_status", "hold_custodians", ["tenant_id", "status"])

    # ----------------------------------------------------------------
    # CREATE search_term_sets
    # ----------------------------------------------------------------
    op.create_table(
        "search_term_sets",
        sa.Column("id", BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("collection_id", BIGINT(unsigned=True),
                  sa.ForeignKey("ediscovery_collections.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("version_label", sa.String(255)),
        sa.Column("author", sa.String(255)),
        sa.Column("notes", sa.Text),
        sa.Column("format_o365_kql", sa.Text),
        sa.Column("format_gmail_vault", sa.Text),
        sa.Column("format_relativity", sa.Text),
        sa.Column("format_generic_boolean", sa.Text),
        sa.Column("hit_count_results", sa.JSON),
        sa.Column("created_at", sa.DateTime(fsp=3), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", BIGINT(unsigned=True), sa.ForeignKey("users.id")),
    )
    op.create_index("idx_sts_tenant_collection", "search_term_sets", ["tenant_id", "collection_id"])

    # ----------------------------------------------------------------
    # CREATE search_terms
    # ----------------------------------------------------------------
    op.create_table(
        "search_terms",
        sa.Column("id", BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("term_set_id", BIGINT(unsigned=True),
                  sa.ForeignKey("search_term_sets.id"), nullable=False),
        sa.Column("tier", sa.Enum("tier_1", "tier_2", "tier_3", name="term_tier_enum"), nullable=False),
        sa.Column("issue_element", sa.String(500)),
        sa.Column("term_text", sa.Text, nullable=False),
        sa.Column("hit_count", BIGINT(unsigned=True)),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime(fsp=3), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_st_tenant_termset", "search_terms", ["tenant_id", "term_set_id"])

    # ----------------------------------------------------------------
    # CREATE esi_protocols
    # ----------------------------------------------------------------
    op.create_table(
        "esi_protocols",
        sa.Column("id", BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", BIGINT(unsigned=True), sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("collection_id", BIGINT(unsigned=True),
                  sa.ForeignKey("ediscovery_collections.id")),
        sa.Column("version", BIGINT(unsigned=True), nullable=False, server_default="1"),
        sa.Column("version_label", sa.String(255)),
        sa.Column("protocol_data", sa.JSON),
        sa.Column("document_path", sa.String(2000)),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime(fsp=3), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", BIGINT(unsigned=True), sa.ForeignKey("users.id")),
    )
    op.create_index("idx_ep_tenant_matter", "esi_protocols", ["tenant_id", "matter_id"])

    # ----------------------------------------------------------------
    # CREATE productions
    # ----------------------------------------------------------------
    op.create_table(
        "productions",
        sa.Column("id", BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("collection_id", BIGINT(unsigned=True),
                  sa.ForeignKey("ediscovery_collections.id"), nullable=False),
        sa.Column("production_name", sa.String(255), nullable=False),
        sa.Column("produced_to", sa.String(500), nullable=False),
        sa.Column("production_date", sa.DateTime(fsp=3)),
        sa.Column("format", sa.Enum("relativity_dat", "native", "tiff", "pdf",
                                    name="production_format_enum"),
                  nullable=False, server_default="relativity_dat"),
        sa.Column("bates_prefix", sa.String(20), nullable=False),
        sa.Column("bates_start", BIGINT(unsigned=True), nullable=False),
        sa.Column("bates_end", BIGINT(unsigned=True)),
        sa.Column("total_documents", BIGINT(unsigned=True), server_default="0"),
        sa.Column("total_pages", BIGINT(unsigned=True), server_default="0"),
        sa.Column("output_path", sa.String(2000)),
        sa.Column("output_hash", sa.String(64)),
        sa.Column("status", sa.Enum("preparing", "qc_review", "produced",
                                    name="production_status_enum"),
                  nullable=False, server_default="preparing"),
        sa.Column("dat_file_path", sa.String(2000)),
        sa.Column("opt_file_path", sa.String(2000)),
        sa.Column("notes", sa.Text),
        sa.Column("metadata", sa.JSON),
        sa.Column("created_at", sa.DateTime(fsp=3), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", BIGINT(unsigned=True), sa.ForeignKey("users.id")),
    )
    op.create_index("idx_prod_tenant_collection", "productions", ["tenant_id", "collection_id"])

    # ----------------------------------------------------------------
    # CREATE privilege_log_entries
    # ----------------------------------------------------------------
    op.create_table(
        "privilege_log_entries",
        sa.Column("id", BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("collection_id", BIGINT(unsigned=True),
                  sa.ForeignKey("ediscovery_collections.id"), nullable=False),
        sa.Column("document_id", BIGINT(unsigned=True),
                  sa.ForeignKey("ediscovery_documents.id"), nullable=False),
        sa.Column("log_number", BIGINT(unsigned=True)),
        sa.Column("doc_date", sa.Date),
        sa.Column("author", sa.String(500)),
        sa.Column("recipients", sa.Text),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("privilege_basis", sa.Enum(
            "attorney_client", "work_product", "joint_defense",
            "common_interest", "other", name="privilege_basis_enum",
        ), nullable=False),
        sa.Column("privilege_detail", sa.Text),
        sa.Column("bates_begin", sa.String(50)),
        sa.Column("bates_end", sa.String(50)),
        sa.Column("created_at", sa.DateTime(fsp=3), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", BIGINT(unsigned=True), sa.ForeignKey("users.id")),
    )
    op.create_index("idx_ple_tenant_collection", "privilege_log_entries", ["tenant_id", "collection_id"])
    op.create_index("idx_ple_tenant_document", "privilege_log_entries", ["tenant_id", "document_id"])


def downgrade() -> None:
    op.drop_table("privilege_log_entries")
    op.drop_table("productions")
    op.drop_table("esi_protocols")
    op.drop_table("search_terms")
    op.drop_table("search_term_sets")
    op.drop_table("hold_custodians")
    op.drop_table("legal_holds")

    # Drop added columns from ediscovery_documents
    for col in [
        "original_path", "working_path", "file_name", "file_size", "mime_type",
        "dms_document_id", "extracted_text", "page_count",
        "email_from", "email_to", "email_cc", "email_subject", "email_date",
        "email_message_id", "email_in_reply_to", "email_references",
        "is_near_duplicate", "near_dupe_of_id", "near_dupe_score",
        "embedding", "relevance_breakdown", "coding_notes", "ingested_at",
    ]:
        op.drop_column("ediscovery_documents", col)

    # Drop added columns from ediscovery_collections
    for col in [
        "source_party", "source_type", "received_date", "received_method",
        "received_by", "original_hash", "original_file_name",
        "stated_bates_range", "dms_source_path", "updated_at",
    ]:
        op.drop_column("ediscovery_collections", col)
