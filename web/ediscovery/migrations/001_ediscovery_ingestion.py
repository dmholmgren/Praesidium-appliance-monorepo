"""eDiscovery COMP 1 — Document ingestion pipeline schema additions

Adds chain-of-custody and source tracking columns to ediscovery_collections.
Adds extracted_text and embedding columns to ediscovery_documents.
Creates ediscovery_document_text table for large extracted text.
Creates ediscovery_ingestion_log for audit trail.

Revision ID: ediscovery_001
Revises: (depends on your current head — update before running)
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "ediscovery_001"
down_revision = None  # UPDATE to current head before running
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------
    # 1. Add source/chain-of-custody columns to ediscovery_collections
    # ---------------------------------------------------------------
    op.add_column(
        "ediscovery_collections",
        sa.Column("source_party", sa.String(500), nullable=True),
    )
    op.add_column(
        "ediscovery_collections",
        sa.Column(
            "source_type",
            sa.Enum(
                "client_collection_dms",
                "client_collection_dedicated",
                "opposing_production",
                "internal_collection",
                "third_party_subpoena",
                name="collection_source_type",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "ediscovery_collections",
        sa.Column("received_date", sa.Date, nullable=True),
    )
    op.add_column(
        "ediscovery_collections",
        sa.Column("received_method", sa.String(255), nullable=True),
    )
    op.add_column(
        "ediscovery_collections",
        sa.Column(
            "received_by",
            sa.BigInteger().with_variant(sa.BigInteger, "mysql"),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "ediscovery_collections",
        sa.Column("original_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "ediscovery_collections",
        sa.Column("original_file_name", sa.String(500), nullable=True),
    )
    op.add_column(
        "ediscovery_collections",
        sa.Column("stated_bates_range", sa.String(255), nullable=True),
    )
    op.add_column(
        "ediscovery_collections",
        sa.Column("dms_source_path", sa.String(2000), nullable=True),
    )
    op.add_column(
        "ediscovery_collections",
        sa.Column("dms_matter_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "ediscovery_collections",
        sa.Column("originals_locked", sa.Boolean, server_default="0"),
    )
    op.add_column(
        "ediscovery_collections",
        sa.Column("originals_locked_at", sa.DateTime(fsp=3), nullable=True),
    )

    # ---------------------------------------------------------------
    # 2. Add columns to ediscovery_documents for text/embedding/chain
    # ---------------------------------------------------------------
    op.add_column(
        "ediscovery_documents",
        sa.Column("original_file_name", sa.String(500), nullable=True),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("original_path", sa.String(2000), nullable=True),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("file_size", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("mime_type", sa.String(100), nullable=True),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("page_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("ingested_at", sa.DateTime(fsp=3), nullable=True),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("ocr_applied", sa.Boolean, server_default="0"),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column(
            "dms_document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id"),
            nullable=True,
        ),
    )
    # Email-specific metadata
    op.add_column(
        "ediscovery_documents",
        sa.Column("email_from", sa.String(500), nullable=True),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("email_to", sa.Text, nullable=True),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("email_cc", sa.Text, nullable=True),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("email_subject", sa.String(1000), nullable=True),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("email_date", sa.DateTime(fsp=3), nullable=True),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("email_message_id", sa.String(500), nullable=True),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("email_in_reply_to", sa.String(500), nullable=True),
    )
    op.add_column(
        "ediscovery_documents",
        sa.Column("email_references", sa.Text, nullable=True),
    )

    op.create_index(
        "idx_edoc_dms_link",
        "ediscovery_documents",
        ["tenant_id", "dms_document_id"],
    )
    op.create_index(
        "idx_edoc_email_thread",
        "ediscovery_documents",
        ["tenant_id", "collection_id", "email_thread_id"],
    )

    # ---------------------------------------------------------------
    # 3. Extracted text table (separated for performance — text can
    #    be megabytes per document, don't want it in the main table)
    # ---------------------------------------------------------------
    op.create_table(
        "ediscovery_document_text",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.BigInteger, "mysql"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("ediscovery_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("extracted_text", sa.Text, nullable=True),
        sa.Column("text_length", sa.Integer(), nullable=True),
        sa.Column(
            "extraction_method",
            sa.Enum(
                "native_pdf",
                "ocr",
                "docx_extract",
                "msg_extract",
                "html_extract",
                "txt_passthrough",
                "dat_import",
                name="text_extraction_method",
            ),
            nullable=True,
        ),
        sa.Column("embedding_vector", sa.LargeBinary, nullable=True),
        sa.Column("embedding_model", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(fsp=3), nullable=False),
    )
    op.create_index(
        "idx_edoctext_tenant_doc",
        "ediscovery_document_text",
        ["tenant_id", "document_id"],
        unique=True,
    )

    # ---------------------------------------------------------------
    # 4. Ingestion audit log — every file touched during ingestion
    # ---------------------------------------------------------------
    op.create_table(
        "ediscovery_ingestion_log",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.BigInteger, "mysql"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column(
            "collection_id",
            sa.BigInteger(),
            sa.ForeignKey("ediscovery_collections.id"),
            nullable=False,
        ),
        sa.Column("source_file_path", sa.String(2000), nullable=False),
        sa.Column("source_file_hash", sa.String(64), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column(
            "action",
            sa.Enum(
                "ingested",
                "duplicate_skipped",
                "copy_to_originals",
                "ocr_applied",
                "text_extracted",
                "embedding_generated",
                "error",
                name="ingestion_action",
            ),
            nullable=False,
        ),
        sa.Column("result_document_id", sa.BigInteger(), nullable=True),
        sa.Column("dupe_of_document_id", sa.BigInteger(), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(fsp=3), nullable=False),
    )
    op.create_index(
        "idx_einglog_tenant_coll",
        "ediscovery_ingestion_log",
        ["tenant_id", "collection_id"],
    )


def downgrade() -> None:
    op.drop_table("ediscovery_ingestion_log")
    op.drop_table("ediscovery_document_text")

    # Drop added columns from ediscovery_documents
    for col in [
        "original_file_name", "original_path", "file_size", "mime_type",
        "page_count", "ingested_at", "ocr_applied", "dms_document_id",
        "email_from", "email_to", "email_cc", "email_subject",
        "email_date", "email_message_id", "email_in_reply_to",
        "email_references",
    ]:
        op.drop_column("ediscovery_documents", col)

    # Drop added columns from ediscovery_collections
    for col in [
        "source_party", "source_type", "received_date", "received_method",
        "received_by", "original_hash", "original_file_name",
        "stated_bates_range", "dms_source_path", "dms_matter_id",
        "originals_locked", "originals_locked_at",
    ]:
        op.drop_column("ediscovery_collections", col)

    op.execute("DROP TYPE IF EXISTS collection_source_type")
    op.execute("DROP TYPE IF EXISTS text_extraction_method")
    op.execute("DROP TYPE IF EXISTS ingestion_action")
