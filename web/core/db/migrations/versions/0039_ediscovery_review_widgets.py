"""0039_ediscovery_review_widgets

eDiscovery Review Workspace — widget + layout registration.

1. ADD COLUMN IF NOT EXISTS output_hash on ediscovery_production_documents
   (idempotent catch-up for tenants where the column was added by hand —
   HJMM already has it; future tenants provisioned from migrations need it).

2. ADD COLUMN IF NOT EXISTS output_manifest_hash on ediscovery_productions
   (same reason — column already present on HJMM, this makes the migration
   graph authoritative).

3. Seed 6 widget_registry rows for the review workspace:
     ediscovery_doc_list          (collection scope)
     ediscovery_doc_viewer        (document scope)
     ediscovery_doc_metadata      (document scope)
     ediscovery_doc_tags          (document scope)
     ediscovery_doc_review_status (document scope)
     ediscovery_doc_family        (document scope)

4. Seed 1 layout_registry row for 'ediscovery_review' layout (platform-default).

5. Seed 1 layout_tabs row for the single-tab review surface
   (single tab keeps the shell consistent with matter_detail pattern;
   future tabs — e.g. 'Analytics' — can be added later).

Revision ID: 0039_ediscovery_review_widgets
Revises: 0038_matter_overview_widgets
Create Date: 2026-04-17
"""
from alembic import op

revision = '0039_ediscovery_review_widgets'
down_revision = '0038_matter_overview_widgets'
branch_labels = None
depends_on = None


PLATFORM_DEFAULT_TENANT = 'platform-default                    '  # CHAR(36) padded


def upgrade():
    # ------------------------------------------------------------------
    # 1 + 2. Idempotent column catch-up for tenants where DDL was run
    # manually in-session. HJMM already has both columns; this no-ops there
    # and creates them on any tenant provisioned fresh from migrations.
    # ------------------------------------------------------------------
    op.execute("""
        ALTER TABLE ediscovery_production_documents
            ADD COLUMN IF NOT EXISTS output_hash CHAR(64)
    """)
    op.execute("""
        ALTER TABLE ediscovery_productions
            ADD COLUMN IF NOT EXISTS output_manifest_hash CHAR(64)
    """)

    # ------------------------------------------------------------------
    # 3. Widget registry seed — 6 review workspace widgets
    # ------------------------------------------------------------------
    op.execute("""
        INSERT INTO widget_registry
            (tenant_id, widget_slug, widget_name, category, widget_type,
             data_source, render_template, target_route,
             default_size, permission_level, is_platform_standard)
        VALUES
            (NULL, 'ediscovery_doc_list',
             'Document List', 'ediscovery', 'data_panel',
             'modules.ediscovery.services.review_widget_service.get_doc_list',
             'widgets/ediscovery_doc_list.html',
             NULL, 'large', 'attorney', true),

            (NULL, 'ediscovery_doc_viewer',
             'Document Viewer', 'ediscovery', 'data_panel',
             'modules.ediscovery.services.review_widget_service.get_doc_viewer',
             'widgets/ediscovery_doc_viewer.html',
             NULL, 'full-width', 'attorney', true),

            (NULL, 'ediscovery_doc_metadata',
             'Document Metadata', 'ediscovery', 'data_panel',
             'modules.ediscovery.services.review_widget_service.get_doc_metadata',
             'widgets/ediscovery_doc_metadata.html',
             NULL, 'medium', 'attorney', true),

            (NULL, 'ediscovery_doc_tags',
             'Document Tags', 'ediscovery', 'data_panel',
             'modules.ediscovery.services.review_widget_service.get_doc_tags',
             'widgets/ediscovery_doc_tags.html',
             NULL, 'medium', 'attorney', true),

            (NULL, 'ediscovery_doc_review_status',
             'Review Status', 'ediscovery', 'data_panel',
             'modules.ediscovery.services.review_widget_service.get_doc_review_status',
             'widgets/ediscovery_doc_review_status.html',
             NULL, 'medium', 'attorney', true),

            (NULL, 'ediscovery_doc_family',
             'Document Family', 'ediscovery', 'data_panel',
             'modules.ediscovery.services.review_widget_service.get_doc_family',
             'widgets/ediscovery_doc_family.html',
             NULL, 'medium', 'attorney', true)
        ON CONFLICT (widget_slug, COALESCE(tenant_id, '')) DO UPDATE SET
            widget_name      = EXCLUDED.widget_name,
            data_source      = EXCLUDED.data_source,
            render_template  = EXCLUDED.render_template,
            default_size     = EXCLUDED.default_size,
            widget_type      = EXCLUDED.widget_type
    """)

    # ------------------------------------------------------------------
    # 4. Layout registry — ediscovery_review default layout
    # widget_positions is a JSON array of {widget_slug, grid_area} entries.
    # The review workspace uses a CSS grid: left rail (doc list),
    # center (viewer), right rail (metadata/tags/status/family stacked).
    # ------------------------------------------------------------------
    op.execute(f"""
        INSERT INTO layout_registry
            (tenant_id, user_id, layout_slug, tab_slug,
             widget_positions, is_default)
        VALUES
            ('{PLATFORM_DEFAULT_TENANT}', NULL, 'ediscovery_review', 'review',
             CAST('[
                {{"widget_slug": "ediscovery_doc_list",           "grid_area": "left"}},
                {{"widget_slug": "ediscovery_doc_viewer",         "grid_area": "center"}},
                {{"widget_slug": "ediscovery_doc_tags",           "grid_area": "right-1"}},
                {{"widget_slug": "ediscovery_doc_review_status",  "grid_area": "right-2"}},
                {{"widget_slug": "ediscovery_doc_family",         "grid_area": "right-3"}},
                {{"widget_slug": "ediscovery_doc_metadata",       "grid_area": "right-4"}}
             ]' AS jsonb),
             true)
        ON CONFLICT (tenant_id, layout_slug, tab_slug, COALESCE(user_id::text, ''))
        DO UPDATE SET
            widget_positions = EXCLUDED.widget_positions,
            is_default       = EXCLUDED.is_default
    """)

    # ------------------------------------------------------------------
    # 5. Layout tabs — single 'review' tab on ediscovery_review surface
    # ------------------------------------------------------------------
    op.execute("""
        INSERT INTO layout_tabs
            (layout_slug, tab_slug, display_name, display_order,
             permission_level, is_visible, icon)
        VALUES
            ('ediscovery_review', 'review', 'Review', 1, 'attorney', true, 'search')
        ON CONFLICT (layout_slug, tab_slug) DO UPDATE SET
            display_name     = EXCLUDED.display_name,
            display_order    = EXCLUDED.display_order,
            permission_level = EXCLUDED.permission_level,
            icon             = EXCLUDED.icon
    """)


def downgrade():
    op.execute("DELETE FROM layout_tabs WHERE layout_slug = 'ediscovery_review'")
    op.execute("""
        DELETE FROM layout_registry
        WHERE layout_slug = 'ediscovery_review'
    """)
    op.execute("""
        DELETE FROM widget_registry
        WHERE widget_slug IN (
            'ediscovery_doc_list',
            'ediscovery_doc_viewer',
            'ediscovery_doc_metadata',
            'ediscovery_doc_tags',
            'ediscovery_doc_review_status',
            'ediscovery_doc_family'
        )
    """)
    # Do NOT drop the output_hash / output_manifest_hash columns on downgrade.
    # They are chain-of-custody columns — removing them during a rollback
    # could cascade-drop production hashes. Idempotent upgrade means
    # a no-op downgrade is the safe choice for these columns.
