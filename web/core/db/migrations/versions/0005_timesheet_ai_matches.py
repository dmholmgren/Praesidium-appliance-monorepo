"""timesheet_ai_matches: AI reconciliation audit trail

Revision ID: 0005_timesheet_ai_matches
Revises: 0004_file_inventory
Create Date: 2026-05-05

Creates the audit table for AI-driven timesheet reconciliation (M9 AI Engine).

Architecture:
- Pass 1 (classifier): billable vs personal; results NOT stored here
- Pass 2 (matcher): full reasoning + tool calls stored here for billable drafts
- 90-day retention on reasoning_text via daily cron (separate component)

Notes:
- timesheet_drafts.id is TEXT (UUID-as-text), so FK is TEXT
- tenant_id is VARCHAR(36) and may have trailing spaces; always TRIM() in queries
- All timestamps are timezone-aware (asyncpg requirement)
- tool_calls is JSONB for queryable tool-use history
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0005_timesheet_ai_matches"
down_revision = "0004_file_inventory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # timesheet_ai_matches
    # One row per Pass-2 (matcher) Claude invocation against a billable draft.
    # Personal drafts (Pass 1 = personal) do NOT create rows here.
    # ------------------------------------------------------------------
    op.create_table(
        "timesheet_ai_matches",
        sa.Column(
            "id",
            sa.Text(),
            primary_key=True,
            server_default=sa.text("(uuid_generate_v4())::text"),
            nullable=False,
        ),
        # Tenant scoping (VARCHAR(36); always TRIM() in queries)
        sa.Column("tenant_id", sa.String(36), nullable=False),
        # Foreign keys -------------------------------------------------
        sa.Column(
            "draft_id",
            sa.Text(),
            sa.ForeignKey("timesheet_drafts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.Text(), nullable=False),
        # Match result -------------------------------------------------
        # Nullable: Claude may report "no confident match" and leave matter_id null
        sa.Column("matter_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("matter_name", sa.Text(), nullable=True),
        # 0.00 .. 1.00 ; matches the existing timesheet_drafts.ai_confidence range
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        # Reasoning text (subject to 90-day retention purge) ----------
        # When this column is purged, matter_id/confidence/model remain
        # so we can still audit "what was billed" without retaining
        # potentially sensitive content from ManicTime activity.
        sa.Column("reasoning_text", sa.Text(), nullable=True),
        # Full tool-use trace for reproducibility ---------------------
        # JSONB array: [{"tool": "...", "input": {...}, "output_summary": "..."}]
        # Also subject to 90-day purge.
        sa.Column(
            "tool_calls",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Model + invocation metadata (retained beyond 90 days) ------
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("source_label", sa.String(64), nullable=True),
        sa.Column("batch_id", sa.Text(), nullable=True),
        # Outcome flags ------------------------------------------------
        # 'matched'   = Claude returned a matter assignment
        # 'no_match'  = Claude reviewed but found no confident match
        # 'fallback'  = AI failed; rule ladder used instead
        # 'error'     = exception raised; row preserved for debugging
        sa.Column("outcome", sa.String(32), nullable=False, server_default="matched"),
        sa.Column("error_message", sa.Text(), nullable=True),
        # Retention tracking ------------------------------------------
        sa.Column(
            "reasoning_purged_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Set when 90-day cron blanks reasoning_text + tool_calls",
        ),
        # Audit fields -------------------------------------------------
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ------------------------------------------------------------------
    # Indexes
    # ------------------------------------------------------------------
    op.create_index(
        "ix_timesheet_ai_matches_tenant_session",
        "timesheet_ai_matches",
        ["tenant_id", "session_id"],
    )
    op.create_index(
        "ix_timesheet_ai_matches_draft_id",
        "timesheet_ai_matches",
        ["draft_id"],
    )
    op.create_index(
        "ix_timesheet_ai_matches_created_at",
        "timesheet_ai_matches",
        ["created_at"],
    )
    # Partial index for the retention cron: find rows older than 90 days
    # whose reasoning has not yet been purged. WHERE clause keeps it small.
    op.create_index(
        "ix_timesheet_ai_matches_retention",
        "timesheet_ai_matches",
        ["created_at"],
        postgresql_where=sa.text("reasoning_purged_at IS NULL"),
    )
    op.create_index(
        "ix_timesheet_ai_matches_outcome",
        "timesheet_ai_matches",
        ["outcome"],
    )

    # ------------------------------------------------------------------
    # Status convention for timesheet_drafts.status
    #
    # Existing values seen in production: 'pending', 'approved', 'edited',
    # 'rejected'. We add (by convention only — no CHECK constraint exists):
    #
    #   'personal'   = Pass 1 classifier marked as non-billable
    #   'ai_matched' = Pass 2 matcher assigned a matter with confidence
    #   'ai_review'  = Pass 2 matcher uncertain; human review required
    #
    # No DDL change needed; documented here for future reference.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Helper: backfill check
    # No backfill required — this is an audit table for go-forward writes.
    # ------------------------------------------------------------------


def downgrade() -> None:
    op.drop_index("ix_timesheet_ai_matches_outcome", table_name="timesheet_ai_matches")
    op.drop_index("ix_timesheet_ai_matches_retention", table_name="timesheet_ai_matches")
    op.drop_index("ix_timesheet_ai_matches_created_at", table_name="timesheet_ai_matches")
    op.drop_index("ix_timesheet_ai_matches_draft_id", table_name="timesheet_ai_matches")
    op.drop_index(
        "ix_timesheet_ai_matches_tenant_session",
        table_name="timesheet_ai_matches",
    )
    op.drop_table("timesheet_ai_matches")
