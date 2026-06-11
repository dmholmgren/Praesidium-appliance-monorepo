"""widget_registry_matter_detail — seed matter widgets + layout

Revision ID: 0037_matter_detail_widgets
Revises: 0036_layout_tabs
Create Date: 2026-04-17

Fix: widget_positions JSON passed as bound :pos parameter to avoid
SQLAlchemy interpreting JSON key:value colons as bind parameters.
"""

import json
from alembic import op
from sqlalchemy import text

revision = '0037_matter_detail_widgets'
down_revision = '0036_layout_tabs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ------------------------------------------------------------------
    # 1. widget_registry
    # ------------------------------------------------------------------
    conn.execute(text("""
        INSERT INTO widget_registry (
            widget_slug, widget_name, category, widget_type,
            data_source, render_template,
            default_size, permission_level,
            tenant_id, is_platform_standard
        ) VALUES
        (
            'matter_header', 'Matter Header', 'matter', 'data_panel',
            'modules.dms.services.widget_service.get_matter_header',
            'widgets/matter_header.html',
            'full-width', 'attorney',
            NULL, TRUE
        ),
        (
            'dms_document_activity_feed', 'Document Activity', 'dms', 'data_panel',
            'modules.dms.services.widget_service.get_document_activity_feed',
            'widgets/dms_document_activity_feed.html',
            'medium', 'attorney',
            NULL, TRUE
        ),
        (
            'dms_folder_health', 'Folder Health', 'dms', 'data_panel',
            'modules.dms.services.widget_service.get_folder_health',
            'widgets/dms_folder_health.html',
            'medium', 'attorney',
            NULL, TRUE
        ),
        (
            'dms_storage_stats', 'Storage Stats', 'dms', 'data_panel',
            'modules.dms.services.widget_service.get_storage_stats',
            'widgets/dms_storage_stats.html',
            'medium', 'attorney',
            NULL, TRUE
        )
        ON CONFLICT (widget_slug, COALESCE(tenant_id, ''::bpchar)) DO UPDATE
            SET widget_type          = EXCLUDED.widget_type,
                category             = EXCLUDED.category,
                data_source          = EXCLUDED.data_source,
                render_template      = EXCLUDED.render_template,
                default_size         = EXCLUDED.default_size,
                is_platform_standard = EXCLUDED.is_platform_standard;
    """))

    conn.execute(text("""
        UPDATE widget_registry
        SET widget_type     = 'data_panel',
            data_source     = 'modules.dms.services.widget_service.get_document_activity_feed',
            render_template = 'widgets/dms_document_activity_feed.html'
        WHERE widget_slug = 'dms_document_activity_feed'
          AND (data_source IS NULL OR widget_type = 'placeholder');
    """))

    conn.execute(text("""
        UPDATE widget_registry
        SET widget_type     = 'data_panel',
            data_source     = 'modules.dms.services.widget_service.get_folder_health',
            render_template = 'widgets/dms_folder_health.html'
        WHERE widget_slug = 'dms_folder_health'
          AND (data_source IS NULL OR widget_type = 'placeholder');
    """))

    conn.execute(text("""
        UPDATE widget_registry
        SET data_source = 'modules.dms.services.widget_service.get_recent_documents'
        WHERE widget_slug = 'dms_recent_documents'
          AND (data_source IS NULL
               OR data_source = 'modules.dms.widget_service.get_widget_recent_documents');
    """))

    # ------------------------------------------------------------------
    # 2. layout_tabs
    # ------------------------------------------------------------------
    conn.execute(text("""
        INSERT INTO layout_tabs (
            layout_slug, tab_slug, display_name,
            display_order, permission_level, is_visible
        ) VALUES
            ('matter_detail', 'matter_overview',  'Overview',   1, 'attorney', TRUE),
            ('matter_detail', 'matter_billing',   'Billing',    2, 'attorney', TRUE),
            ('matter_detail', 'matter_docs',      'Documents',  3, 'attorney', TRUE),
            ('matter_detail', 'matter_deadlines', 'Deadlines',  4, 'attorney', TRUE)
        ON CONFLICT (layout_slug, tab_slug) DO UPDATE
            SET display_name  = EXCLUDED.display_name,
                display_order = EXCLUDED.display_order,
                is_visible    = EXCLUDED.is_visible;
    """))

    # ------------------------------------------------------------------
    # 3. layout_registry — pass JSON as bound parameter to avoid
    #    SQLAlchemy misinterpreting "key":value colons as bind params
    # ------------------------------------------------------------------

    insert_sql = text("""
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

    overview = json.dumps([
        {"widget_slug": "matter_header",              "row": 1, "col": 1, "size_override": "full-width", "config_override": {"context": "dashboard"}},
        {"widget_slug": "billing_matter_kpi",         "row": 2, "col": 1, "size_override": "full-width"},
        {"widget_slug": "dms_recent_documents",       "row": 3, "col": 1, "size_override": "medium"},
        {"widget_slug": "dms_document_activity_feed", "row": 3, "col": 3, "size_override": "medium"},
        {"widget_slug": "firm_calendar_strip",        "row": 4, "col": 1, "size_override": "medium"},
        {"widget_slug": "atty_deadlines",             "row": 4, "col": 3, "size_override": "medium"},
    ])

    billing = json.dumps([
        {"widget_slug": "matter_header",                    "row": 1, "col": 1, "size_override": "full-width", "config_override": {"context": "dashboard"}},
        {"widget_slug": "billing_matter_kpi",               "row": 2, "col": 1, "size_override": "full-width"},
        {"widget_slug": "billing_matter_wip_by_timekeeper", "row": 3, "col": 1, "size_override": "medium"},
        {"widget_slug": "billing_matter_open_invoices",     "row": 3, "col": 3, "size_override": "medium"},
        {"widget_slug": "billing_matter_recent_slips",      "row": 4, "col": 1, "size_override": "full-width"},
    ])

    docs = json.dumps([
        {"widget_slug": "matter_header",              "row": 1, "col": 1, "size_override": "full-width", "config_override": {"context": "dashboard"}},
        {"widget_slug": "dms_recent_documents",       "row": 2, "col": 1, "size_override": "medium"},
        {"widget_slug": "dms_document_activity_feed", "row": 2, "col": 3, "size_override": "medium"},
        {"widget_slug": "dms_folder_health",          "row": 3, "col": 1, "size_override": "medium"},
        {"widget_slug": "dms_doc_count",              "row": 3, "col": 3, "size_override": "small"},
    ])

    deadlines = json.dumps([
        {"widget_slug": "matter_header",          "row": 1, "col": 1, "size_override": "full-width", "config_override": {"context": "dashboard"}},
        {"widget_slug": "atty_deadlines",         "row": 2, "col": 1, "size_override": "medium"},
        {"widget_slug": "firm_calendar_strip",    "row": 2, "col": 3, "size_override": "medium"},
        {"widget_slug": "firm_new_service_items", "row": 3, "col": 1, "size_override": "full-width"},
    ])

    conn.execute(insert_sql, {"slug": "matter_detail", "tab": "matter_overview",  "pos": overview})
    conn.execute(insert_sql, {"slug": "matter_detail", "tab": "matter_billing",   "pos": billing})
    conn.execute(insert_sql, {"slug": "matter_detail", "tab": "matter_docs",      "pos": docs})
    conn.execute(insert_sql, {"slug": "matter_detail", "tab": "matter_deadlines", "pos": deadlines})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("""
        DELETE FROM layout_registry
        WHERE layout_slug = 'matter_detail'
          AND tenant_id = 'platform-default';
    """))
    conn.execute(text("DELETE FROM layout_tabs WHERE layout_slug = 'matter_detail';"))
    conn.execute(text("""
        DELETE FROM widget_registry
        WHERE widget_slug IN ('matter_header', 'dms_storage_stats')
          AND tenant_id IS NULL;
    """))
