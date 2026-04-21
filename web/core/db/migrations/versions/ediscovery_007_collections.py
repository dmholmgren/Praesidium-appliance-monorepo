"""ediscovery_007 - collections and collection_documents tables

Revision ID: ediscovery_007
Revises: ediscovery_s20_base
Create Date: 2026-03-29

Types confirmed against live DB:
  matters.id              = UUID
  users.id                = BIGINT
  ediscovery_documents.id = UUID
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "ediscovery_007"
down_revision = "ediscovery_s20_base"
branch_labels = None
depends_on = None


def upgrade():
    # collections table was already created in a previous attempt
    # Only create collection_documents
    op.create_table(
        "collection_documents",
        sa.Column("id", sa.String(36), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("collection_id", sa.String(36), nullable=False),
        sa.Column("document_id", UUID(as_uuid=False), nullable=True),
        sa.Column("original_filename", sa.Text, nullable=False),
        sa.Column("file_hash", sa.CHAR(64), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger, nullable=True),
        sa.Column("mime_type", sa.Text, nullable=True),
        sa.Column("upload_status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("dms_match_id", sa.String(36), nullable=True),
        sa.Column("dms_match_confidence", sa.Float, nullable=True),
        sa.Column("rejection_reason", sa.Text, nullable=True),
        sa.Column("override_confirmed_by", sa.BigInteger, nullable=True),
        sa.Column("override_confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("uploaded_by", sa.BigInteger, nullable=True),
        sa.Column("uploaded_at", sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("rq_job_id", sa.Text, nullable=True),
        sa.CheckConstraint(
            "upload_status IN ('pending','processing','ingested','rejected','dms_duplicate')",
            name="ck_collection_documents_upload_status"
        ),
        sa.UniqueConstraint("collection_id", "file_hash",
                            name="uq_collection_documents_hash"),
        sa.ForeignKeyConstraint(["collection_id"], ["collections.id"],
                                ondelete="CASCADE", name="fk_colldocs_collection"),
        sa.ForeignKeyConstraint(["document_id"], ["ediscovery_documents.id"],
                                ondelete="SET NULL", name="fk_colldocs_document"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"],
                                ondelete="SET NULL", name="fk_colldocs_uploaded_by"),
        sa.ForeignKeyConstraint(["override_confirmed_by"], ["users.id"],
                                ondelete="SET NULL", name="fk_colldocs_override_by"),
    )
    op.create_index("ix_collection_documents_collection",
                    "collection_documents", ["collection_id"])
    op.create_index("ix_collection_documents_status",
                    "collection_documents", ["upload_status"])
    op.create_index("ix_collection_documents_tenant",
                    "collection_documents", ["tenant_id"])


def downgrade():
    op.drop_table("collection_documents")
    op.drop_table("collections")
