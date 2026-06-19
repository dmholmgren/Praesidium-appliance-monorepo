"""0091: issue_preservation -- Module B / Unit 8, the preservation check.

For each Issue Presented in the brief, trace to the trial-court objection + ruling
in the Reporter's Record (via Module A's trial_preservation index, U3) and grade
whether the complaint was preserved for appeal under Tex. R. App. P. 33.1 (and
Tex. R. Evid. 103 for excluded evidence). The single most expensive appellate
mistake -- briefing an unpreserved issue -- caught before filing.

One row per issue: the classified complaint type, the matched objection/ruling
loci, and a status in {preserved, unpreserved, fundamental_error_only,
no_objection_required, unclear} with a record-cited rationale.

(Re-chained from 0090 onto 0090_task_deadline_types, a parallel branch that landed
mid-session; mirrors the U7 re-chain.)
"""
from alembic import op


revision = "0091_issue_preservation"
down_revision = "0090_task_deadline_types"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS issue_preservation (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         char(36) NOT NULL,
            matter_id         uuid,
            appellate_case_id uuid REFERENCES appellate_cases(id) ON DELETE CASCADE,
            trial_id          uuid,
            issue_index       integer NOT NULL,
            issue_text        text,
            complaint_type    varchar(24),   -- admission|exclusion|legal_sufficiency|factual_sufficiency|charge|legal_ruling|other
            status            varchar(28),   -- preserved|unpreserved|fundamental_error_only|no_objection_required|unclear
            needs_offer_of_proof boolean NOT NULL DEFAULT false,
            rationale         text,
            matched           jsonb,         -- [{preservation_id, locus, ruling, ruling_locus, grounds, similarity}]
            best_similarity   numeric,
            source            varchar(15) NOT NULL DEFAULT 'auto',
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_issue_preservation_idx "
               "ON issue_preservation (appellate_case_id, issue_index)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_issue_preservation_status "
               "ON issue_preservation (appellate_case_id, status)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS issue_preservation")
