"""0091: backfill legacy `deadlines` rows into `tasks` (deadlines-as-tasks).

Per the model decision in 0090: a deadline is just a task with a deadline-flavored
task_type + due_date. This one-time, idempotent, set-based backfill projects every
non-superseded legacy deadline into the unified `tasks` stream so old (document-
extracted) deadlines roll up to the calendar and the task lists like everything else.

Provenance: source='deadline_backfill', source_ref=<deadline id> (first-class, used
for the idempotency guard) plus the original deadline_type/category preserved in tags.
The legacy `deadlines` table is left intact; the /calendar/deadlines feed is patched
(separately) to exclude rows already backfilled so nothing shows twice.

task_type mapping: SOL -> 'sol'; a deadline_type that is already a valid task code
passes through; everything else (pleading/motion/expert/mediation/pretrial/trial/...)
-> 'deadline' (original kept in tags.orig_deadline_type). Add more task codes later
with one INSERT if you want them to pass through losslessly.

priority: SOL -> urgent; court-imposed -> high; else medium.
status: completed deadlines come over completed (with completed_at); else open.
"""
from alembic import op


revision = "0092_backfill_deadlines_to_tasks"
down_revision = "0091_issue_preservation"
branch_labels = None
depends_on = None

# task codes that already exist in the registry (0085 + 0090) — pass through as-is
_PASSTHROUGH = (
    "general", "drafting", "filing", "review", "research", "hearing",
    "milestone", "deposition_prep", "discovery", "deadline", "sol",
)


def upgrade():
    passthrough = ", ".join("'" + c + "'" for c in _PASSTHROUGH)
    op.execute(f"""
        INSERT INTO tasks
            (tenant_id, matter_id, title, due_date, task_type, priority, status,
             completed_at, notes, source, source_ref, tags, created_by,
             created_at, updated_at)
        SELECT
            TRIM(d.tenant_id),
            d.matter_id,
            d.title,
            d.deadline_date,
            CASE
                WHEN COALESCE(d.is_sol, false) THEN 'sol'
                WHEN d.deadline_type IN ({passthrough}) THEN d.deadline_type
                ELSE 'deadline'
            END,
            CASE
                WHEN COALESCE(d.is_sol, false) THEN 'urgent'
                WHEN COALESCE(d.is_court_imposed, true) THEN 'high'
                ELSE 'medium'
            END,
            CASE WHEN d.completed_at IS NOT NULL THEN 'completed' ELSE 'open' END,
            d.completed_at,
            d.notes,
            'deadline_backfill',
            d.id::text,
            jsonb_build_object(
                'backfill_deadline_id', d.id,
                'orig_deadline_type', d.deadline_type,
                'deadline_category', d.deadline_category,
                'is_court_imposed', COALESCE(d.is_court_imposed, true),
                'is_sol', COALESCE(d.is_sol, false)
            ),
            d.created_by,
            d.created_at,
            now()
        FROM deadlines d
        WHERE COALESCE(d.superseded, false) = false
          AND NOT EXISTS (
              SELECT 1 FROM tasks t
              WHERE t.source = 'deadline_backfill'
                AND t.source_ref = d.id::text
                AND TRIM(t.tenant_id) = TRIM(d.tenant_id)
          )
    """)


def downgrade():
    op.execute("DELETE FROM tasks WHERE source = 'deadline_backfill'")
