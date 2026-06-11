"""Add dynamic rail support to ui_nav_items and seed platform defaults.

Revision ID: 0011_nav_rail
Revises: 0010_data_sources
Create Date: 2026-05-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_nav_rail"
down_revision = "0010_data_sources"
branch_labels = None
depends_on = None


def upgrade():
    # ── Add columns for dynamic rail ──
    op.add_column("ui_nav_items", sa.Column("tenant_id", sa.String(36), nullable=True))
    op.add_column("ui_nav_items", sa.Column("section", sa.String(32), nullable=False, server_default="main"))
    op.add_column("ui_nav_items", sa.Column("rail_label", sa.String(16), nullable=True))
    op.add_column("ui_nav_items", sa.Column("icon_svg", sa.Text(), nullable=True))
    op.add_column("ui_nav_items", sa.Column("badge_source", sa.String(255), nullable=True))
    op.add_column("ui_nav_items", sa.Column("context_scope", sa.String(64), nullable=True))
    op.add_column("ui_nav_items", sa.Column("page_key", sa.String(128), nullable=True))

    # ── Seed platform-default rail items ──
    op.execute("""
        INSERT INTO ui_nav_items
            (nav_key, label, rail_label, icon, icon_svg, url_path, parent_key,
             display_order, required_role, feature_flag, is_active,
             tenant_id, section, badge_source, context_scope, page_key)
        VALUES
        ('home', 'Home', 'Home',
         'home', '<path stroke-linecap="round" stroke-linejoin="round" d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6"/>',
         '/dashboard', NULL,
         100, NULL, NULL, TRUE,
         NULL, 'main', NULL, NULL, 'dashboard'),

        ('matters', 'Matters', 'Matters',
         'box', '<path stroke-linecap="round" stroke-linejoin="round" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/>',
         '/matters', NULL,
         200, NULL, NULL, TRUE,
         NULL, 'main', NULL, NULL, 'matters'),

        ('projects', 'Projects', 'Projects',
         'layout-kanban', '<path stroke-linecap="round" stroke-linejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"/>',
         '/projects', NULL,
         300, NULL, NULL, TRUE,
         NULL, 'main', NULL, NULL, 'projects'),

        ('documents', 'Documents', 'Docs',
         'folder', '<path stroke-linecap="round" stroke-linejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/>',
         '/dms', NULL,
         400, NULL, NULL, TRUE,
         NULL, 'main', NULL, NULL, 'dms'),

        ('div_billing', '', '', '', '', '', NULL,
         450, NULL, NULL, TRUE,
         NULL, 'divider', NULL, NULL, NULL),

        ('billing', 'Billing', 'Billing',
         'cash', '<path stroke-linecap="round" stroke-linejoin="round" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>',
         '/billing', NULL,
         500, NULL, NULL, TRUE,
         NULL, 'main', NULL, NULL, 'billing'),

        ('timesheets', 'Timesheets', 'Time',
         'clock', '<path stroke-linecap="round" stroke-linejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/>',
         '/billing/timesheet', NULL,
         600, NULL, NULL, TRUE,
         NULL, 'main', NULL, NULL, 'timesheets'),

        ('div_discovery', '', '', '', '', '', NULL,
         650, NULL, NULL, TRUE,
         NULL, 'divider', NULL, NULL, NULL),

        ('ediscovery', 'eDiscovery', 'eDisc',
         'search', '<path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>',
         '/ediscovery', NULL,
         700, NULL, NULL, TRUE,
         NULL, 'main', NULL, NULL, 'ediscovery'),

        ('div_work', '', '', '', '', '', NULL,
         750, NULL, NULL, TRUE,
         NULL, 'divider', NULL, NULL, NULL),

        ('drafting', 'Drafting', 'Draft',
         'edit', '<path stroke-linecap="round" stroke-linejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/>',
         '/drafting', NULL,
         800, NULL, NULL, TRUE,
         NULL, 'main', NULL, NULL, 'drafting'),

        ('calendar', 'Calendar', 'Calendar',
         'calendar', '<path stroke-linecap="round" stroke-linejoin="round" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/>',
         '/calendar', NULL,
         900, NULL, NULL, TRUE,
         NULL, 'main', NULL, NULL, 'calendar'),

        ('contacts', 'Contacts', 'Contacts',
         'user', '<path stroke-linecap="round" stroke-linejoin="round" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/>',
         '/contacts', NULL,
         1000, NULL, NULL, TRUE,
         NULL, 'main', NULL, NULL, 'contacts'),

        ('ai_assistant', 'AI Assistant', 'AI',
         'bulb', '<path stroke-linecap="round" stroke-linejoin="round" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/>',
         '#panel:ai', NULL,
         1100, NULL, NULL, TRUE,
         NULL, 'bottom', NULL, NULL, NULL),

        ('settings', 'Firm Settings', 'Settings',
         'settings', '<path stroke-linecap="round" stroke-linejoin="round" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>',
         '/tenant-admin/', NULL,
         1200, 'admin', NULL, TRUE,
         NULL, 'bottom', NULL, NULL, 'admin')
    """)


def downgrade():
    op.execute("DELETE FROM ui_nav_items WHERE tenant_id IS NULL")
    op.drop_column("ui_nav_items", "page_key")
    op.drop_column("ui_nav_items", "context_scope")
    op.drop_column("ui_nav_items", "badge_source")
    op.drop_column("ui_nav_items", "icon_svg")
    op.drop_column("ui_nav_items", "rail_label")
    op.drop_column("ui_nav_items", "section")
    op.drop_column("ui_nav_items", "tenant_id")
