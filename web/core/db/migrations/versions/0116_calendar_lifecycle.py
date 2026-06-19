"""0116: calendar event lifecycle — change log, soft-delete, reasons, doc/task links.

Extends the provenance philosophy (§0 "no date is ever overwritten") to the
calendar surface. A calendar entry is never silently dropped: removing one leaves
it CROSSED OUT (lifecycle_state) so "what was supposed to happen but didn't" stays
visible, and every change is recorded in an append-only log with a guided reason.

  calendar_events (new columns):
    lifecycle_state      active | cancelled | rescheduled | removed | completed
                         | missed | deleted   (default 'active')
    lifecycle_reason_code/_note, lifecycle_changed_at/_by
    superseded_by_event_id  -- a reschedule points the old (crossed-out) row at
                               the new active row; the reconciler clusters both
    task_id              -- a task this event spawned (events link to their task)
    docs_status          pending | will_add_later | none | linked

  calendar_event_changes  -- append-only audit; survives a hard delete (event_id
                             ON DELETE SET NULL + a subject/date snapshot in detail)
  calendar_event_documents-- event <-> documents link (project_documents family)
  calendar_change_reasons -- seeded guided answers (tenant-overridable like entry_types)
"""
from alembic import op


revision = "0116_calendar_lifecycle"
down_revision = "0115_notice_extractions"
branch_labels = None
depends_on = None


# (code, label, result_state, applies_to, sort)
_REASONS = [
    ("cancelled",       "Cancelled",                     "cancelled",   "remove", 10),
    ("reset",           "Reset / Continued",             "rescheduled", "remove", 20),
    ("continued_court", "Continued by the court",        "rescheduled", "remove", 30),
    ("settled",         "Settled",                       "cancelled",   "remove", 40),
    ("mooted",          "Mooted / no longer needed",     "cancelled",   "remove", 50),
    ("combined",        "Combined with another setting", "removed",     "remove", 60),
    ("duplicate",       "Duplicate entry",               "removed",     "remove", 70),
    ("error",           "Entered in error",              "removed",     "remove", 80),
    ("completed",       "Completed / Held",              "completed",   "remove", 90),
    ("passed",          "Passed — did not occur",        "missed",      "remove", 100),
    ("other",           "Other (explain)",               "removed",     "remove", 110),
]


def upgrade():
    for col, ddl in [
        ("lifecycle_state", "varchar(16) NOT NULL DEFAULT 'active'"),
        ("lifecycle_reason_code", "varchar(32)"),
        ("lifecycle_reason_note", "text"),
        ("lifecycle_changed_at", "timestamptz"),
        ("lifecycle_changed_by", "bigint"),
        ("superseded_by_event_id", "uuid REFERENCES calendar_events(id) ON DELETE SET NULL"),
        ("task_id", "bigint REFERENCES tasks(id) ON DELETE SET NULL"),
        ("docs_status", "varchar(16) NOT NULL DEFAULT 'pending'"),
    ]:
        op.execute(f"ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS {col} {ddl}")
    op.execute("CREATE INDEX IF NOT EXISTS ix_calendar_events_lifecycle "
               "ON calendar_events (tenant_id, lifecycle_state)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS calendar_event_changes (
            id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       char(36)    NOT NULL,
            event_id        uuid        REFERENCES calendar_events(id) ON DELETE SET NULL,
            change_type     varchar(24) NOT NULL,
                -- created | updated | rescheduled | cancelled | removed
                -- | reinstated | deleted | doc_linked | docs_waived
                -- | task_created | matched
            reason_code     varchar(32),
            reason_note     text,
            from_start_at   timestamptz,
            to_start_at     timestamptz,
            actor_user_id   bigint,
            source          varchar(16) NOT NULL DEFAULT 'ui',  -- ui|caldav|reconciler|pipeline
            detail          jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- incl snapshot
            created_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_cal_changes_event "
               "ON calendar_event_changes (event_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_cal_changes_tenant "
               "ON calendar_event_changes (tenant_id, created_at)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS calendar_event_documents (
            id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       char(36)    NOT NULL,
            event_id        uuid        NOT NULL
                                REFERENCES calendar_events(id) ON DELETE CASCADE,
            document_id     uuid        NOT NULL,   -- documents.id (app DMS)
            role            varchar(40),
            added_by        bigint,
            created_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_cal_event_doc UNIQUE (event_id, document_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_cal_event_docs_event "
               "ON calendar_event_documents (event_id)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS calendar_change_reasons (
            id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     char(36),               -- NULL = platform default
            code          varchar(32) NOT NULL,
            label         varchar(120) NOT NULL,
            result_state  varchar(16) NOT NULL,   -- lifecycle_state this reason yields
            applies_to    varchar(16) NOT NULL DEFAULT 'remove',
            sort_order    integer     NOT NULL DEFAULT 0,
            is_active     boolean     NOT NULL DEFAULT true
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_cal_reason_code "
               "ON calendar_change_reasons (COALESCE(tenant_id,''), code)")
    for code, label, state, applies, sort in _REASONS:
        op.execute(
            "INSERT INTO calendar_change_reasons "
            "(tenant_id, code, label, result_state, applies_to, sort_order) "
            f"SELECT NULL, '{code}', '{label.replace(chr(39), chr(39)*2)}', "
            f"'{state}', '{applies}', {sort} "
            "WHERE NOT EXISTS (SELECT 1 FROM calendar_change_reasons "
            f"WHERE tenant_id IS NULL AND code='{code}')")

    # §2 typed-event -> workspace projector as DATA (a dispatch row, not code).
    # The calendar is the entry point: an event's type dispatches to the
    # workspace its underlying issue lives in.
    op.execute("""
        CREATE TABLE IF NOT EXISTS event_workspace_dispatch (
            id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     char(36),               -- NULL = platform default
            event_type    varchar(32) NOT NULL,
            workspace_kind varchar(32) NOT NULL,  -- hearing|meeting|deposition|court_dashboard|task|none
            label         varchar(80),
            auto_task     boolean     NOT NULL DEFAULT false,  -- spawn a backing task on create
            is_active     boolean     NOT NULL DEFAULT true
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_event_dispatch "
               "ON event_workspace_dispatch (COALESCE(tenant_id,''), event_type)")
    for et, kind, label, auto in [
        ("hearing", "hearing", "Hearing workspace", True),
        ("trial", "court_dashboard", "Trial / Court dashboard", True),
        ("deposition", "deposition", "Deposition prep", True),
        ("meeting", "meeting", "Meeting / conference", False),
        ("deadline", "task", "Task / deadline", True),
        ("admin", "task", "Task", False),
        ("personal", "task", "Task", False),
    ]:
        op.execute(
            "INSERT INTO event_workspace_dispatch "
            "(tenant_id, event_type, workspace_kind, label, auto_task) "
            f"SELECT NULL, '{et}', '{kind}', '{label}', {str(auto).lower()} "
            "WHERE NOT EXISTS (SELECT 1 FROM event_workspace_dispatch "
            f"WHERE tenant_id IS NULL AND event_type='{et}')")


def downgrade():
    op.execute("DROP TABLE IF EXISTS event_workspace_dispatch")
    op.execute("DROP TABLE IF EXISTS calendar_change_reasons")
    op.execute("DROP TABLE IF EXISTS calendar_event_documents")
    op.execute("DROP TABLE IF EXISTS calendar_event_changes")
    for col in ("docs_status", "task_id", "superseded_by_event_id",
                "lifecycle_changed_by", "lifecycle_changed_at",
                "lifecycle_reason_note", "lifecycle_reason_code", "lifecycle_state"):
        op.execute(f"ALTER TABLE calendar_events DROP COLUMN IF EXISTS {col}")
