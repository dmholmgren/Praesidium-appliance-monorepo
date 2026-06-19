"""0117: forward-loop routing + alert inbox + motion workspaces (Court/Hearing §5).

"The information you need in one place, in a usable format" — automate to the
max, and where automation can't, raise an alert so nothing falls through.

  routing_alerts  — the triage inbox. An alert is raised whenever an incoming
    item (transcript / motion / response / notice) can't be auto-routed with
    confidence. Snooze-that-returns is the keystone: snoozing sets snoozed_until
    and the daily surface query brings it back when that passes; only an
    AFFIRMATIVE "does not need a workspace" (status='dismissed_no_action') stops
    it for good. Idempotent per (alert_type, source_kind, source_id).

  motion_workspaces — one per motion that will result in a hearing (the §2
    per-issue workspace), auto-populated from the classifier + reconciler; gaps
    (no hearing set, no response on file) become missing_filing alerts.
"""
from alembic import op


revision = "0117_routing_alerts"
down_revision = "0116_calendar_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS routing_alerts (
            id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       char(36)    NOT NULL,
            alert_type      varchar(32) NOT NULL,
                -- route_transcript | route_motion | route_response
                -- | unmatched_notice | missing_filing | needs_workspace | needs_routing
            title           text        NOT NULL,
            detail          jsonb       NOT NULL DEFAULT '{}'::jsonb,
            source_kind     varchar(24) NOT NULL,   -- document | hearing | calendar_event | motion_workspace
            source_id       text        NOT NULL,
            matter_id       uuid        REFERENCES matters(id) ON DELETE SET NULL,
            suggested       jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- {workspace_kind,target_id,confidence}
            severity        varchar(12) NOT NULL DEFAULT 'warning',
            status          varchar(24) NOT NULL DEFAULT 'open',
                -- open | snoozed | resolved | dismissed_no_action
            snoozed_until   date,
            snooze_count    integer     NOT NULL DEFAULT 0,
            resolution      varchar(40),            -- auto_routed | routed | dismissed | ...
            resolved_target text,
            resolved_by     bigint,
            resolved_at     timestamptz,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            last_surfaced_at timestamptz
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_routing_alert_src "
               "ON routing_alerts (tenant_id, alert_type, source_kind, source_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_routing_alert_status "
               "ON routing_alerts (tenant_id, status, snoozed_until)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_routing_alert_matter "
               "ON routing_alerts (tenant_id, matter_id)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS motion_workspaces (
            id                       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                char(36)    NOT NULL,
            matter_id                uuid        NOT NULL
                                         REFERENCES matters(id) ON DELETE CASCADE,
            title                    varchar(500),
            motion_document_id       uuid,       -- documents/dms id of the motion
            motion_type              varchar(64),-- classifier taxonomy code
            hearing_id               uuid        REFERENCES hearings(id) ON DELETE SET NULL,
            response_document_id     uuid,
            reply_document_id        uuid,
            proposed_order_document_id uuid,
            status                   varchar(24) NOT NULL DEFAULT 'open',
                -- open | hearing_set | submitted | ruled | closed
            outcome                  varchar(64),
            filed_at                 date,
            autopop                  jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- filled/missing audit
            created_at               timestamptz NOT NULL DEFAULT now(),
            updated_at               timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_motion_ws_doc UNIQUE (matter_id, motion_document_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_motion_ws_matter "
               "ON motion_workspaces (tenant_id, matter_id)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS motion_workspaces")
    op.execute("DROP TABLE IF EXISTS routing_alerts")
