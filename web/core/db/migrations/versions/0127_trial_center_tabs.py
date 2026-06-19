"""trial_center nav tabs — registry-driven section tabs for the Trial Center matter home.

Seeds the platform-default rows in layout_tabs for layout_slug='trial_center', replacing
the hardcoded TABS array that used to live in src/pages/trial-center.jsx. All tabs are
in-page (target_type='tab_scope'); trial-center.jsx renders them via useNavTabs + TabBar
(right-aligned), the same pattern as matter_detail / appellate_home.

  - dashboard    (the landing panels: alerts / hearings & trials / calendar / tasks / projects)
  - pleadings
  - motions
  - depositions
  - hearings     (Hearing Transcripts)
  - exhibits     (Trial Exhibits)

Idempotent: skips rows that already exist (by layout_slug + tab_slug).

Revision ID: 0127_trial_center_tabs
Revises: 0126_documents_legal_category
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0127_trial_center_tabs"
down_revision = "0126_documents_legal_category"
branch_labels = None
depends_on = None

_LAYOUT = "trial_center"

# (tab_slug, display_name, order, icon)  -- all in-page (tab_scope)
_TABS = [
    ("dashboard",   "Dashboard",           1, "layout-dashboard"),
    ("pleadings",   "Pleadings",           2, "file-text"),
    ("motions",     "Motions",             3, "file-check"),
    ("depositions", "Depositions",         4, "message-square"),
    ("hearings",    "Hearing Transcripts", 5, "book-open"),
    ("exhibits",    "Trial Exhibits",      6, "briefcase"),
]


def upgrade() -> None:
    conn = op.get_bind()
    existing = {
        (r[0], r[1])
        for r in conn.execute(sa.text(
            "SELECT layout_slug, tab_slug FROM layout_tabs WHERE layout_slug = :ls"
        ), {"ls": _LAYOUT}).fetchall()
    }

    rows = []
    for slug, name, order, icon in _TABS:
        if (_LAYOUT, slug) in existing:
            continue
        rows.append({
            "id": str(uuid.uuid4()),
            "layout_slug": _LAYOUT,
            "tab_slug": slug,
            "display_name": name,
            "display_order": order,
            "icon": icon,
            "permission_level": "attorney",
            "is_visible": True,
            "target_type": "tab_scope",
            "target_ref": None,
            "is_platform_standard": True,
            "is_active": True,
        })

    if rows:
        conn.execute(sa.text("""
            INSERT INTO layout_tabs
                (id, layout_slug, tab_slug, display_name, display_order,
                 icon, permission_level, is_visible,
                 target_type, target_ref, config,
                 is_platform_standard, is_active)
            VALUES
                (:id, :layout_slug, :tab_slug, :display_name, :display_order,
                 :icon, :permission_level, :is_visible,
                 :target_type, :target_ref, CAST('{}' AS jsonb),
                 :is_platform_standard, :is_active)
        """), rows)


def downgrade() -> None:
    op.get_bind().execute(sa.text(
        "DELETE FROM layout_tabs WHERE layout_slug = :ls AND tab_slug IN "
        "('dashboard','pleadings','motions','depositions','hearings','exhibits') "
        "AND tenant_id IS NULL"
    ), {"ls": _LAYOUT})
