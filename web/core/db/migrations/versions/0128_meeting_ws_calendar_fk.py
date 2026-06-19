"""0128: repoint meeting_workspaces.calendar_event_id FK to calendar_events.

The meeting-workspace projection (G1) dispatches off the canonical calendar
layer `calendar_events` (the same table hearings/lifecycle/dispatch use), but
the FK still pointed at the legacy Exchange-sync staging table
`exchange_calendar_events`, blocking every projection. No meeting_workspaces
rows exist yet and no code depends on the exchange target, so we repoint the
constraint to `calendar_events(id)` and make it ON DELETE SET NULL (the link is
nullable; a superseded/deleted event should not block the workspace).
"""
from alembic import op

revision = "0128_meeting_ws_calendar_fk"
down_revision = "0127_trial_center_tabs"
branch_labels = None
depends_on = None

_FK = "meeting_workspaces_calendar_event_id_fkey"


def upgrade():
    op.execute(f"ALTER TABLE meeting_workspaces DROP CONSTRAINT IF EXISTS {_FK}")
    op.execute(
        f"ALTER TABLE meeting_workspaces ADD CONSTRAINT {_FK} "
        "FOREIGN KEY (calendar_event_id) REFERENCES calendar_events(id) ON DELETE SET NULL"
    )


def downgrade():
    op.execute(f"ALTER TABLE meeting_workspaces DROP CONSTRAINT IF EXISTS {_FK}")
    op.execute(
        f"ALTER TABLE meeting_workspaces ADD CONSTRAINT {_FK} "
        "FOREIGN KEY (calendar_event_id) REFERENCES exchange_calendar_events(id)"
    )
