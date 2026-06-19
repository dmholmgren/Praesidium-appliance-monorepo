"""trial presentation sessions + immutable event audit

Revision ID: 0134_trial_presentation
Revises: 0133_trial_discovery_tab
Create Date: 2026-06-18

Net-new tables for the Trial Presentation module (M14 TrialDesk).
Live routing state stays in-memory in the sibling relay
(core/services/trial_presentation_ws.py); these two tables persist the
session header and the immutable, self-writing display audit (the record).
Exhibit identity / status reuse trial_exhibits + trial_exhibit_state.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0134_trial_presentation"
down_revision = "0133_trial_discovery_tab"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "trial_presentation_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("proceeding_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("matter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("token", sa.Text, nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("exhibit_numbering_start", sa.Integer, nullable=False,
                  server_default="1"),
        sa.Column("created_by", sa.BigInteger, nullable=True),
        sa.Column("config", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_tps_tenant_matter", "trial_presentation_sessions",
                    ["tenant_id", "matter_id"])

    op.create_table(
        "trial_presentation_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("session_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("trial_presentation_sessions.id",
                                ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("exhibit_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("page", sa.Integer, nullable=True),
        sa.Column("target_roles", postgresql.ARRAY(sa.Text), nullable=True),
        sa.Column("actor_id", sa.BigInteger, nullable=True),
        sa.Column("actor_role", sa.Text, nullable=True),
        sa.Column("payload", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("occurred_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_tpe_session_time", "trial_presentation_events",
                    ["session_id", "occurred_at"])


def downgrade():
    op.drop_index("ix_tpe_session_time", table_name="trial_presentation_events")
    op.drop_table("trial_presentation_events")
    op.drop_index("ix_tps_tenant_matter", table_name="trial_presentation_sessions")
    op.drop_table("trial_presentation_sessions")
