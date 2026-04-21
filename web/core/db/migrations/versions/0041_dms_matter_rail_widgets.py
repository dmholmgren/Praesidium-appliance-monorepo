"""dms_matter_rail_widgets — checkout tracker, drafting tags, redline launcher stubs

Revision ID: 0041_dms_matter_rail_widgets
Revises: 0040_dms_capture_widgets
Create Date: 2026-04-18

Three new stub widgets for the DMS matter page right rail:
    dms_checkout_tracker  — document checkout and version history
    dms_drafting_tags     — cross-module tagging (review + work product sets)
    dms_redline_launcher  — Draftable/DocuSign comparison launcher

All three are placeholder type now. Each has a schema target comment in its
template and service function stub for when the backing is built.
dms_recent_documents already exists in registry — wired in right rail via
existing slug, no new row needed.
"""

from alembic import op
from sqlalchemy import text

revision = '0041_dms_matter_rail_widgets'
down_revision = '0040_dms_capture_widgets'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(text("""
        INSERT INTO widget_registry (
            widget_slug, widget_name, category, widget_type,
            data_source, render_template,
            default_size, permission_level,
            tenant_id, is_platform_standard
        ) VALUES
        (
            'dms_checkout_tracker',
            'Checkout & Versions',
            'dms', 'placeholder',
            NULL,
            'widgets/dms_checkout_tracker.html',
            'medium', 'attorney',
            NULL, TRUE
        ),
        (
            'dms_drafting_tags',
            'Drafting Tags',
            'dms', 'placeholder',
            NULL,
            'widgets/dms_drafting_tags.html',
            'medium', 'attorney',
            NULL, TRUE
        ),
        (
            'dms_redline_launcher',
            'Redline',
            'dms', 'launcher',
            NULL,
            'widgets/dms_redline_launcher.html',
            'small', 'attorney',
            NULL, TRUE
        )
        ON CONFLICT (widget_slug, COALESCE(tenant_id, ''::bpchar)) DO UPDATE
            SET widget_type          = EXCLUDED.widget_type,
                category             = EXCLUDED.category,
                render_template      = EXCLUDED.render_template,
                default_size         = EXCLUDED.default_size,
                is_platform_standard = EXCLUDED.is_platform_standard;
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("""
        DELETE FROM widget_registry
        WHERE widget_slug IN (
            'dms_checkout_tracker', 'dms_drafting_tags', 'dms_redline_launcher'
        ) AND tenant_id IS NULL;
    """))
