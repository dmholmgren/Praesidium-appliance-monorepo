"""Trial Center: add Analytics subtab + trim main-nav (remove Court & Files).

Two DB edits:
  1. Seed a 7th registry tab for layout_slug='trial_center' — 'analytics'
     (in-page tab_scope; trial-center.jsx renders AnalyticsPanel). Idempotent.
  2. Deactivate the 'court' (/court) and 'files_panel' (#panel:files) rows in
     ui_nav_items so they drop out of the left rail. Platform rows (tenant_id
     NULL). Reversible in downgrade.

Revision ID: 0131_trial_analytics_nav_trim
Revises: 0130_evidence_for_against
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0131_trial_analytics_nav_trim"
down_revision = "0130_evidence_for_against"
branch_labels = None
depends_on = None

_LAYOUT = "trial_center"
_NAV_REMOVE = ("court", "files_panel")


def upgrade() -> None:
    conn = op.get_bind()

    # 1. analytics tab (skip if already present)
    exists = conn.execute(sa.text(
        "SELECT 1 FROM layout_tabs WHERE layout_slug = :ls AND tab_slug = 'analytics'"
    ), {"ls": _LAYOUT}).fetchone()
    if not exists:
        conn.execute(sa.text("""
            INSERT INTO layout_tabs
                (id, layout_slug, tab_slug, display_name, display_order,
                 icon, permission_level, is_visible,
                 target_type, target_ref, config,
                 is_platform_standard, is_active)
            VALUES
                (:id, :ls, 'analytics', 'Analytics', 7,
                 'bar-chart', 'attorney', true,
                 'tab_scope', NULL, CAST('{}' AS jsonb),
                 true, true)
        """), {"id": str(uuid.uuid4()), "ls": _LAYOUT})

    # 2. trim the main left nav
    conn.execute(sa.text(
        "UPDATE ui_nav_items SET is_active = false "
        "WHERE nav_key IN :keys AND tenant_id IS NULL"
    ).bindparams(sa.bindparam("keys", expanding=True)), {"keys": list(_NAV_REMOVE)})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(
        "DELETE FROM layout_tabs WHERE layout_slug = :ls AND tab_slug = 'analytics' "
        "AND tenant_id IS NULL"
    ), {"ls": _LAYOUT})
    conn.execute(sa.text(
        "UPDATE ui_nav_items SET is_active = true "
        "WHERE nav_key IN :keys AND tenant_id IS NULL"
    ).bindparams(sa.bindparam("keys", expanding=True)), {"keys": list(_NAV_REMOVE)})
