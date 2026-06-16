"""appellate_home nav tabs — registry-driven section tabs for the appellate matter homepage.

Seeds three platform-default rows in layout_tabs for layout_slug='appellate_home':
  - overview (in-page tab; the 6-card Overview grid)
  - briefs   (navigate -> split-screen brief editor /appellate/brief/{appellate_case_id})
  - record   (navigate -> transcript viewer /depositions/transcript/{rr_transcript_id})

briefs/record carry per-matter ids (appellate_case_id, rr_transcript_id) that are NOT
expressible via the target_ref {matter_id} template, so the AppellateHomepage resolves
the final URL client-side by tab_slug from its dashboard payload. target_ref stores the
base path for documentation/fallback only.

Idempotent: skips rows that already exist (by layout_slug + tab_slug).

Revision ID: 0102_appellate_home_tabs
Revises: 0101_exhibit_usages
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0102_appellate_home_tabs"
down_revision = "0101_exhibit_usages"
branch_labels = None
depends_on = None

_LAYOUT = "appellate_home"

# (tab_slug, display_name, order, icon, target_type, target_ref)
_TABS = [
    ("overview", "Overview", 1, "layout-dashboard", "tab_scope", None),
    ("briefs",   "Briefs",   2, "edit",             "navigate",  "/appellate/brief/"),
    ("record",   "Record",   3, "book-open",        "navigate",  "/depositions/transcript/"),
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
    for slug, name, order, icon, ttype, tref in _TABS:
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
            "target_type": ttype,
            "target_ref": tref,
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
        "('overview','briefs','record') AND tenant_id IS NULL"
    ), {"ls": _LAYOUT})
