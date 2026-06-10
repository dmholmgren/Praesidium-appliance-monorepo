"""0064: stage ledger becomes a claimable work queue (ledger-as-queue DAG).

Adds what the SKIP LOCKED claim loop needs:
  - priority column (lower = sooner; default 100)
  - partial index on the pending predicate (the claim scan)
  - partial index on running/started_at (the stale-claim reaper)
  - aggressive per-table autovacuum + fillfactor 85 (high-churn queue rows
    flip state constantly; HOT updates + early vacuum keep bloat down)

All idempotent: IF NOT EXISTS / reloptions are safe to re-apply.
"""
from alembic import op

revision = "0064_stage_ledger_queue"
down_revision = "0063_layout_token_block_line"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE ediscovery_stage_status "
        "ADD COLUMN IF NOT EXISTS priority integer NOT NULL DEFAULT 100"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_stage_claim "
        "ON ediscovery_stage_status (stage, priority, updated_at) "
        "WHERE state = 'pending'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_stage_reap "
        "ON ediscovery_stage_status (started_at) "
        "WHERE state = 'running'"
    )
    op.execute(
        "ALTER TABLE ediscovery_stage_status SET ("
        "autovacuum_vacuum_scale_factor = 0.01, "
        "autovacuum_vacuum_cost_delay = 0, "
        "autovacuum_analyze_scale_factor = 0.02, "
        "fillfactor = 85)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_stage_reap")
    op.execute("DROP INDEX IF EXISTS ix_stage_claim")
    op.execute("ALTER TABLE ediscovery_stage_status DROP COLUMN IF EXISTS priority")
    op.execute(
        "ALTER TABLE ediscovery_stage_status RESET ("
        "autovacuum_vacuum_scale_factor, autovacuum_vacuum_cost_delay, "
        "autovacuum_analyze_scale_factor, fillfactor)"
    )
