"""0029 widget compositor

Revision ID: 0029_widget_compositor
Revises: 0028_deal_room_tables
"""
from alembic import op
from sqlalchemy import text

revision = "0029_widget_compositor"
down_revision = "0028_deal_room_tables"

def upgrade():
    conn = op.get_bind()

    # 1. Tables
    conn.execute(text("""
CREATE TABLE IF NOT EXISTS user_widget_layouts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       CHAR(40) NOT NULL,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    context_type    VARCHAR(50) NOT NULL,
    context_id      UUID,
    widget_layout   JSONB NOT NULL DEFAULT '[]',
    layout_version  INTEGER NOT NULL DEFAULT 1,
    is_default      BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_user_widget_layout UNIQUE (tenant_id, user_id, context_type, context_id)
)
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_uwl_tenant_user ON user_widget_layouts (tenant_id, user_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_uwl_context ON user_widget_layouts (context_type, context_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_uwl_default ON user_widget_layouts (tenant_id, context_type) WHERE is_default = true"))

    conn.execute(text("""
CREATE TABLE IF NOT EXISTS widget_data_sources (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       CHAR(40),
    source_key      VARCHAR(120) NOT NULL,
    api_endpoint    VARCHAR(500) NOT NULL,
    http_method     VARCHAR(10) NOT NULL DEFAULT 'GET',
    default_params  JSONB DEFAULT '{}',
    cache_ttl_sec   INTEGER DEFAULT 60,
    requires_context VARCHAR(200),
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_widget_data_source UNIQUE (source_key, tenant_id)
)
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_wds_source_key ON widget_data_sources (source_key)"))

    # 2. ALTER widget_registry
    for col in [
        "component_path VARCHAR(200)",
        "default_props JSONB DEFAULT '{}'",
        "lazy_load BOOLEAN NOT NULL DEFAULT true",
        "min_size VARCHAR(20) DEFAULT 'small'",
        "max_size VARCHAR(20) DEFAULT 'full'",
        "icon VARCHAR(10)",
        "sort_order INTEGER DEFAULT 0",
    ]:
        conn.execute(text(f"ALTER TABLE widget_registry ADD COLUMN IF NOT EXISTS {col}"))

    # 3. ALTER workspace_templates
    for col in [
        "layout_schema VARCHAR(50) DEFAULT 'freeform'",
        "icon VARCHAR(10)",
        "sort_order INTEGER DEFAULT 0",
    ]:
        conn.execute(text(f"ALTER TABLE workspace_templates ADD COLUMN IF NOT EXISTS {col}"))

    # 4. Populate component_path — use bind params to avoid JSON parse issues
    widget_updates = [
        ("ws_whiteboard", "widgets/WhiteboardCanvas", "{}", "\U0001f3a8", 1),
        ("ws_notes", "widgets/NotesEditor", '{"icon":"\\ud83d\\udcdd","title":"Notes"}', "\U0001f4dd", 2),
        ("ws_agenda", "widgets/NotesEditor", '{"icon":"\\ud83d\\udccb","title":"Agenda","placeholder":"# Agenda"}', "\U0001f4cb", 3),
        ("ws_checklist", "widgets/ChecklistWidget", '{"icon":"\\u2611\\ufe0f","title":"Checklist"}', "\u2611\ufe0f", 4),
        ("ws_action_items", "widgets/ChecklistWidget", '{"icon":"\\u26a1","title":"Action Items","showAssignee":true,"showDue":true}', "\u26a1", 5),
        ("ws_timer", "widgets/SessionTimer", "{}", "\u23f1", 6),
        ("ws_document_panel", "widgets/DocumentPanel", "{}", "\U0001f4c4", 7),
        ("ws_participant_list", "widgets/ParticipantPanel", "{}", "\U0001f465", 8),
    ]
    for slug, cpath, dprops, icon, sorder in widget_updates:
        conn.execute(text(
            "UPDATE widget_registry SET component_path = :cp, default_props = CAST(:dp AS jsonb), icon = :ic, sort_order = :so WHERE widget_slug = :sl"
        ), {"cp": cpath, "dp": dprops, "ic": icon, "so": sorder, "sl": slug})

    # Widgets without default_props
    extra_widgets = [
        ("ws_video_pip", "widgets/VideoPIP", "\U0001f4f9", 9),
        ("ws_transcript_live", "widgets/LiveTranscript", "\U0001f399\ufe0f", 10),
        ("ws_exhibit_presenter", "widgets/ExhibitPresenter", "\U0001f4d1", 11),
        ("ws_ai_assistant", "widgets/AIMeetingAssistant", "\U0001f916", 12),
    ]
    for slug, cpath, icon, sorder in extra_widgets:
        conn.execute(text(
            "UPDATE widget_registry SET component_path = :cp, icon = :ic, sort_order = :so WHERE widget_slug = :sl"
        ), {"cp": cpath, "ic": icon, "so": sorder, "sl": slug})

    # 5. Template updates
    tmpl_updates = [
        ("meeting_standard", "freeform", "\U0001f4c5", 1),
        ("deposition", "three-panel", "\u2696\ufe0f", 2),
        ("hearing", "three-panel", "\U0001f3db\ufe0f", 3),
        ("case_strategy", "freeform", "\U0001f9e0", 4),
        ("deal_room", "three-panel", "\U0001f91d", 5),
        ("client_meeting", "freeform", "\U0001f465", 6),
        ("conference_room", "full-screen", "\U0001f5a5\ufe0f", 7),
        ("notebook_only", "tabbed", "\U0001f4d3", 8),
    ]
    for slug, schema, icon, sorder in tmpl_updates:
        conn.execute(text(
            "UPDATE workspace_templates SET layout_schema = :ls, icon = :ic, sort_order = :so WHERE template_slug = :sl"
        ), {"ls": schema, "ic": icon, "so": sorder, "sl": slug})

    # 6. Seed data sources
    sources = [
        ("modules.billing.services.widget_service.get_billing_revenue_chart", "/api/v1/dashboard/pi?tab_slug=firm_view", None, 120),
        ("modules.billing.services.billing_service.get_ar_aging", "/api/v1/billing/widgets/ar-aging", "tenant_id", 120),
        ("modules.billing.services.billing_service.get_wip_summary", "/api/v1/billing/widgets/wip-summary", "tenant_id", 120),
        ("modules.billing.services.billing_service.get_matter_financials", "/api/v1/billing/widgets/matter-financials", "matter_id", 60),
        ("modules.dashboard.services.pi_widget_service.get_atty_calendar", "/api/v1/dashboard/pi?tab_slug=my_view", "user_id", 60),
        ("modules.calendar.services.deadline_service.get_upcoming_deadlines", "/api/v1/calendar/deadlines/upcoming", "user_id", 60),
        ("modules.calendar.services.task_service.get_open_tasks", "/api/v1/calendar/tasks/open", "user_id", 60),
    ]
    for sk, ep, rc, ttl in sources:
        conn.execute(text(
            "INSERT INTO widget_data_sources (source_key, api_endpoint, requires_context, cache_ttl_sec) VALUES (:sk, :ep, :rc, :ttl) ON CONFLICT DO NOTHING"
        ), {"sk": sk, "ep": ep, "rc": rc, "ttl": ttl})


def downgrade():
    op.execute("DROP TABLE IF EXISTS widget_data_sources CASCADE")
    op.execute("DROP TABLE IF EXISTS user_widget_layouts CASCADE")
    for col in ["component_path","default_props","lazy_load","min_size","max_size","icon","sort_order"]:
        op.execute(f"ALTER TABLE widget_registry DROP COLUMN IF EXISTS {col}")
    for col in ["layout_schema","icon","sort_order"]:
        op.execute(f"ALTER TABLE workspace_templates DROP COLUMN IF EXISTS {col}")
