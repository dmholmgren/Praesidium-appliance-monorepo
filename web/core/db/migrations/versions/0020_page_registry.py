"""0020_page_registry

Revision ID: 0020_page_registry
Revises: 0019_m10_dms_framework
Create Date: 2026-04-05

Creates:
  - connector_registry      (missing from 0018 — blocks registry_router)
  - ui_templates            (missing from 0018 — blocks template loading)
  - ui_nav_items            (missing from 0018 — blocks nav)
  - page_registry           (new — every page in the platform as a row)
  - dashboard_panel_registry (new — matter dashboard panels as data)
  - tenant_panel_defaults   (new — per-firm default panel layout)
  - user_dashboard_layout   (new — per-user panel arrangement)

Drops:
  - collections             (legacy pre-PostgreSQL table, VARCHAR PK, orphaned)

Seeds:
  - 7 connector rows into connector_registry
  - 9 panel rows into dashboard_panel_registry
"""
import os
import json
import psycopg2

revision = '0020_page_registry'
down_revision = '0019_m10_dms_framework'
branch_labels = None
depends_on = None


def _get_conn():
    db_url = os.environ.get("DATABASE_URL", "")
    raw = db_url.replace("postgresql+asyncpg://", "").replace("postgresql://", "")
    at = raw.rfind("@")
    userpass = raw[:at]
    hostdb = raw[at + 1:]
    colon = userpass.find(":")
    user = userpass[:colon]
    password = userpass[colon + 1:]
    slash = hostdb.rfind("/")
    hostport = hostdb[:slash]
    dbname = hostdb[slash + 1:].split("?")[0]
    hp = hostport.split(":")
    host = hp[0]
    conn = psycopg2.connect(host=host, port=5432, dbname=dbname, user=user, password=password)
    conn.autocommit = True
    return conn


def upgrade():
    conn = _get_conn()
    cur = conn.cursor()

    # ── Drop legacy orphan ────────────────────────────────────
    cur.execute("DROP TABLE IF EXISTS collections CASCADE")

    # ── connector_registry ────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS connector_registry (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            connector_type      VARCHAR(64) NOT NULL UNIQUE,
            display_name        VARCHAR(255) NOT NULL,
            description         TEXT,
            icon                VARCHAR(255),
            sync_type           VARCHAR(32) NOT NULL DEFAULT 'api_pull',
            config_fields       JSONB NOT NULL DEFAULT '[]',
            credential_fields   JSONB NOT NULL DEFAULT '[]',
            schedule_options    JSONB NOT NULL DEFAULT '[]',
            ingest_endpoint     VARCHAR(512),
            is_active           BOOLEAN NOT NULL DEFAULT true,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_connector_registry_type
            ON connector_registry (connector_type)
    """)

    # ── ui_templates ─────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ui_templates (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            template_name   VARCHAR(512) NOT NULL,
            content         TEXT NOT NULL,
            version         INTEGER NOT NULL DEFAULT 1,
            is_active       BOOLEAN NOT NULL DEFAULT true,
            tenant_id       CHAR(36),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_ui_templates_name_version
            ON ui_templates (template_name, version DESC)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_ui_templates_tenant
            ON ui_templates (tenant_id) WHERE tenant_id IS NOT NULL
    """)

    # ── ui_nav_items ──────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ui_nav_items (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            nav_key         VARCHAR(128) NOT NULL UNIQUE,
            label           VARCHAR(255) NOT NULL,
            icon            VARCHAR(255),
            url_path        VARCHAR(512),
            parent_key      VARCHAR(128),
            display_order   INTEGER NOT NULL DEFAULT 0,
            required_role   VARCHAR(64),
            feature_flag    VARCHAR(128),
            is_active       BOOLEAN NOT NULL DEFAULT true,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_ui_nav_items_parent
            ON ui_nav_items (parent_key) WHERE parent_key IS NOT NULL
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_ui_nav_items_order
            ON ui_nav_items (display_order)
    """)

    # ── page_registry ─────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS page_registry (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            page_slug           VARCHAR(128) NOT NULL UNIQUE,
            display_name        VARCHAR(255) NOT NULL,
            module              VARCHAR(128) NOT NULL,
            route_pattern       VARCHAR(512) NOT NULL,
            page_type           VARCHAR(32) NOT NULL DEFAULT 'detail',
            default_layout      JSONB NOT NULL DEFAULT '{}',
            required_license    VARCHAR(64),
            required_permission VARCHAR(64),
            feature_flag        VARCHAR(128),
            nav_key             VARCHAR(128),
            is_enabled          BOOLEAN NOT NULL DEFAULT true,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_page_registry_module
            ON page_registry (module)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_page_registry_flag
            ON page_registry (feature_flag) WHERE feature_flag IS NOT NULL
    """)

    # ── dashboard_panel_registry ──────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dashboard_panel_registry (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            panel_slug          VARCHAR(128) NOT NULL UNIQUE,
            display_name        VARCHAR(255) NOT NULL,
            description         TEXT,
            service_module      VARCHAR(512),
            service_function    VARCHAR(255),
            template_name       VARCHAR(512) NOT NULL,
            display_order       INTEGER NOT NULL DEFAULT 0,
            panel_width         VARCHAR(16) NOT NULL DEFAULT 'full',
            practice_areas      JSONB,
            required_permission VARCHAR(64),
            feature_flag        VARCHAR(128),
            is_enabled          BOOLEAN NOT NULL DEFAULT true,
            is_default          BOOLEAN NOT NULL DEFAULT true,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_dashboard_panel_order
            ON dashboard_panel_registry (display_order)
    """)

    # ── tenant_panel_defaults ─────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tenant_panel_defaults (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       CHAR(36) NOT NULL,
            panel_slug      VARCHAR(128) NOT NULL,
            is_enabled      BOOLEAN NOT NULL DEFAULT true,
            display_order   INTEGER NOT NULL DEFAULT 0,
            is_locked       BOOLEAN NOT NULL DEFAULT false,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, panel_slug)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_tenant_panel_defaults_tenant
            ON tenant_panel_defaults (tenant_id)
    """)

    # ── user_dashboard_layout ─────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_dashboard_layout (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       CHAR(36) NOT NULL,
            user_id         BIGINT NOT NULL,
            page_slug       VARCHAR(128) NOT NULL DEFAULT 'matter_dashboard',
            panel_order     JSONB NOT NULL DEFAULT '[]',
            hidden_panels   JSONB NOT NULL DEFAULT '[]',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, user_id, page_slug)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ix_user_dashboard_layout_user
            ON user_dashboard_layout (tenant_id, user_id)
    """)

    # ── Grants ────────────────────────────────────────────────
    for tbl in [
        'connector_registry', 'ui_templates', 'ui_nav_items',
        'page_registry', 'dashboard_panel_registry',
        'tenant_panel_defaults', 'user_dashboard_layout',
    ]:
        cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO praesidium_db")

    # ── Seed connector_registry ───────────────────────────────
    connectors = [
        {
            "connector_type": "timeslips",
            "display_name": "Sage Timeslips",
            "description": "Billing data from Sage Timeslips via Windows agent push",
            "icon": "clock",
            "sync_type": "agent_push",
            "config_fields": [
                {"name": "sync_frequency", "label": "Sync Frequency", "type": "select",
                 "options": ["hourly", "daily", "manual"], "required": True}
            ],
            "credential_fields": [
                {"name": "ingest_api_key", "label": "Ingest API Key", "type": "password",
                 "required": True, "secret": True}
            ],
            "schedule_options": [
                {"label": "Hourly", "value": "hourly"},
                {"label": "Daily", "value": "daily"},
                {"label": "Manual only", "value": "manual"}
            ],
            "ingest_endpoint": "/api/connectors/timeslips/ingest",
        },
        {
            "connector_type": "windows_agent",
            "display_name": "Windows File Agent",
            "description": "Local file indexing agent on Windows Server — direct NTFS access",
            "icon": "folder",
            "sync_type": "agent_push",
            "config_fields": [
                {"name": "watch_folders", "label": "Watch Folders", "type": "text",
                 "placeholder": "D:\\Public\\Clients", "required": True},
                {"name": "batch_size", "label": "Batch Size", "type": "number",
                 "placeholder": "250", "required": False}
            ],
            "credential_fields": [
                {"name": "ingest_api_key", "label": "Ingest API Key", "type": "password",
                 "required": True, "secret": True}
            ],
            "schedule_options": [
                {"label": "Continuous", "value": "continuous"},
                {"label": "Manual only", "value": "manual"}
            ],
            "ingest_endpoint": "/api/connectors/files/ingest",
        },
        {
            "connector_type": "exchange",
            "display_name": "Microsoft Exchange",
            "description": "On-premises Exchange via EWS — email and calendar sync",
            "icon": "mail",
            "sync_type": "api_pull",
            "config_fields": [
                {"name": "ews_url", "label": "EWS URL", "type": "text",
                 "placeholder": "https://mail.firm.com/EWS/Exchange.asmx", "required": True},
                {"name": "mailbox", "label": "Mailbox", "type": "text",
                 "placeholder": "attorney@firm.com", "required": True}
            ],
            "credential_fields": [
                {"name": "password", "label": "Password", "type": "password",
                 "required": True, "secret": True}
            ],
            "schedule_options": [
                {"label": "Every 15 minutes", "value": "15min"},
                {"label": "Hourly", "value": "hourly"}
            ],
            "ingest_endpoint": None,
        },
        {
            "connector_type": "manictime",
            "display_name": "ManicTime",
            "description": "Desktop activity tracking via ManicTime Server REST API",
            "icon": "activity",
            "sync_type": "api_pull",
            "config_fields": [
                {"name": "server_url", "label": "ManicTime Server URL", "type": "text",
                 "placeholder": "https://manictime.firm.com", "required": True},
                {"name": "username", "label": "Username", "type": "text", "required": True}
            ],
            "credential_fields": [
                {"name": "api_key", "label": "API Key", "type": "password",
                 "required": True, "secret": True}
            ],
            "schedule_options": [
                {"label": "Hourly", "value": "hourly"},
                {"label": "Daily", "value": "daily"}
            ],
            "ingest_endpoint": None,
        },
        {
            "connector_type": "pbx_cdr",
            "display_name": "FreePBX Call Records",
            "description": "Phone call records from FreePBX via CSV import",
            "icon": "phone",
            "sync_type": "csv_import",
            "config_fields": [
                {"name": "pbx_url", "label": "FreePBX URL", "type": "text",
                 "placeholder": "https://pbx.firm.com", "required": True}
            ],
            "credential_fields": [
                {"name": "api_key", "label": "API Key", "type": "password",
                 "required": True, "secret": True}
            ],
            "schedule_options": [
                {"label": "Daily", "value": "daily"},
                {"label": "Manual only", "value": "manual"}
            ],
            "ingest_endpoint": None,
        },
        {
            "connector_type": "courtlistener",
            "display_name": "CourtListener",
            "description": "Federal court data and PACER docket monitoring",
            "icon": "gavel",
            "sync_type": "api_pull",
            "config_fields": [
                {"name": "username", "label": "CourtListener Username", "type": "text",
                 "required": True}
            ],
            "credential_fields": [
                {"name": "password", "label": "Password", "type": "password",
                 "required": True, "secret": True}
            ],
            "schedule_options": [
                {"label": "Daily", "value": "daily"},
                {"label": "Manual only", "value": "manual"}
            ],
            "ingest_endpoint": None,
        },
        {
            "connector_type": "file_crawler",
            "display_name": "CIFS File Crawler",
            "description": "Legacy CIFS share crawler — superseded by Windows agent for bulk",
            "icon": "search",
            "sync_type": "agent_push",
            "config_fields": [
                {"name": "share_path", "label": "Share Path", "type": "text",
                 "placeholder": "\\\\server\\share", "required": True}
            ],
            "credential_fields": [],
            "schedule_options": [
                {"label": "Manual only", "value": "manual"}
            ],
            "ingest_endpoint": None,
        },
    ]

    for c in connectors:
        cur.execute("""
            INSERT INTO connector_registry
                (connector_type, display_name, description, icon, sync_type,
                 config_fields, credential_fields, schedule_options, ingest_endpoint, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true)
            ON CONFLICT (connector_type) DO NOTHING
        """, (
            c["connector_type"], c["display_name"], c["description"], c["icon"],
            c["sync_type"],
            json.dumps(c["config_fields"]),
            json.dumps(c["credential_fields"]),
            json.dumps(c["schedule_options"]),
            c.get("ingest_endpoint"),
        ))

    # ── Seed dashboard_panel_registry ─────────────────────────
    panels = [
        {
            "panel_slug": "summary",
            "display_name": "Case Summary",
            "description": "AI-generated two-paragraph case summary and causes of action",
            "service_module": "modules.dashboard.services.case_summary",
            "service_function": "get_summary_panel_data",
            "template_name": "dashboard/partials/summary.html",
            "display_order": 1,
            "panel_width": "full",
            "practice_areas": None,
            "required_permission": None,
            "feature_flag": None,
            "is_default": True,
        },
        {
            "panel_slug": "tasks",
            "display_name": "Tasks",
            "description": "Open tasks and commitments for this matter",
            "service_module": "modules.dashboard.services.task_system",
            "service_function": "get_tasks_for_matter",
            "template_name": "dashboard/partials/tasks.html",
            "display_order": 2,
            "panel_width": "half",
            "practice_areas": None,
            "required_permission": None,
            "feature_flag": None,
            "is_default": True,
        },
        {
            "panel_slug": "gantt",
            "display_name": "Deadline Timeline",
            "description": "Gantt chart of matter deadlines",
            "service_module": "modules.dashboard.services.gantt",
            "service_function": "generate_matter_gantt",
            "template_name": "dashboard/partials/gantt.html",
            "display_order": 3,
            "panel_width": "full",
            "practice_areas": None,
            "required_permission": None,
            "feature_flag": None,
            "is_default": True,
        },
        {
            "panel_slug": "communications",
            "display_name": "Communications",
            "description": "Client and opposing counsel communication log",
            "service_module": "modules.dashboard.services.matter_modules",
            "service_function": "get_communications",
            "template_name": "dashboard/partials/communications.html",
            "display_order": 4,
            "panel_width": "full",
            "practice_areas": None,
            "required_permission": None,
            "feature_flag": None,
            "is_default": True,
        },
        {
            "panel_slug": "settlements",
            "display_name": "Settlement History",
            "description": "Demand and offer tracking with trend analysis",
            "service_module": "modules.dashboard.services.matter_modules",
            "service_function": "get_settlement_history",
            "template_name": "dashboard/partials/settlements.html",
            "display_order": 5,
            "panel_width": "half",
            "practice_areas": '["litigation"]',
            "required_permission": None,
            "feature_flag": None,
            "is_default": True,
        },
        {
            "panel_slug": "experts",
            "display_name": "Expert Witnesses",
            "description": "Retained and opposing expert witness tracking",
            "service_module": "modules.dashboard.services.matter_modules",
            "service_function": "get_experts_for_matter",
            "template_name": "dashboard/partials/experts.html",
            "display_order": 6,
            "panel_width": "half",
            "practice_areas": '["litigation"]',
            "required_permission": None,
            "feature_flag": None,
            "is_default": True,
        },
        {
            "panel_slug": "mediations",
            "display_name": "Mediations",
            "description": "Scheduled and completed mediation sessions",
            "service_module": "modules.dashboard.services.matter_modules",
            "service_function": "get_mediations_for_matter",
            "template_name": "dashboard/partials/mediations.html",
            "display_order": 7,
            "panel_width": "half",
            "practice_areas": '["litigation"]',
            "required_permission": None,
            "feature_flag": None,
            "is_default": True,
        },
        {
            "panel_slug": "engagement",
            "display_name": "Engagement Letters",
            "description": "Engagement letter status and signature tracking",
            "service_module": "modules.dashboard.services.matter_modules",
            "service_function": "get_engagement_letters",
            "template_name": "dashboard/partials/engagement.html",
            "display_order": 8,
            "panel_width": "half",
            "practice_areas": None,
            "required_permission": None,
            "feature_flag": None,
            "is_default": True,
        },
        {
            "panel_slug": "title_comparison",
            "display_name": "Title Date Comparison",
            "description": "Title commitment date discrepancy analysis",
            "service_module": "modules.dashboard.services.real_estate",
            "service_function": "get_title_comparison",
            "template_name": "dashboard/partials/title_comparison.html",
            "display_order": 9,
            "panel_width": "full",
            "practice_areas": '["real_estate"]',
            "required_permission": None,
            "feature_flag": None,
            "is_default": False,
        },
    ]

    for p in panels:
        cur.execute("""
            INSERT INTO dashboard_panel_registry
                (panel_slug, display_name, description, service_module, service_function,
                 template_name, display_order, panel_width, practice_areas,
                 required_permission, feature_flag, is_enabled, is_default)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s)
            ON CONFLICT (panel_slug) DO NOTHING
        """, (
            p["panel_slug"], p["display_name"], p["description"],
            p["service_module"], p["service_function"], p["template_name"],
            p["display_order"], p["panel_width"],
            p["practice_areas"],
            p["required_permission"], p["feature_flag"], p["is_default"],
        ))

    # ── Seed page_registry ────────────────────────────────────
    pages = [
        ("matter_dashboard", "Matter Dashboard", "dashboard",
         "/dashboard/matter/{matter_id}", "widget_grid", "attorney"),
        ("dashboard_home", "Matters", "dashboard",
         "/dashboard/", "list", None),
        ("ediscovery_review", "Document Review", "ediscovery",
         "/ediscovery/review", "list", "attorney"),
        ("billing_home", "Billing", "billing",
         "/billing/", "list", "attorney"),
        ("connector_list", "Connectors", "connectors",
         "/tenant-admin/connectors", "list", "admin"),
    ]
    for slug, name, module, route, ptype, perm in pages:
        cur.execute("""
            INSERT INTO page_registry
                (page_slug, display_name, module, route_pattern, page_type, required_permission)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (page_slug) DO NOTHING
        """, (slug, name, module, route, ptype, perm))

    cur.close()
    conn.close()


def downgrade():
    conn = _get_conn()
    cur = conn.cursor()
    for tbl in [
        'user_dashboard_layout', 'tenant_panel_defaults',
        'dashboard_panel_registry', 'page_registry',
        'ui_nav_items', 'ui_templates', 'connector_registry',
    ]:
        cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
    cur.close()
    conn.close()
