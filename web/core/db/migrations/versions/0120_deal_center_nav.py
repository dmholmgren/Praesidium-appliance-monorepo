"""Deal Center top-level nav item (Unit A — data-driven nav).

Adds one platform-default ui_nav_items row so "Deal Center" appears in the
left rail (between Matters and Workflows & Projects), routing to /deals — the
transactional-matters landing. Idempotent.

Revision ID: 0120_deal_center_nav
Revises: 0119_seeding_sessions
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0120_deal_center_nav"
down_revision = "0119_seeding_sessions"
branch_labels = None
depends_on = None

_NAV_KEY = "dealcenter"
# briefcase glyph (stroke-based, matches the icon_svg convention).
_ICON_SVG = ('<path stroke-linecap="round" stroke-linejoin="round" '
             'd="M3 8.5h18v10.5a1.5 1.5 0 01-1.5 1.5h-15A1.5 1.5 0 013 19V8.5z'
             'M8 8.5V6a2 2 0 012-2h4a2 2 0 012 2v2.5M3 13h18"/>')


def upgrade() -> None:
    conn = op.get_bind()
    exists = conn.execute(sa.text(
        "SELECT 1 FROM ui_nav_items WHERE nav_key = :k AND tenant_id IS NULL"
    ), {"k": _NAV_KEY}).fetchone()
    if exists:
        return
    conn.execute(sa.text("""
        INSERT INTO ui_nav_items
            (id, nav_key, label, icon, url_path, parent_key, display_order,
             required_role, feature_flag, is_active, tenant_id, section,
             rail_label, icon_svg, page_key, icon_emoji)
        VALUES
            (:id, :nav_key, 'Deal Center', 'briefcase', '/deals', NULL, 350,
             NULL, NULL, TRUE, NULL, 'main',
             'Deals', :icon_svg, 'dealcenter', '\U0001F91D')
    """), {"id": str(uuid.uuid4()), "nav_key": _NAV_KEY, "icon_svg": _ICON_SVG})


def downgrade() -> None:
    op.get_bind().execute(sa.text(
        "DELETE FROM ui_nav_items WHERE nav_key = :k AND tenant_id IS NULL"
    ), {"k": _NAV_KEY})
