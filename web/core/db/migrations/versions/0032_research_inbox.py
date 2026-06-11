"""Extend scan_queue for research inbox workflow.

Add user_id, ai_suggested_matter_id, ai_confidence, ai_reasoning,
ai_suggested_subfolder, and source_url columns to support the
research inbox escalation loop.

Revision ID: 0032_research_inbox
Revises: 0031_client_drop_links
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0032_research_inbox"
down_revision = "0031_client_drop_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # user_id — proper FK for per-user inbox filtering
    op.add_column("scan_queue", sa.Column(
        "user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True
    ))

    # AI proposal columns — escalation loop
    op.add_column("scan_queue", sa.Column(
        "ai_suggested_matter_id", sa.VARCHAR(255), nullable=True
    ))
    op.add_column("scan_queue", sa.Column(
        "ai_confidence", sa.Float(), nullable=True
    ))
    op.add_column("scan_queue", sa.Column(
        "ai_reasoning", sa.Text(), nullable=True
    ))
    op.add_column("scan_queue", sa.Column(
        "ai_suggested_subfolder", sa.VARCHAR(255),
        server_default="04-Research", nullable=True
    ))

    # source_url — for web research captures (browser save-as, etc.)
    op.add_column("scan_queue", sa.Column(
        "source_url", sa.Text(), nullable=True
    ))

    # reviewed_at / reviewed_by — when human accepts/rejects AI proposal
    op.add_column("scan_queue", sa.Column(
        "reviewed_at", sa.DateTime(timezone=True), nullable=True
    ))
    op.add_column("scan_queue", sa.Column(
        "reviewed_by", sa.BigInteger(), nullable=True
    ))

    # Indexes for the inbox view
    op.create_index(
        "ix_scan_queue_user_status",
        "scan_queue",
        ["tenant_id", "user_id", "status"]
    )
    op.create_index(
        "ix_scan_queue_source",
        "scan_queue",
        ["tenant_id", "source"]
    )


def downgrade() -> None:
    op.drop_index("ix_scan_queue_source")
    op.drop_index("ix_scan_queue_user_status")
    op.drop_column("scan_queue", "reviewed_by")
    op.drop_column("scan_queue", "reviewed_at")
    op.drop_column("scan_queue", "source_url")
    op.drop_column("scan_queue", "ai_suggested_subfolder")
    op.drop_column("scan_queue", "ai_reasoning")
    op.drop_column("scan_queue", "ai_confidence")
    op.drop_column("scan_queue", "ai_suggested_matter_id")
    op.drop_column("scan_queue", "user_id")
