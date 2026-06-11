"""contact_dedup_candidates: hygiene + duplicate-review audit table

Revision ID: 0007_contact_hygiene_aux
Revises: 0006_drafts_status_ai_states
Create Date: 2026-05-05

Adds the auxiliary table the contact-hygiene job (Phase A1) writes to when
it detects probable duplicate contacts. NEVER auto-merges — every candidate
goes through human review.

DESIGN:
- One row per detected duplicate cluster (NOT one row per pair). A cluster
  with 4 'Bhadresh Trivedi' records produces ONE candidate row pointing to
  ALL 4 contact ids. This avoids the O(n^2) explosion of pairwise rows
  and matches the way humans review duplicates ("here are 4 records of
  the same person; which to keep?").
- contact_ids stored as JSONB array of bigints to avoid a junction table
  for what is fundamentally an audit/review queue.
- signal_type tracks WHY the cluster was flagged: 'exact_phone',
  'exact_email', 'fuzzy_name', 'fuzzy_name+exact_phone', etc.
- review_outcome captures human disposition: 'merged', 'kept_separate',
  'rejected_false_positive', or NULL while pending.
- canonical_contact_id: when reviewer chooses 'merged', records WHICH
  record of the cluster became canonical. The hygiene job will then
  re-point matter_contacts and email_routing_queue references to it.

The actual merge action is NOT implemented in this migration; only the
review queue infrastructure. Merge execution is Phase C UI work.

This table is per-tenant. tenant_id is the standard varchar(36) trim-prone
column; queries must always TRIM().
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0007_contact_hygiene_aux"
down_revision = "0006_drafts_status_ai_states"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contact_dedup_candidates",
        sa.Column(
            "id",
            sa.Text(),
            primary_key=True,
            server_default=sa.text("(uuid_generate_v4())::text"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(36), nullable=False),

        # Cluster of contact ids that look like duplicates.
        # Stored as JSONB array of bigints. Length always >= 2.
        sa.Column(
            "contact_ids",
            sa.dialects.postgresql.JSONB,
            nullable=False,
        ),

        # Signal that triggered detection. Loose vocabulary, not a CHECK,
        # so the hygiene job can introduce new signals without DDL.
        # Examples: 'exact_phone', 'exact_email', 'fuzzy_name',
        #           'fuzzy_name+exact_phone'
        sa.Column("signal_type", sa.String(64), nullable=False),

        # Confidence score 0.000-1.000.
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),

        # Free-text explanation of what was matched. Auditing aid only.
        sa.Column("notes", sa.Text(), nullable=True),

        # The "winning" record of a cluster, if a reviewer chose to merge.
        sa.Column("canonical_contact_id", sa.BigInteger(), nullable=True),

        # Disposition: 'merged' | 'kept_separate' | 'rejected_false_positive'
        # NULL = pending review.
        sa.Column("review_outcome", sa.String(32), nullable=True),

        # Reviewer audit (nullable until reviewed)
        sa.Column("reviewed_by", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),

        # Detection audit
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "hygiene_run_id",
            sa.Text(),
            nullable=True,
            comment="Set to a stable id per hygiene-job run for grouping",
        ),
    )

    # ---------------- Indexes ----------------
    op.create_index(
        "ix_contact_dedup_candidates_tenant_pending",
        "contact_dedup_candidates",
        ["tenant_id"],
        postgresql_where=sa.text("review_outcome IS NULL"),
    )
    op.create_index(
        "ix_contact_dedup_candidates_signal",
        "contact_dedup_candidates",
        ["tenant_id", "signal_type"],
    )
    op.create_index(
        "ix_contact_dedup_candidates_run",
        "contact_dedup_candidates",
        ["hygiene_run_id"],
    )
    # GIN index on contact_ids array so we can quickly find candidates
    # involving a given contact (for "is this contact in any pending merge?")
    op.create_index(
        "ix_contact_dedup_candidates_ids_gin",
        "contact_dedup_candidates",
        ["contact_ids"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_contact_dedup_candidates_ids_gin",
        table_name="contact_dedup_candidates",
    )
    op.drop_index(
        "ix_contact_dedup_candidates_run",
        table_name="contact_dedup_candidates",
    )
    op.drop_index(
        "ix_contact_dedup_candidates_signal",
        table_name="contact_dedup_candidates",
    )
    op.drop_index(
        "ix_contact_dedup_candidates_tenant_pending",
        table_name="contact_dedup_candidates",
    )
    op.drop_table("contact_dedup_candidates")
