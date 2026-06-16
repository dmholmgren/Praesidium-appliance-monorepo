"""0097: record_gaps -- appellate record completeness / omission detection (Scope v2 C-6).

Stores gaps found by comparing the parent trial matter's file against the
clerk-compiled CR (and brief-cited RR against the filed Reporter's Record):

  - gap_kind='cr_omission': a trial-court filing type present in the trial file but
    absent from the Clerk's Record -> prompt TRAP 34.5(c) supplemental clerk's record.
  - gap_kind='rr_gap': a Reporter's Record volume/page the brief relies on that is
    not in the filed record -> prompt TRAP 34.6 supplemental reporter's record.

Idempotent re-runs: the C-6 job deletes its prior open gaps for a case before
re-inserting (status dismissed/resolved rows are preserved).
"""
from alembic import op


revision = "0097_record_gaps"
down_revision = "0096_matter_links"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS record_gaps (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          varchar(64) NOT NULL,
            matter_id          uuid NOT NULL,
            appellate_case_id  uuid,
            gap_kind           varchar(16) NOT NULL,
            filing_type        varchar(48),
            severity           varchar(8) NOT NULL DEFAULT 'medium',
            title              text NOT NULL,
            detail             text,
            trial_refs         jsonb NOT NULL DEFAULT '[]'::jsonb,
            best_cr_match      text,
            best_sim           double precision,
            brief_reliance     boolean NOT NULL DEFAULT false,
            rule_cite          varchar(32),
            prompt             text,
            status             varchar(12) NOT NULL DEFAULT 'open',
            detected_run       text,
            detected_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_record_gaps_matter ON record_gaps (matter_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_record_gaps_case ON record_gaps (appellate_case_id, gap_kind, status)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS record_gaps")
