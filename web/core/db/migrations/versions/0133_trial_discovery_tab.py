"""trial_center Discovery tab — registry-driven e-discovery section tab.

Adds a 'discovery' tab to layout_slug='trial_center' (between Motions and
Depositions) and renumbers the trailing tabs so the document corpora read
Pleadings · Motions · Discovery, then Depositions · Hearing Transcripts ·
Trial Exhibits · Analytics. The Discovery tab is rendered by trial-center.jsx
(DocWorkspace kind='discovery') as a projection of the matter's e-discovery
substrate (ediscovery_collections + ediscovery_documents).

Idempotent: skips the discovery insert if it already exists; the renumber is a
plain UPDATE keyed on the platform-standard rows (tenant_id IS NULL).

Revision ID: 0133_trial_discovery_tab
Revises: 0132_depo_video_status
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0133_trial_discovery_tab"
down_revision = "0132_depo_video_status"
branch_labels = None
depends_on = None

_LAYOUT = "trial_center"

# desired final ordering (platform-standard rows only)
_ORDER = {
    "dashboard": 1, "pleadings": 2, "motions": 3, "discovery": 4,
    "depositions": 5, "hearings": 6, "exhibits": 7, "analytics": 8,
}


def upgrade() -> None:
    conn = op.get_bind()

    # 1. insert the Discovery tab if absent
    exists = conn.execute(sa.text(
        "SELECT 1 FROM layout_tabs WHERE layout_slug = :ls AND tab_slug = 'discovery' "
        "AND tenant_id IS NULL"
    ), {"ls": _LAYOUT}).first()
    if not exists:
        conn.execute(sa.text("""
            INSERT INTO layout_tabs
                (id, layout_slug, tab_slug, display_name, display_order,
                 icon, permission_level, is_visible,
                 target_type, target_ref, config,
                 is_platform_standard, is_active)
            VALUES
                (:id, :ls, 'discovery', 'Discovery', 4,
                 'inbox', 'attorney', true,
                 'tab_scope', NULL, CAST('{}' AS jsonb),
                 true, true)
        """), {"id": str(uuid.uuid4()), "ls": _LAYOUT})

    # 2. renumber to keep the corpora contiguous
    for slug, order in _ORDER.items():
        conn.execute(sa.text(
            "UPDATE layout_tabs SET display_order = :o "
            "WHERE layout_slug = :ls AND tab_slug = :s AND tenant_id IS NULL"
        ), {"o": order, "ls": _LAYOUT, "s": slug})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(
        "DELETE FROM layout_tabs WHERE layout_slug = :ls AND tab_slug = 'discovery' "
        "AND tenant_id IS NULL"
    ), {"ls": _LAYOUT})
    # restore the pre-discovery ordering
    for slug, order in {
        "dashboard": 1, "pleadings": 2, "motions": 3, "depositions": 4,
        "hearings": 5, "exhibits": 6, "analytics": 7,
    }.items():
        conn.execute(sa.text(
            "UPDATE layout_tabs SET display_order = :o "
            "WHERE layout_slug = :ls AND tab_slug = :s AND tenant_id IS NULL"
        ), {"o": order, "ls": _LAYOUT, "s": slug})
