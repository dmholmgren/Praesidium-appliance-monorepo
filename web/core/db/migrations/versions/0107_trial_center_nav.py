"""Trial Center top-level nav item (Scope v2 §1 — data-driven nav).

Adds one platform-default ui_nav_items row so "Trial Center" appears in the
left rail (between Depositions and Billing), routing to /trial. Idempotent.

Revision ID: 0108_trial_center_nav
Revises: 0107_brief_workspace_tables
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0108_trial_center_nav"
down_revision = "0107_brief_workspace_tables"
branch_labels = None
depends_on = None

_NAV_KEY = "trial"
# scales-of-justice glyph (matches the stroke-based icon_svg convention).
_ICON_SVG = ('<path stroke-linecap="round" stroke-linejoin="round" '
             'd="M12 3v18m0-18l-6 2m6-2l6 2M6 5l-3 7a3 3 0 006 0L6 5zm12 0l-3 7'
             'a3 3 0 006 0l-3-7zM8 21h8"/>')


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
            (:id, :nav_key, 'Trial Center', 'gavel', '/trial', NULL, 760,
             NULL, NULL, TRUE, NULL, 'main',
             'Trial', :icon_svg, 'trial', '⚖️')
    """), {"id": str(uuid.uuid4()), "nav_key": _NAV_KEY, "icon_svg": _ICON_SVG})


def downgrade() -> None:
    op.get_bind().execute(sa.text(
        "DELETE FROM ui_nav_items WHERE nav_key = :k AND tenant_id IS NULL"
    ), {"k": _NAV_KEY})
