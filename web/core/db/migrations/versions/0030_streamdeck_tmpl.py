"""0030 streamdeck templates

Revision ID: 0030_streamdeck_tmpl
Revises: 0029_widget_compositor
"""
from alembic import op
from sqlalchemy import text
import json

revision = "0030_streamdeck_tmpl"
down_revision = "0029_widget_compositor"


# ── eDiscovery Review MK.2 15-key ────────────────────────────────────────────

EDISCOVERY_REVIEW_MK2 = [
    # Row 1 — Review coding (positions 0-4)
    {"position": 0, "action_type": "api_call", "label": "Responsive",
     "action_payload": {"method": "POST", "path": "/api/v1/ediscovery/review/doc/{doc_id}/status", "body": {"review_status": "responsive"}},
     "color_active": "#22C55E", "color_inactive": "#1A3D2E", "color_bg": "#0F1A14",
     "state_binding": "context.review_status == responsive", "icon": "check-circle",
     "auto_advance": True},
    {"position": 1, "action_type": "api_call", "label": "Not Resp.",
     "action_payload": {"method": "POST", "path": "/api/v1/ediscovery/review/doc/{doc_id}/status", "body": {"review_status": "not_responsive"}},
     "color_active": "#EF4444", "color_inactive": "#3D1A1A", "color_bg": "#1A0F0F",
     "state_binding": "context.review_status == not_responsive", "icon": "x-circle",
     "auto_advance": True},
    {"position": 2, "action_type": "api_call", "label": "Needs Rev.",
     "action_payload": {"method": "POST", "path": "/api/v1/ediscovery/review/doc/{doc_id}/status", "body": {"review_status": "needs_review"}},
     "color_active": "#F59E0B", "color_inactive": "#3D2E0A", "color_bg": "#1A150A",
     "state_binding": "context.review_status == needs_review", "icon": "alert-circle",
     "auto_advance": True},
    {"position": 3, "action_type": "api_call", "label": "Hot Doc",
     "action_payload": {"method": "POST", "path": "/api/v1/ediscovery/review/doc/{doc_id}/status", "body": {"review_status": "hot"}},
     "color_active": "#F97316", "color_inactive": "#3D2510", "color_bg": "#1A1008",
     "state_binding": "context.review_status == hot", "icon": "flame",
     "auto_advance": True},
    {"position": 4, "action_type": "api_call", "label": "Skip",
     "action_payload": {"method": "POST", "path": "/api/v1/streamdeck/review-advance", "body": {"skip": True}},
     "color_active": "#6B7280", "color_inactive": "#374151", "color_bg": "#111827",
     "state_binding": None, "icon": "skip-forward",
     "auto_advance": False},

    # Row 2 — Privilege + navigation (positions 5-9)
    {"position": 5, "action_type": "api_call", "label": "Privileged",
     "action_payload": {"method": "POST", "path": "/api/v1/ediscovery/review/doc/{doc_id}/status", "body": {"privilege_status": "privileged"}},
     "color_active": "#3B82F6", "color_inactive": "#1E3A5F", "color_bg": "#0F1D30",
     "state_binding": "context.privilege_status == privileged", "icon": "shield"},
    {"position": 6, "action_type": "api_call", "label": "Work Prod.",
     "action_payload": {"method": "POST", "path": "/api/v1/ediscovery/review/doc/{doc_id}/status", "body": {"privilege_status": "work_product"}},
     "color_active": "#6366F1", "color_inactive": "#2D2F6B", "color_bg": "#151636",
     "state_binding": "context.privilege_status == work_product", "icon": "briefcase"},
    {"position": 7, "action_type": "api_call", "label": "Not Priv.",
     "action_payload": {"method": "POST", "path": "/api/v1/ediscovery/review/doc/{doc_id}/status", "body": {"privilege_status": "not_privileged"}},
     "color_active": "#9CA3AF", "color_inactive": "#374151", "color_bg": "#111827",
     "state_binding": "context.privilege_status == not_privileged", "icon": "unlock"},
    {"position": 8, "action_type": "api_call", "label": "\u2190 Prev",
     "action_payload": {"method": "POST", "path": "/api/v1/streamdeck/review-advance", "body": {"direction": "prev"}},
     "color_active": "#374151", "color_inactive": "#1F2937", "color_bg": "#111827",
     "state_binding": None, "icon": "arrow-left"},
    {"position": 9, "action_type": "api_call", "label": "Next \u2192",
     "action_payload": {"method": "POST", "path": "/api/v1/streamdeck/review-advance", "body": {"direction": "next"}},
     "color_active": "#374151", "color_inactive": "#1F2937", "color_bg": "#111827",
     "state_binding": None, "icon": "arrow-right"},

    # Row 3 — Tags + context (positions 10-14)
    {"position": 10, "action_type": "toggle", "label": "{tag_1_name}",
     "action_payload": {"method": "POST", "path": "/api/v1/ediscovery/review/doc/{doc_id}/tag", "body_on": {"tag_id": "{pinned_tag_1}", "action": "add"}, "body_off": {"tag_id": "{pinned_tag_1}", "action": "remove"}},
     "color_active": "#8B5CF6", "color_inactive": "#2D1F5E", "color_bg": "#150F2E",
     "state_binding": "context.tags includes pinned_tag_1", "icon": "tag"},
    {"position": 11, "action_type": "toggle", "label": "{tag_2_name}",
     "action_payload": {"method": "POST", "path": "/api/v1/ediscovery/review/doc/{doc_id}/tag", "body_on": {"tag_id": "{pinned_tag_2}", "action": "add"}, "body_off": {"tag_id": "{pinned_tag_2}", "action": "remove"}},
     "color_active": "#8B5CF6", "color_inactive": "#2D1F5E", "color_bg": "#150F2E",
     "state_binding": "context.tags includes pinned_tag_2", "icon": "tag"},
    {"position": 12, "action_type": "toggle", "label": "{tag_3_name}",
     "action_payload": {"method": "POST", "path": "/api/v1/ediscovery/review/doc/{doc_id}/tag", "body_on": {"tag_id": "{pinned_tag_3}", "action": "add"}, "body_off": {"tag_id": "{pinned_tag_3}", "action": "remove"}},
     "color_active": "#8B5CF6", "color_inactive": "#2D1F5E", "color_bg": "#150F2E",
     "state_binding": "context.tags includes pinned_tag_3", "icon": "tag"},
    {"position": 13, "action_type": "display", "label": "{doc_pos}/{doc_total}",
     "action_payload": None,
     "color_active": "#1F2937", "color_inactive": "#1F2937", "color_bg": "#111827",
     "state_binding": None, "icon": "hash"},
    {"position": 14, "action_type": "navigate", "label": "{matter_short}",
     "action_payload": {"url": "/matters/{matter_id}"},
     "color_active": "#1B3A5C", "color_inactive": "#1B3A5C", "color_bg": "#0D1F2F",
     "state_binding": None, "icon": "briefcase"},
]


# ── Billing MK.2 15-key ──────────────────────────────────────────────────────

BILLING_MK2 = [
    # Row 1 — Timer + matter
    {"position": 0, "action_type": "toggle", "label": "{timer_label}",
     "action_payload": {"method": "POST", "path": "/api/v1/billing/timer/toggle", "body_on": {"matter_id": "{matter_id}"}, "body_off": {}},
     "color_active": "#22C55E", "color_inactive": "#EF4444", "color_bg": "#111827",
     "state_binding": "context.timer_active", "icon": "clock"},
    {"position": 1, "action_type": "display", "label": "{elapsed}",
     "action_payload": None, "color_active": "#1F2937", "color_inactive": "#1F2937", "color_bg": "#111827",
     "state_binding": None, "icon": "timer"},
    {"position": 2, "action_type": "display", "label": "{matter_short}",
     "action_payload": None, "color_active": "#1B3A5C", "color_inactive": "#1B3A5C", "color_bg": "#0D1F2F",
     "state_binding": None, "icon": "briefcase"},
    {"position": 3, "action_type": "folder", "label": "Matters",
     "action_payload": {"children_source": "recent_matters"},
     "color_active": "#374151", "color_inactive": "#1F2937", "color_bg": "#111827",
     "state_binding": None, "icon": "layers"},
    {"position": 4, "action_type": "navigate", "label": "New Slip",
     "action_payload": {"url": "/billing/clients/{client_id}/matters/{matter_id}#new-entry"},
     "color_active": "#F59E0B", "color_inactive": "#3D2E0A", "color_bg": "#1A150A",
     "state_binding": None, "icon": "plus-circle"},

    # Row 2 — Activity type quick-start
    {"position": 5, "action_type": "api_call", "label": "Phone",
     "action_payload": {"method": "POST", "path": "/api/v1/billing/timer/start", "body": {"matter_id": "{matter_id}", "activity_type": "phone_call"}},
     "color_active": "#22C55E", "color_inactive": "#374151", "color_bg": "#111827",
     "state_binding": "context.timer_activity == phone_call", "icon": "phone"},
    {"position": 6, "action_type": "api_call", "label": "Research",
     "action_payload": {"method": "POST", "path": "/api/v1/billing/timer/start", "body": {"matter_id": "{matter_id}", "activity_type": "research"}},
     "color_active": "#22C55E", "color_inactive": "#374151", "color_bg": "#111827",
     "state_binding": "context.timer_activity == research", "icon": "search"},
    {"position": 7, "action_type": "api_call", "label": "Draft",
     "action_payload": {"method": "POST", "path": "/api/v1/billing/timer/start", "body": {"matter_id": "{matter_id}", "activity_type": "drafting"}},
     "color_active": "#22C55E", "color_inactive": "#374151", "color_bg": "#111827",
     "state_binding": "context.timer_activity == drafting", "icon": "edit-3"},
    {"position": 8, "action_type": "api_call", "label": "Conference",
     "action_payload": {"method": "POST", "path": "/api/v1/billing/timer/start", "body": {"matter_id": "{matter_id}", "activity_type": "conference"}},
     "color_active": "#22C55E", "color_inactive": "#374151", "color_bg": "#111827",
     "state_binding": "context.timer_activity == conference", "icon": "users"},
    {"position": 9, "action_type": "api_call", "label": "Court",
     "action_payload": {"method": "POST", "path": "/api/v1/billing/timer/start", "body": {"matter_id": "{matter_id}", "activity_type": "court_appearance"}},
     "color_active": "#22C55E", "color_inactive": "#374151", "color_bg": "#111827",
     "state_binding": "context.timer_activity == court_appearance", "icon": "scale"},

    # Row 3 — Status + navigation
    {"position": 10, "action_type": "api_call", "label": "Finalize",
     "action_payload": {"method": "POST", "path": "/api/v1/billing/bill-runs/{matter_id}/finalize", "body": {}},
     "color_active": "#F59E0B", "color_inactive": "#374151", "color_bg": "#111827",
     "state_binding": None, "icon": "check-square", "confirmation": True},
    {"position": 11, "action_type": "display", "label": "WIP ${wip_total}",
     "action_payload": None, "color_active": "#1F2937", "color_inactive": "#1F2937", "color_bg": "#111827",
     "state_binding": None, "icon": "dollar-sign"},
    {"position": 12, "action_type": "navigate", "label": "Inbox ({inbox_unread})",
     "action_payload": {"url": "/dashboard#email"},
     "color_active": "#3B82F6", "color_inactive": "#1E3A5F", "color_bg": "#0F1D30",
     "state_binding": None, "icon": "mail"},
    {"position": 13, "action_type": "navigate", "label": "Dashboard",
     "action_payload": {"url": "/dashboard"},
     "color_active": "#1B3A5C", "color_inactive": "#1B3A5C", "color_bg": "#0D1F2F",
     "state_binding": None, "icon": "home"},
    {"position": 14, "action_type": "navigate", "label": "Settings",
     "action_payload": {"url": "/admin/streamdeck"},
     "color_active": "#374151", "color_inactive": "#374151", "color_bg": "#111827",
     "state_binding": None, "icon": "settings"},
]


# ── Global / Idle MK.2 ───────────────────────────────────────────────────────

GLOBAL_IDLE_MK2 = [
    {"position": 0, "action_type": "toggle", "label": "{timer_label}",
     "action_payload": {"method": "POST", "path": "/api/v1/billing/timer/toggle", "body_on": {"matter_id": "{matter_id}"}, "body_off": {}},
     "color_active": "#22C55E", "color_inactive": "#EF4444", "color_bg": "#111827",
     "state_binding": "context.timer_active", "icon": "clock"},
    {"position": 1, "action_type": "display", "label": "{matter_short}",
     "action_payload": None, "color_active": "#1B3A5C", "color_inactive": "#1B3A5C", "color_bg": "#0D1F2F",
     "state_binding": None, "icon": "briefcase"},
    {"position": 2, "action_type": "navigate", "label": "Inbox ({inbox_unread})",
     "action_payload": {"url": "/dashboard#email"},
     "color_active": "#3B82F6", "color_inactive": "#1E3A5F", "color_bg": "#0F1D30",
     "state_binding": None, "icon": "mail"},
    {"position": 3, "action_type": "navigate", "label": "Calendar",
     "action_payload": {"url": "/calendar"},
     "color_active": "#374151", "color_inactive": "#374151", "color_bg": "#111827",
     "state_binding": None, "icon": "calendar"},
    {"position": 4, "action_type": "navigate", "label": "Matters",
     "action_payload": {"url": "/matters"},
     "color_active": "#374151", "color_inactive": "#374151", "color_bg": "#111827",
     "state_binding": None, "icon": "search"},
    # Positions 5-9: recent matters (populated dynamically)
    {"position": 5, "action_type": "navigate", "label": "{recent_1_short}", "action_payload": {"url": "/matters/{recent_1_id}"}, "color_active": "#1F2937", "color_inactive": "#1F2937", "color_bg": "#111827", "state_binding": None, "icon": "folder"},
    {"position": 6, "action_type": "navigate", "label": "{recent_2_short}", "action_payload": {"url": "/matters/{recent_2_id}"}, "color_active": "#1F2937", "color_inactive": "#1F2937", "color_bg": "#111827", "state_binding": None, "icon": "folder"},
    {"position": 7, "action_type": "navigate", "label": "{recent_3_short}", "action_payload": {"url": "/matters/{recent_3_id}"}, "color_active": "#1F2937", "color_inactive": "#1F2937", "color_bg": "#111827", "state_binding": None, "icon": "folder"},
    {"position": 8, "action_type": "navigate", "label": "{recent_4_short}", "action_payload": {"url": "/matters/{recent_4_id}"}, "color_active": "#1F2937", "color_inactive": "#1F2937", "color_bg": "#111827", "state_binding": None, "icon": "folder"},
    {"position": 9, "action_type": "navigate", "label": "{recent_5_short}", "action_payload": {"url": "/matters/{recent_5_id}"}, "color_active": "#1F2937", "color_inactive": "#1F2937", "color_bg": "#111827", "state_binding": None, "icon": "folder"},
    # Row 3
    {"position": 10, "action_type": "navigate", "label": "Dashboard",
     "action_payload": {"url": "/dashboard"},
     "color_active": "#1B3A5C", "color_inactive": "#1B3A5C", "color_bg": "#0D1F2F",
     "state_binding": None, "icon": "home"},
    {"position": 11, "action_type": "navigate", "label": "Billing",
     "action_payload": {"url": "/billing/"},
     "color_active": "#374151", "color_inactive": "#374151", "color_bg": "#111827",
     "state_binding": None, "icon": "dollar-sign"},
    {"position": 12, "action_type": "navigate", "label": "Documents",
     "action_payload": {"url": "/dms/"},
     "color_active": "#374151", "color_inactive": "#374151", "color_bg": "#111827",
     "state_binding": None, "icon": "file-text"},
    {"position": 13, "action_type": "navigate", "label": "eDiscovery",
     "action_payload": {"url": "/ediscovery/"},
     "color_active": "#374151", "color_inactive": "#374151", "color_bg": "#111827",
     "state_binding": None, "icon": "database"},
    {"position": 14, "action_type": "navigate", "label": "Settings",
     "action_payload": {"url": "/admin/streamdeck"},
     "color_active": "#374151", "color_inactive": "#374151", "color_bg": "#111827",
     "state_binding": None, "icon": "settings"},
]


def upgrade():
    conn = op.get_bind()

    # ── 1. streamdeck_templates ──────────────────────────────────────────────

    conn.execute(text("""
CREATE TABLE IF NOT EXISTS streamdeck_templates (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       CHAR(40),
    template_slug   VARCHAR(100) NOT NULL,
    display_name    VARCHAR(200) NOT NULL,
    context_type    VARCHAR(50) NOT NULL,
    device_type     VARCHAR(30) NOT NULL DEFAULT 'mk2_15',
    grid_rows       SMALLINT NOT NULL DEFAULT 3,
    grid_cols       SMALLINT NOT NULL DEFAULT 5,
    buttons         JSONB NOT NULL DEFAULT '[]',
    is_default      BOOLEAN NOT NULL DEFAULT false,
    created_by      INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_sd_template UNIQUE (tenant_id, template_slug)
)
    """))

    conn.execute(text("""
CREATE INDEX IF NOT EXISTS idx_sd_tmpl_context
    ON streamdeck_templates (context_type, device_type)
    """))
    conn.execute(text("""
CREATE INDEX IF NOT EXISTS idx_sd_tmpl_default
    ON streamdeck_templates (context_type, device_type)
    WHERE is_default = true
    """))

    # ── 2. streamdeck_user_config ────────────────────────────────────────────

    conn.execute(text("""
CREATE TABLE IF NOT EXISTS streamdeck_user_config (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           CHAR(40) NOT NULL,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    device_serial       VARCHAR(100),
    device_type         VARCHAR(30) NOT NULL DEFAULT 'mk2_15',
    context_assignments JSONB NOT NULL DEFAULT '{}',
    pinned_tags         JSONB NOT NULL DEFAULT '{}',
    auto_advance        BOOLEAN NOT NULL DEFAULT true,
    api_token_hash      VARCHAR(256),
    last_connected_at   TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_sd_user_device UNIQUE (tenant_id, user_id, device_serial)
)
    """))

    conn.execute(text("""
CREATE INDEX IF NOT EXISTS idx_sd_user_tenant
    ON streamdeck_user_config (tenant_id, user_id)
    """))

    # ── 3. Seed default templates ────────────────────────────────────────────

    seeds = [
        ("ediscovery_review_mk2", "eDiscovery Review (MK.2 15-key)", "ediscovery_review", "mk2_15", 3, 5, EDISCOVERY_REVIEW_MK2, True),
        ("billing_mk2",           "Billing & Timekeeping (MK.2 15-key)", "billing", "mk2_15", 3, 5, BILLING_MK2, True),
        ("global_idle_mk2",       "Global / Idle (MK.2 15-key)", "global", "mk2_15", 3, 5, GLOBAL_IDLE_MK2, True),
    ]

    for slug, name, ctx, dev, rows, cols, buttons, default in seeds:
        conn.execute(text("""
INSERT INTO streamdeck_templates
    (tenant_id, template_slug, display_name, context_type, device_type,
     grid_rows, grid_cols, buttons, is_default)
VALUES
    (NULL, :slug, :name, :ctx, :dev, :rows, :cols, CAST(:buttons AS jsonb), :dflt)
ON CONFLICT (tenant_id, template_slug) DO NOTHING
        """), {
            "slug": slug, "name": name, "ctx": ctx, "dev": dev,
            "rows": rows, "cols": cols,
            "buttons": json.dumps(buttons),
            "dflt": default,
        })


def downgrade():
    op.execute("DROP TABLE IF EXISTS streamdeck_user_config CASCADE")
    op.execute("DROP TABLE IF EXISTS streamdeck_templates CASCADE")
