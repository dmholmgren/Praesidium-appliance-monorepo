"""dms_capture_widgets — register scan/dictation drop zone and queue widgets

Revision ID: 0040_dms_capture_widgets
Revises: 0039_ediscovery_review_widgets
Create Date: 2026-04-18

Four new platform-standard widgets for the DMS home capture surface:
    dms_scan_drop_zone    — document file drop zone with matter assignment
    dms_dictation_drop    — audio file drop zone with matter assignment
    dms_scan_queue        — scan queue with assign/file actions
    dms_dictation_queue   — dictation queue with transcript previews

Plus dms_home layout entry composing all four in a 3x2 grid
(folder tree is rendered inline in dms_home.html, not a widget).
"""

import json
from alembic import op
from sqlalchemy import text

revision = '0040_dms_capture_widgets'
down_revision = '0039_ediscovery_review_widgets'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. widget_registry rows ───────────────────────────────────────────────
    conn.execute(text("""
        INSERT INTO widget_registry (
            widget_slug, widget_name, category, widget_type,
            data_source, render_template,
            default_size, permission_level,
            tenant_id, is_platform_standard
        ) VALUES
        (
            'dms_scan_drop_zone',
            'Document Drop Zone',
            'dms', 'data_panel',
            'modules.dms.services.widget_service.get_scan_drop_zone',
            'widgets/dms_scan_drop_zone.html',
            'medium', 'attorney',
            NULL, TRUE
        ),
        (
            'dms_dictation_drop',
            'Dictation Drop Zone',
            'dms', 'data_panel',
            'modules.dms.services.widget_service.get_dictation_drop',
            'widgets/dms_dictation_drop.html',
            'medium', 'attorney',
            NULL, TRUE
        ),
        (
            'dms_scan_queue',
            'Scan Queue',
            'dms', 'data_panel',
            'modules.dms.services.widget_service.get_scan_queue',
            'widgets/dms_scan_queue.html',
            'medium', 'attorney',
            NULL, TRUE
        ),
        (
            'dms_dictation_queue',
            'Dictation Queue',
            'dms', 'data_panel',
            'modules.dms.services.widget_service.get_dictation_queue',
            'widgets/dms_dictation_queue.html',
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

    # ── 2. layout_registry — dms_home 3x2 grid (tree inline, 4 widgets) ──────
    # Grid positions reference the 2-column widget area (cols 2-3 of the full
    # 3-column page layout). Row 1 = drop zones, Row 2 = queues.
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

    positions = json.dumps([
        {"widget_slug": "dms_scan_drop_zone", "row": 1, "col": 1,
         "size_override": "medium"},
        {"widget_slug": "dms_dictation_drop", "row": 1, "col": 2,
         "size_override": "medium"},
        {"widget_slug": "dms_scan_queue",     "row": 2, "col": 1,
         "size_override": "medium"},
        {"widget_slug": "dms_dictation_queue","row": 2, "col": 2,
         "size_override": "medium"},
    ])

    conn.execute(insert_sql, {
        "slug": "dms_home",
        "tab":  "capture",
        "pos":  positions,
    })


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("""
        DELETE FROM layout_registry
        WHERE layout_slug = 'dms_home' AND tenant_id = 'platform-default';
    """))
    conn.execute(text("""
        DELETE FROM widget_registry
        WHERE widget_slug IN (
            'dms_scan_drop_zone', 'dms_dictation_drop',
            'dms_scan_queue',     'dms_dictation_queue'
        ) AND tenant_id IS NULL;
    """))
