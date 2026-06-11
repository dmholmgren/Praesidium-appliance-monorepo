"""Add client_drop_links table for secure inbound file collection.

Revision ID: 0031_client_drop_links
Revises: 0030_streamdeck_tmpl
Create Date: 2026-05-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

# revision identifiers
revision = "0031_client_drop_links"
down_revision = "0030_streamdeck_tmpl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "client_drop_links",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", UUID(as_uuid=True), sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("collection_id", UUID(as_uuid=True), sa.ForeignKey("ediscovery_collections.id"), nullable=True),
        # Link metadata
        sa.Column("token", sa.VARCHAR(64), nullable=False, unique=True),
        sa.Column("label", sa.VARCHAR(255), nullable=False),           # attorney-facing name
        sa.Column("instructions", sa.Text(), nullable=True),           # message shown to client
        sa.Column("recipient_name", sa.VARCHAR(255), nullable=True),   # expected uploader
        sa.Column("recipient_email", sa.VARCHAR(255), nullable=True),
        # Constraints
        sa.Column("max_uploads", sa.Integer(), nullable=True),         # null = unlimited
        sa.Column("max_file_size_mb", sa.Integer(), server_default="500"),
        sa.Column("allowed_extensions", sa.Text(), nullable=True),     # JSON array or null=all
        sa.Column("upload_count", sa.Integer(), server_default="0"),
        sa.Column("total_bytes_uploaded", sa.BigInteger(), server_default="0"),
        # Lifecycle
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        sa.Column("is_revoked", sa.Boolean(), server_default="false"),
        # Custody
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_index("idx_drop_links_token", "client_drop_links", ["token"], unique=True)
    op.create_index("idx_drop_links_tenant", "client_drop_links", ["tenant_id", "matter_id"])

    op.create_table(
        "client_drop_access_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("drop_link_id", UUID(as_uuid=True), sa.ForeignKey("client_drop_links.id"), nullable=False),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("action", sa.VARCHAR(50), nullable=False),  # viewed, registered, uploaded, failed
        sa.Column("visitor_name", sa.VARCHAR(255), nullable=True),
        sa.Column("visitor_email", sa.VARCHAR(255), nullable=True),
        sa.Column("visitor_firm", sa.VARCHAR(255), nullable=True),
        sa.Column("ip_address", sa.VARCHAR(45), nullable=True),
        sa.Column("user_agent", sa.VARCHAR(500), nullable=True),
        sa.Column("file_names", sa.Text(), nullable=True),     # JSON array of uploaded filenames
        sa.Column("file_count", sa.Integer(), nullable=True),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("metadata_json", JSONB, nullable=True),
        sa.Column("accessed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_index("idx_drop_access_link", "client_drop_access_log", ["drop_link_id", "accessed_at"])


def downgrade() -> None:
    op.drop_table("client_drop_access_log")
    op.drop_table("client_drop_links")
