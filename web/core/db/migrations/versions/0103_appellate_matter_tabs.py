"""Fold appellate Briefs/Record into the shared matter_detail tab bar.

Supersedes 0102's standalone 'appellate_home' layout: the appellate homepage now
uses the same top tab bar as every other matter page (consistency). This:
  - removes the unused platform-default 'appellate_home' layout_tabs rows
  - adds 'briefs' and 'record' to layout_slug='matter_detail', restricted to
    matter_types={appellate}, so they appear in the standard top bar only for
    appellate matters (Overview already exists in matter_detail).

briefs/record are navigate tabs whose final URLs (brief editor /appellate/brief/{cid};
transcript viewer /depositions/transcript/{rr_transcript_id}) carry per-matter ids not
expressible via the target_ref {matter_id} template, so MatterDashboard resolves them
client-side from the appellate dashboard payload. target_ref stores the base path only.

Idempotent.

Revision ID: 0103_appellate_matter_tabs
Revises: 0102_appellate_home_tabs
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0103_appellate_matter_tabs"
down_revision = "0102_appellate_home_tabs"
branch_labels = None
depends_on = None

# (tab_slug, display_name, order, icon, target_ref)
_MD_TABS = [
    ("briefs", "Briefs", 2, "edit",      "/appellate/brief/"),
    ("record", "Record", 3, "book-open", "/depositions/transcript/"),
]


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Drop the now-unused standalone appellate_home layout.
    conn.execute(sa.text(
        "DELETE FROM layout_tabs WHERE layout_slug = 'appellate_home' AND tenant_id IS NULL"
    ))

    # 2. Add Briefs/Record to the shared matter_detail bar, appellate-only.
    existing = {
        r[0] for r in conn.execute(sa.text(
            "SELECT tab_slug FROM layout_tabs "
            "WHERE layout_slug = 'matter_detail' AND tab_slug IN ('briefs','record') "
            "  AND tenant_id IS NULL"
        )).fetchall()
    }
    for slug, name, order, icon, tref in _MD_TABS:
        if slug in existing:
            continue
        conn.execute(sa.text("""
            INSERT INTO layout_tabs
                (id, layout_slug, tab_slug, display_name, display_order,
                 icon, permission_level, is_visible,
                 target_type, target_ref, config,
                 is_platform_standard, is_active, matter_types)
            VALUES
                (:id, 'matter_detail', :tab_slug, :display_name, :display_order,
                 :icon, 'attorney', TRUE,
                 'navigate', :target_ref, CAST('{}' AS jsonb),
                 TRUE, TRUE, ARRAY['appellate']::varchar[])
        """), {
            "id": str(uuid.uuid4()),
            "tab_slug": slug,
            "display_name": name,
            "display_order": order,
            "icon": icon,
            "target_ref": tref,
        })


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(
        "DELETE FROM layout_tabs WHERE layout_slug = 'matter_detail' "
        "AND tab_slug IN ('briefs','record') AND tenant_id IS NULL"
    ))
    # Re-seed the standalone appellate_home layout (mirror of 0102).
    for slug, name, order, icon, ttype, tref in [
        ("overview", "Overview", 1, "layout-dashboard", "tab_scope", None),
        ("briefs",   "Briefs",   2, "edit",             "navigate",  "/appellate/brief/"),
        ("record",   "Record",   3, "book-open",        "navigate",  "/depositions/transcript/"),
    ]:
        conn.execute(sa.text("""
            INSERT INTO layout_tabs
                (id, layout_slug, tab_slug, display_name, display_order,
                 icon, permission_level, is_visible,
                 target_type, target_ref, config, is_platform_standard, is_active)
            VALUES
                (:id, 'appellate_home', :tab_slug, :display_name, :display_order,
                 :icon, 'attorney', TRUE,
                 :target_type, :target_ref, CAST('{}' AS jsonb), TRUE, TRUE)
        """), {
            "id": str(uuid.uuid4()), "tab_slug": slug, "display_name": name,
            "display_order": order, "icon": icon, "target_type": ttype, "target_ref": tref,
        })
