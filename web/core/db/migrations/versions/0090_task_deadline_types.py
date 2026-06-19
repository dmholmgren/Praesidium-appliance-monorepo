"""0090: deadline-flavored task types.

Model decision (DMH, 2026-06-15): a deadline is NOT a separate entity — it's a
TASK with a deadline-flavored task_type and a due_date. Tasks already roll up to
the calendar (GET /calendar/tasks, /calendar/combined) and the calendar already
provisions tasks (POST /calendar/tasks). So we just give the task type registry
the deadline vocabulary. The legacy `deadlines` table stays read-only/legacy.

Adds platform-default `task` codes that weren't already seeded in 0085
(filing/hearing/discovery/deposition_prep/milestone already exist). More can be
added later with a single INSERT — the registry is data, no deploy required.
"""
from alembic import op


revision = "0090_task_deadline_types"
down_revision = "0089_trial_preservation"
branch_labels = None
depends_on = None


# (code, display_name, icon, color, display_order)
_SEED = [
    ("deadline", "Deadline", "⏰", "#DC2626", 200),
    ("sol",      "Statute of Limitations", "⏳", "#B91C1C", 210),
]

_ADDED = ["deadline", "sol"]


def _q(v):
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def upgrade():
    for (code, name, icon, color, order) in _SEED:
        op.execute(
            "INSERT INTO entry_types "
            "  (tenant_id, entity_kind, code, display_name, display_order, icon, color, is_active) "
            "VALUES (NULL, 'task', %s, %s, %s, %s, %s, true) "
            "ON CONFLICT (entity_kind, code, COALESCE(tenant_id, '__platform__')) DO NOTHING" %
            (_q(code), _q(name), order, _q(icon), _q(color)))


def downgrade():
    codes = ", ".join(_q(c) for c in _ADDED)
    op.execute(
        "DELETE FROM entry_types WHERE entity_kind = 'task' AND tenant_id IS NULL "
        "AND code IN (" + codes + ")")
