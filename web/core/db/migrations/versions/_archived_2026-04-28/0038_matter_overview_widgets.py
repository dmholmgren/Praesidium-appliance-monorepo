"""matter_overview_widgets — seed new matter widgets + reseed layout

Revision ID: 0038_matter_overview_widgets
Revises: 0037_matter_detail_widgets
Create Date: 2026-04-17

Fix: split widget inserts into separate statements to avoid asyncpg
ambiguous parameter type error with NULL data_source.
"""

import json
from alembic import op
from sqlalchemy import text

revision = '0038_matter_overview_widgets'
down_revision = '0037_matter_detail_widgets'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ------------------------------------------------------------------
    # 1. widget_registry — widgets WITH data_source
    # ------------------------------------------------------------------
    widgets_with_ds = [
        ('matter_summary',            'Case Status',     'matter',
         'modules.dashboard.services.matter_widget_service.get_matter_summary',
         'widgets/matter_summary.html', 'medium'),
        ('matter_tasks',              'Tasks',           'matter',
         'modules.dashboard.services.matter_widget_service.get_matter_tasks',
         'widgets/matter_tasks.html', 'medium'),
        ('matter_communications',     'Communications',  'matter',
         'modules.dashboard.services.matter_widget_service.get_matter_communications',
         'widgets/matter_communications.html', 'medium'),
        ('matter_deadlines',          'Deadlines',       'matter',
         'modules.dashboard.services.matter_widget_service.get_matter_deadlines',
         'widgets/matter_deadlines.html', 'medium'),
        ('matter_documents_launcher', 'Documents',       'matter',
         'modules.dashboard.services.matter_widget_service.get_matter_documents_launcher',
         'widgets/matter_documents_launcher.html', 'full-width'),
    ]

    insert_with_ds = text("""
        INSERT INTO widget_registry (
            widget_slug, widget_name, category, widget_type,
            data_source, render_template,
            default_size, permission_level,
            tenant_id, is_platform_standard
        ) VALUES (
            :slug, :name, :category, 'data_panel',
            :ds, :tmpl, :size, 'attorney', NULL, TRUE
        )
        ON CONFLICT (widget_slug, COALESCE(tenant_id, ''::bpchar)) DO UPDATE
            SET data_source          = EXCLUDED.data_source,
                render_template      = EXCLUDED.render_template,
                default_size         = EXCLUDED.default_size,
                is_platform_standard = EXCLUDED.is_platform_standard;
    """)

    for slug, name, category, ds, tmpl, size in widgets_with_ds:
        conn.execute(insert_with_ds, {
            "slug": slug, "name": name, "category": category,
            "ds": ds, "tmpl": tmpl, "size": size
        })

    # ------------------------------------------------------------------
    # 2. widget_registry — matter_ai_chat (no data_source — template only)
    # ------------------------------------------------------------------
    conn.execute(text("""
        INSERT INTO widget_registry (
            widget_slug, widget_name, category, widget_type,
            render_template, default_size, permission_level,
            tenant_id, is_platform_standard
        ) VALUES (
            'matter_ai_chat', 'Ask Praesidium', 'matter', 'data_panel',
            'widgets/matter_ai_chat.html', 'full-width', 'attorney',
            NULL, TRUE
        )
        ON CONFLICT (widget_slug, COALESCE(tenant_id, ''::bpchar)) DO UPDATE
            SET render_template      = EXCLUDED.render_template,
                default_size         = EXCLUDED.default_size,
                is_platform_standard = EXCLUDED.is_platform_standard;
    """))

    # ------------------------------------------------------------------
    # 3. Placeholder widgets for Matter Intelligence tab
    # ------------------------------------------------------------------
    placeholders = [
        ('matter_causes_of_action', 'Causes of Action', 'matter'),
        ('matter_gantt',            'Timeline',          'matter'),
        ('matter_drift',            'Drift Analysis',    'matter'),
    ]
    for slug, name, category in placeholders:
        conn.execute(text("""
            INSERT INTO widget_registry (
                widget_slug, widget_name, category, widget_type,
                default_size, permission_level,
                tenant_id, is_platform_standard
            ) VALUES (
                :slug, :name, :category, 'placeholder',
                'full-width', 'attorney', NULL, TRUE
            )
            ON CONFLICT (widget_slug, COALESCE(tenant_id, ''::bpchar)) DO NOTHING;
        """), {"slug": slug, "name": name, "category": category})

    # ------------------------------------------------------------------
    # 4. layout_tabs — replace old 4 tabs with new 4 tabs
    # ------------------------------------------------------------------
    conn.execute(text("""
        DELETE FROM layout_tabs WHERE layout_slug = 'matter_detail';
    """))

    conn.execute(text("""
        INSERT INTO layout_tabs (
            layout_slug, tab_slug, display_name,
            display_order, permission_level, is_visible
        ) VALUES
            ('matter_detail', 'overview',            'Overview',            1, 'attorney', TRUE),
            ('matter_detail', 'matter_intelligence', 'Matter Intelligence', 2, 'attorney', TRUE),
            ('matter_detail', 'billing',             'Billing',             3, 'attorney', TRUE),
            ('matter_detail', 'documents',           'Documents',           4, 'attorney', TRUE)
        ON CONFLICT (layout_slug, tab_slug) DO UPDATE
            SET display_name  = EXCLUDED.display_name,
                display_order = EXCLUDED.display_order,
                is_visible    = EXCLUDED.is_visible;
    """))

    # ------------------------------------------------------------------
    # 5. layout_registry — remove old, seed new
    # ------------------------------------------------------------------
    conn.execute(text("""
        DELETE FROM layout_registry
        WHERE layout_slug = 'matter_detail'
          AND tenant_id = 'platform-default';
    """))

    insert_layout = text("""
        INSERT INTO layout_registry (
            layout_slug, tab_slug, widget_positions,
            is_default, tenant_id, user_id
        ) VALUES (
            :slug, :tab, CAST(:pos AS jsonb),
            TRUE, 'platform-default', NULL
        )
        ON CONFLICT (tenant_id, layout_slug, tab_slug, COALESCE(user_id::text, ''))
        DO UPDATE SET widget_positions = EXCLUDED.widget_positions;
    """)

    # Overview: Row1 medium|small|small, Row2 medium|small|small, Row3 full
    overview = json.dumps([
        {"widget_slug": "matter_summary",        "row": 1, "col": 1, "size_override": "medium"},
        {"widget_slug": "matter_deadlines",      "row": 1, "col": 2, "size_override": "small"},
        {"widget_slug": "billing_matter_kpi",    "row": 1, "col": 3, "size_override": "small"},
        {"widget_slug": "matter_tasks",          "row": 2, "col": 1, "size_override": "medium"},
        {"widget_slug": "dms_recent_documents",  "row": 2, "col": 2, "size_override": "small"},
        {"widget_slug": "matter_communications", "row": 2, "col": 3, "size_override": "small"},
        {"widget_slug": "matter_ai_chat",        "row": 3, "col": 1, "size_override": "full-width"},
    ])

    # Matter Intelligence: placeholders
    intelligence = json.dumps([
        {"widget_slug": "matter_causes_of_action", "row": 1, "col": 1, "size_override": "full-width"},
        {"widget_slug": "matter_gantt",            "row": 2, "col": 1, "size_override": "medium"},
        {"widget_slug": "matter_drift",            "row": 2, "col": 3, "size_override": "medium"},
    ])

    # Billing: existing widgets + AI chat with billing context
    billing = json.dumps([
        {"widget_slug": "billing_matter_kpi",               "row": 1, "col": 1, "size_override": "full-width"},
        {"widget_slug": "billing_matter_wip_by_timekeeper", "row": 2, "col": 1, "size_override": "medium"},
        {"widget_slug": "billing_matter_open_invoices",     "row": 2, "col": 3, "size_override": "medium"},
        {"widget_slug": "billing_matter_recent_slips",      "row": 3, "col": 1, "size_override": "full-width"},
        {"widget_slug": "matter_ai_chat",                   "row": 4, "col": 1, "size_override": "full-width",
         "config_override": {"context": "billing"}},
    ])

    # Documents: launcher only
    documents = json.dumps([
        {"widget_slug": "matter_documents_launcher", "row": 1, "col": 1, "size_override": "full-width"},
    ])

    conn.execute(insert_layout, {"slug": "matter_detail", "tab": "overview",            "pos": overview})
    conn.execute(insert_layout, {"slug": "matter_detail", "tab": "matter_intelligence", "pos": intelligence})
    conn.execute(insert_layout, {"slug": "matter_detail", "tab": "billing",             "pos": billing})
    conn.execute(insert_layout, {"slug": "matter_detail", "tab": "documents",           "pos": documents})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("""
        DELETE FROM layout_registry
        WHERE layout_slug = 'matter_detail' AND tenant_id = 'platform-default';
    """))
    conn.execute(text("DELETE FROM layout_tabs WHERE layout_slug = 'matter_detail';"))
    conn.execute(text("""
        DELETE FROM widget_registry
        WHERE widget_slug IN (
            'matter_summary', 'matter_tasks', 'matter_communications',
            'matter_deadlines', 'matter_documents_launcher', 'matter_ai_chat',
            'matter_causes_of_action', 'matter_gantt', 'matter_drift'
        ) AND tenant_id IS NULL;
    """))
