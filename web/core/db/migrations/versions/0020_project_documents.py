"""0020_project_documents — project_documents table + drafting_sessions.project_id

Revision ID: 0020_project_documents
Revises: 0019_payment_gateway_config
Create Date: 2026-05-14
"""

from alembic import op
import sqlalchemy as sa

revision = "0020_project_documents"
down_revision = "0019_payment_gateway_config"
branch_labels = None
depends_on = None


def upgrade():
    # ── project_documents: links documents to projects with ordering + exhibit metadata ──
    op.create_table(
        "project_documents",
        sa.Column("id", sa.dialects.postgresql.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.dialects.postgresql.UUID(), nullable=False),
        sa.Column("document_id", sa.dialects.postgresql.UUID(), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("role", sa.String(32), server_default="exhibit", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("exhibit_label", sa.String(64), nullable=True),
        sa.Column("exhibit_number", sa.Integer(), nullable=True),
        sa.Column("bates_start", sa.String(64), nullable=True),
        sa.Column("bates_end", sa.String(64), nullable=True),
        sa.Column("bates_embossed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("added_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index("ix_project_documents_project", "project_documents", ["tenant_id", "project_id"])
    op.create_index("ix_project_documents_document", "project_documents", ["tenant_id", "document_id"])
    op.create_index("ix_project_documents_sort", "project_documents", ["project_id", "role", "sort_order"])

    op.create_unique_constraint(
        "uq_project_documents_project_doc",
        "project_documents",
        ["project_id", "document_id"],
    )

    # ── Add project_id to drafting_sessions ──
    op.add_column("drafting_sessions", sa.Column("project_id", sa.dialects.postgresql.UUID(), nullable=True))
    op.create_index("ix_drafting_sessions_project", "drafting_sessions", ["project_id"])


def downgrade():
    op.drop_index("ix_drafting_sessions_project", table_name="drafting_sessions")
    op.drop_column("drafting_sessions", "project_id")
    op.drop_constraint("uq_project_documents_project_doc", "project_documents", type_="unique")
    op.drop_index("ix_project_documents_sort", table_name="project_documents")
    op.drop_index("ix_project_documents_document", table_name="project_documents")
    op.drop_index("ix_project_documents_project", table_name="project_documents")
    op.drop_table("project_documents")
