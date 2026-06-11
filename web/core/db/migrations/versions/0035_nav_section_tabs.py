"""0035_nav_section_tabs — Evolve layout_tabs into nav section tab registry.

Adds tenant/role scoping, target resolution, icon SVGs, feature flags,
and config JSONB to layout_tabs. Seeds section tabs for all surfaces.
Same architectural pattern as context_menu_actions (0034) and
ui_nav_items — data-driven, tenant-overridable, role-gated.

Revision ID: 0035_nav_section_tabs
Revises: 0034_context_menu_reg
Create Date: 2026-05-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
import uuid

# revision identifiers
revision = '0035_nav_section_tabs'
down_revision = '0034_context_menu_reg'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Add new columns to layout_tabs ──────────────────────────────────

    op.add_column('layout_tabs', sa.Column(
        'tenant_id', sa.String(36), nullable=True,
        comment='NULL = platform default; populated = tenant override'))

    op.add_column('layout_tabs', sa.Column(
        'target_type', sa.String(32), nullable=False,
        server_default='tab_scope',
        comment='tab_scope | navigate | widget_layout | api_endpoint | external_url'))

    op.add_column('layout_tabs', sa.Column(
        'target_ref', sa.String(512), nullable=True,
        comment='URL template, widget_layouts.id, or API endpoint'))

    op.add_column('layout_tabs', sa.Column(
        'icon_svg', sa.Text(), nullable=True,
        comment='Inline SVG path data for shell rendering'))

    op.add_column('layout_tabs', sa.Column(
        'required_role', sa.String(32), nullable=True,
        comment='Minimum role to see this tab (NULL = everyone)'))

    op.add_column('layout_tabs', sa.Column(
        'feature_flag', sa.String(64), nullable=True,
        comment='Feature flag gate from tenant_licenses.feature_flags'))

    op.add_column('layout_tabs', sa.Column(
        'is_platform_standard', sa.Boolean(), nullable=False,
        server_default='true',
        comment='TRUE = shipped with platform, FALSE = tenant-created'))

    op.add_column('layout_tabs', sa.Column(
        'is_active', sa.Boolean(), nullable=False,
        server_default='true'))

    op.add_column('layout_tabs', sa.Column(
        'config', JSONB(), nullable=True, server_default='{}',
        comment='Tab-specific config (query params, widget overrides, etc.)'))

    op.add_column('layout_tabs', sa.Column(
        'separator_before', sa.Boolean(), nullable=False,
        server_default='false',
        comment='Draw a visual separator before this tab'))

    op.add_column('layout_tabs', sa.Column(
        'badge_source', sa.String(128), nullable=True,
        comment='API endpoint or query for live badge count'))

    op.add_column('layout_tabs', sa.Column(
        'updated_at', sa.DateTime(timezone=True), nullable=True,
        server_default=sa.text('now()')))

    # ── 2. Create user_tab_prefs for per-user tab customization ────────────

    op.create_table(
        'user_tab_prefs',
        sa.Column('id', sa.dialects.postgresql.UUID(), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('layout_slug', sa.String(128), nullable=False),
        sa.Column('tab_slug', sa.String(128), nullable=False),
        sa.Column('is_hidden', sa.Boolean(), nullable=False,
                  server_default='false'),
        sa.Column('custom_order', sa.Integer(), nullable=True),
        sa.Column('is_pinned', sa.Boolean(), nullable=False,
                  server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True,
                  server_default=sa.text('now()')),
    )
    op.create_unique_constraint(
        'uq_user_tab_prefs_user_layout_tab',
        'user_tab_prefs',
        ['tenant_id', 'user_id', 'layout_slug', 'tab_slug']
    )
    op.create_index('ix_user_tab_prefs_tenant_user',
                     'user_tab_prefs', ['tenant_id', 'user_id'])

    # ── 3. Add unique constraint to layout_tabs ────────────────────────────
    # Prevent duplicate slugs within the same layout scope + tenant
    # Existing rows have tenant_id NULL — this is fine for the constraint

    op.create_index(
        'ix_layout_tabs_layout_tab_tenant',
        'layout_tabs',
        ['layout_slug', 'tab_slug', 'tenant_id'],
        unique=False  # not unique because tenant overrides coexist with platform defaults
    )

    # ── 4. Seed section tabs for all current surfaces ──────────────────────
    _seed_tabs()


def downgrade() -> None:
    op.drop_index('ix_layout_tabs_layout_tab_tenant', 'layout_tabs')
    op.drop_index('ix_user_tab_prefs_tenant_user', 'user_tab_prefs')
    op.drop_constraint('uq_user_tab_prefs_user_layout_tab', 'user_tab_prefs')
    op.drop_table('user_tab_prefs')

    for col in ['tenant_id', 'target_type', 'target_ref', 'icon_svg',
                'required_role', 'feature_flag', 'is_platform_standard',
                'is_active', 'config', 'separator_before', 'badge_source',
                'updated_at']:
        op.drop_column('layout_tabs', col)


# ───────────────────────────────────────────────────────────────────────────
#  Seed Data
# ───────────────────────────────────────────────────────────────────────────

# Lucide SVG paths — consistent with context_menu_actions icon naming
_ICONS = {
    'building': '<path stroke-linecap="round" stroke-linejoin="round" d="M6 22V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v18Z"/><path stroke-linecap="round" stroke-linejoin="round" d="M6 12H4a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h2"/><path stroke-linecap="round" stroke-linejoin="round" d="M18 9h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-2"/><path stroke-linecap="round" stroke-linejoin="round" d="M10 6h4"/><path stroke-linecap="round" stroke-linejoin="round" d="M10 10h4"/><path stroke-linecap="round" stroke-linejoin="round" d="M10 14h4"/><path stroke-linecap="round" stroke-linejoin="round" d="M10 18h4"/>',
    'user': '<path stroke-linecap="round" stroke-linejoin="round" d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    'briefcase': '<rect width="20" height="14" x="2" y="7" rx="2" ry="2"/><path stroke-linecap="round" stroke-linejoin="round" d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>',
    'layout-dashboard': '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
    'search': '<circle cx="11" cy="11" r="8"/><path stroke-linecap="round" stroke-linejoin="round" d="m21 21-4.3-4.3"/>',
    'file-text': '<path stroke-linecap="round" stroke-linejoin="round" d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/><line x1="16" x2="8" y1="13" y2="13"/><line x1="16" x2="8" y1="17" y2="17"/><line x1="10" x2="8" y1="9" y2="9"/>',
    'receipt': '<path stroke-linecap="round" stroke-linejoin="round" d="M4 2v20l2-1 2 1 2-1 2 1 2-1 2 1 2-1 2 1V2l-2 1-2-1-2 1-2-1-2 1-2-1-2 1Z"/><path stroke-linecap="round" stroke-linejoin="round" d="M16 8h-6a2 2 0 1 0 0 4h4a2 2 0 1 1 0 4H8"/><path stroke-linecap="round" stroke-linejoin="round" d="M12 17.5v-11"/>',
    'scale': '<path stroke-linecap="round" stroke-linejoin="round" d="m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/><path stroke-linecap="round" stroke-linejoin="round" d="m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/><path stroke-linecap="round" stroke-linejoin="round" d="M7 21h10"/><path stroke-linecap="round" stroke-linejoin="round" d="M12 3v18"/><path stroke-linecap="round" stroke-linejoin="round" d="M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2"/>',
    'folder': '<path stroke-linecap="round" stroke-linejoin="round" d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    'inbox': '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path stroke-linecap="round" stroke-linejoin="round" d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    'settings': '<path stroke-linecap="round" stroke-linejoin="round" d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
    'chart-bar': '<line x1="12" x2="12" y1="20" y2="10"/><line x1="18" x2="18" y1="20" y2="4"/><line x1="6" x2="6" y1="20" y2="16"/>',
    'clock': '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    'shield': '<path stroke-linecap="round" stroke-linejoin="round" d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10"/>',
    'users': '<path stroke-linecap="round" stroke-linejoin="round" d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path stroke-linecap="round" stroke-linejoin="round" d="M22 21v-2a4 4 0 0 0-3-3.87"/><path stroke-linecap="round" stroke-linejoin="round" d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    'credit-card': '<rect width="20" height="14" x="2" y="5" rx="2"/><line x1="2" x2="22" y1="10" y2="10"/>',
    'file-check': '<path stroke-linecap="round" stroke-linejoin="round" d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/><path stroke-linecap="round" stroke-linejoin="round" d="m9 15 2 2 4-4"/>',
    'list-checks': '<path stroke-linecap="round" stroke-linejoin="round" d="m3 17 2 2 4-4"/><path stroke-linecap="round" stroke-linejoin="round" d="m3 7 2 2 4-4"/><path stroke-linecap="round" stroke-linejoin="round" d="M13 6h8"/><path stroke-linecap="round" stroke-linejoin="round" d="M13 12h8"/><path stroke-linecap="round" stroke-linejoin="round" d="M13 18h8"/>',
    'package': '<path stroke-linecap="round" stroke-linejoin="round" d="m7.5 4.27 9 5.15"/><path stroke-linecap="round" stroke-linejoin="round" d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path stroke-linecap="round" stroke-linejoin="round" d="m3.3 7 8.7 5 8.7-5"/><path stroke-linecap="round" stroke-linejoin="round" d="M12 22V12"/>',
    'mail': '<rect width="20" height="16" x="2" y="4" rx="2"/><path stroke-linecap="round" stroke-linejoin="round" d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
    'calendar': '<rect width="18" height="18" x="3" y="4" rx="2" ry="2"/><line x1="16" x2="16" y1="2" y2="6"/><line x1="8" x2="8" y1="2" y2="6"/><line x1="3" x2="21" y1="10" y2="10"/>',
    'eye': '<path stroke-linecap="round" stroke-linejoin="round" d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
    'brain': '<path stroke-linecap="round" stroke-linejoin="round" d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z"/><path stroke-linecap="round" stroke-linejoin="round" d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z"/>',
    'database': '<ellipse cx="12" cy="5" rx="9" ry="3"/><path stroke-linecap="round" stroke-linejoin="round" d="M3 5V19A9 3 0 0 0 21 19V5"/><path stroke-linecap="round" stroke-linejoin="round" d="M3 12A9 3 0 0 0 21 12"/>',
    'upload': '<path stroke-linecap="round" stroke-linejoin="round" d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/>',
    'gavel': '<path stroke-linecap="round" stroke-linejoin="round" d="m14 13-7.5 7.5c-.83.83-2.17.83-3 0 0 0 0 0 0 0a2.12 2.12 0 0 1 0-3L11 10"/><path stroke-linecap="round" stroke-linejoin="round" d="m16 16 6 6"/><path stroke-linecap="round" stroke-linejoin="round" d="m8 8 8-8"/><path stroke-linecap="round" stroke-linejoin="round" d="m11.5 11.5 5-5"/>',
    'landmark': '<line x1="3" x2="3" y1="22" y2="12"/><line x1="9" x2="9" y1="22" y2="12"/><line x1="15" x2="15" y1="22" y2="12"/><line x1="21" x2="21" y1="22" y2="12"/><path stroke-linecap="round" stroke-linejoin="round" d="M2 22h20"/><path stroke-linecap="round" stroke-linejoin="round" d="m12 2 10 5H2Z"/>',
    'target': '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
    'file-search': '<path stroke-linecap="round" stroke-linejoin="round" d="M14 2v4a2 2 0 0 0 2 2h4"/><path stroke-linecap="round" stroke-linejoin="round" d="M4.268 21a2 2 0 0 0 1.727 1H18a2 2 0 0 0 2-2V7l-5-5H6a2 2 0 0 0-2 2v3"/><path stroke-linecap="round" stroke-linejoin="round" d="m9 18-1.5-1.5"/><circle cx="5" cy="14" r="3"/>',
    'truck': '<path stroke-linecap="round" stroke-linejoin="round" d="M14 18V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v11a1 1 0 0 0 1 1h2"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 18h2a1 1 0 0 0 1-1v-3.65a1 1 0 0 0-.22-.624l-3.48-4.35A1 1 0 0 0 13.52 8H14"/><circle cx="17" cy="18" r="2"/><circle cx="7" cy="18" r="2"/>',
    'bar-chart': '<line x1="12" x2="12" y1="20" y2="10"/><line x1="18" x2="18" y1="20" y2="4"/><line x1="6" x2="6" y1="20" y2="16"/>',
}


def _seed_tabs():
    """Seed section tabs. Skip any that already exist (by layout_slug + tab_slug)."""
    conn = op.get_bind()

    # Check existing tabs to avoid duplicates
    existing = set()
    for row in conn.execute(sa.text(
        "SELECT layout_slug, tab_slug FROM layout_tabs"
    )):
        existing.add((row[0], row[1]))

    tabs = []

    # ── Practice Intelligence Dashboard ────────────────────────────────────
    # Already seeded: firm_view, attorney_view, matter_view, my_view
    # Update existing rows with new column values
    _update_existing(conn, 'practice_intelligence', 'firm_view',
                     icon='building', icon_svg=_ICONS['building'],
                     target_type='tab_scope',
                     config='{"scope": "tenant"}')
    _update_existing(conn, 'practice_intelligence', 'attorney_view',
                     icon='user', icon_svg=_ICONS['user'],
                     target_type='tab_scope',
                     config='{"scope": "attorney"}')
    _update_existing(conn, 'practice_intelligence', 'matter_view',
                     icon='briefcase', icon_svg=_ICONS['briefcase'],
                     target_type='tab_scope',
                     config='{"scope": "matter"}')
    _update_existing(conn, 'practice_intelligence', 'my_view',
                     icon='layout-dashboard', icon_svg=_ICONS['layout-dashboard'],
                     target_type='tab_scope',
                     config='{"scope": "user"}')

    # ── Matter Detail (litigation) ─────────────────────────────────────────
    # Already seeded: overview, matter_intelligence, billing, documents
    _update_existing(conn, 'matter_detail', 'overview',
                     icon='eye', icon_svg=_ICONS['eye'],
                     target_type='tab_scope',
                     config='{"scope": "overview"}')
    _update_existing(conn, 'matter_detail', 'matter_intelligence',
                     icon='brain', icon_svg=_ICONS['brain'],
                     target_type='tab_scope',
                     config='{"scope": "intelligence"}')
    _update_existing(conn, 'matter_detail', 'billing',
                     icon='receipt', icon_svg=_ICONS['receipt'],
                     target_type='navigate',
                     target_ref='/billing/clients/{client_id}/matters/{matter_id}?from=matter')
    _update_existing(conn, 'matter_detail', 'documents',
                     icon='folder', icon_svg=_ICONS['folder'],
                     target_type='navigate',
                     target_ref='/dms/matter/{matter_id}?from=matter')

    # New matter detail tabs
    for slug, name, order, icon, ttype, tref, cfg in [
        ('ediscovery', 'eDiscovery', 5, 'search',
         'navigate', '/ediscovery/matters/{matter_id}/?from=matter',
         '{"feature_flag": "feature_ediscovery"}'),
        ('witnesses', 'Witnesses', 6, 'users',
         'tab_scope', None, '{"scope": "witnesses"}'),
        ('deadlines', 'Deadlines & Calendar', 7, 'calendar',
         'tab_scope', None, '{"scope": "deadlines"}'),
        ('causes_of_action', 'Causes of Action', 8, 'scale',
         'tab_scope', None, '{"scope": "causes_of_action"}'),
        ('communications', 'Communications', 9, 'mail',
         'tab_scope', None, '{"scope": "communications"}'),
        ('analytics', 'Analytics', 10, 'bar-chart',
         'tab_scope', None, '{"scope": "analytics"}'),
    ]:
        if ('matter_detail', slug) not in existing:
            tabs.append(_tab('matter_detail', slug, name, order, icon,
                             _ICONS.get(icon, ''), ttype, tref, cfg))

    # ── Billing Section ────────────────────────────────────────────────────
    for slug, name, order, icon, ttype, tref in [
        ('overview', 'Overview', 1, 'receipt', 'tab_scope', None),
        ('clients', 'Clients', 2, 'users', 'tab_scope', None),
        ('timekeepers', 'Timekeepers', 3, 'clock', 'tab_scope', None),
        ('bill_runs', 'Bill Runs', 4, 'file-check', 'tab_scope', None),
        ('trust', 'Trust Accounts', 5, 'shield', 'tab_scope', None),
        ('reports', 'Reports', 6, 'bar-chart', 'tab_scope', None),
        ('templates', 'Templates', 7, 'file-text', 'tab_scope', None),
        ('settings', 'Settings', 8, 'settings', 'tab_scope', None),
    ]:
        if ('billing', slug) not in existing:
            tabs.append(_tab('billing', slug, name, order, icon,
                             _ICONS.get(icon, ''), ttype, tref))

    # ── eDiscovery Section ─────────────────────────────────────────────────
    for slug, name, order, icon, ttype, tref in [
        ('overview', 'Overview', 1, 'layout-dashboard', 'tab_scope', None),
        ('collections', 'Collections', 2, 'inbox', 'tab_scope', None),
        ('imports', 'File Import', 3, 'upload', 'tab_scope', None),
        ('review', 'Review', 4, 'search', 'tab_scope', None),
        ('productions', 'Productions', 5, 'package', 'tab_scope', None),
        ('search', 'Search', 6, 'file-search', 'tab_scope', None),
        ('exports', 'Exports', 7, 'truck', 'tab_scope', None),
    ]:
        if ('ediscovery', slug) not in existing:
            tabs.append(_tab('ediscovery', slug, name, order, icon,
                             _ICONS.get(icon, ''), ttype, tref))

    # ── eDiscovery Review (already seeded: review) ─────────────────────────
    _update_existing(conn, 'ediscovery_review', 'review',
                     icon='search', icon_svg=_ICONS['search'],
                     target_type='tab_scope')

    # ── DMS Section ────────────────────────────────────────────────────────
    for slug, name, order, icon, ttype, tref in [
        ('browse', 'Browse', 1, 'folder', 'tab_scope', None),
        ('search', 'Search', 2, 'file-search', 'tab_scope', None),
        ('recent', 'Recent', 3, 'clock', 'tab_scope', None),
    ]:
        if ('dms', slug) not in existing:
            tabs.append(_tab('dms', slug, name, order, icon,
                             _ICONS.get(icon, ''), ttype, tref))

    # ── Communications Section ─────────────────────────────────────────────
    for slug, name, order, icon, ttype, tref in [
        ('inbox', 'Inbox', 1, 'inbox', 'tab_scope', None),
        ('calendar', 'Calendar', 2, 'calendar', 'tab_scope', None),
        ('contacts', 'Contacts', 3, 'users', 'tab_scope', None),
    ]:
        if ('communications', slug) not in existing:
            tabs.append(_tab('communications', slug, name, order, icon,
                             _ICONS.get(icon, ''), ttype, tref))

    # ── Tenant Admin Section ───────────────────────────────────────────────
    for slug, name, order, icon, ttype, tref in [
        ('overview', 'Overview', 1, 'layout-dashboard', 'tab_scope', None),
        ('file_import', 'File Import', 2, 'upload', 'tab_scope', None),
        ('connectors', 'Connectors', 3, 'database', 'tab_scope', None),
        ('users', 'Users', 4, 'users', 'tab_scope', None),
        ('ai_settings', 'AI Settings', 5, 'brain', 'tab_scope', None),
        ('platform', 'Platform Control', 6, 'settings', 'tab_scope', None),
        ('settings', 'Settings', 7, 'settings', 'tab_scope', None),
    ]:
        if ('tenant_admin', slug) not in existing:
            tabs.append(_tab('tenant_admin', slug, name, order, icon,
                             _ICONS.get(icon, ''), ttype, tref,
                             required_role='admin'))

    # ── Insert new tabs ────────────────────────────────────────────────────
    if tabs:
        conn.execute(sa.text("""
            INSERT INTO layout_tabs
                (id, layout_slug, tab_slug, display_name, display_order,
                 icon, icon_svg, permission_level, is_visible,
                 target_type, target_ref, config,
                 required_role, is_platform_standard, is_active)
            VALUES
                (:id, :layout_slug, :tab_slug, :display_name, :display_order,
                 :icon, :icon_svg, :permission_level, :is_visible,
                 :target_type, :target_ref, CAST(:config AS jsonb),
                 :required_role, :is_platform_standard, :is_active)
        """), tabs)


def _tab(layout, slug, name, order, icon, icon_svg, ttype, tref=None,
         config=None, required_role=None, feature_flag=None):
    return {
        'id': str(uuid.uuid4()),
        'layout_slug': layout,
        'tab_slug': slug,
        'display_name': name,
        'display_order': order,
        'icon': icon,
        'icon_svg': icon_svg,
        'permission_level': 'attorney',
        'is_visible': True,
        'target_type': ttype,
        'target_ref': tref,
        'config': config or '{}',
        'required_role': required_role,
        'is_platform_standard': True,
        'is_active': True,
    }


def _update_existing(conn, layout_slug, tab_slug, **kwargs):
    """Update existing layout_tabs rows with new column values."""
    sets = []
    params = {'ls': layout_slug, 'ts': tab_slug}
    for k, v in kwargs.items():
        sets.append(f"{k} = :{k}")
        params[k] = v
    if sets:
        conn.execute(sa.text(
            f"UPDATE layout_tabs SET {', '.join(sets)} "
            f"WHERE layout_slug = :ls AND tab_slug = :ts"
        ), params)
