"""0110: rename the Depositions nav item to "Transcripts" and give it an icon.

The global nav rail (shell.html) renders ONLY icon_svg — the depositions row
was inserted (0093) with just icon_emoji, so it showed a blank icon in the rail.
This sets a heroicons document-text outline path for icon_svg, renames the
label/rail_label to "Transcripts", and swaps the 🎤 emoji to 📄 for the
emoji-first renderers (React nav-icon-strip / mobile). nav_key and url_path
are unchanged so routing is unaffected.
"""
from alembic import op


revision = "0110_transcripts_nav_rename"
down_revision = "0109_trial_intake_sessions"
branch_labels = None
depends_on = None

_ICON_SVG = (
    '<path stroke-linecap="round" stroke-linejoin="round" '
    'd="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 '
    '7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 '
    '2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 '
    '1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z"/>'
)


def upgrade():
    op.execute("""
        UPDATE ui_nav_items
        SET label = 'Transcripts',
            rail_label = 'Transcripts',
            icon = 'document-text',
            icon_emoji = '📄',
            icon_svg = :svg
        WHERE nav_key = 'depositions' AND tenant_id IS NULL
    """.replace(":svg", "'" + _ICON_SVG.replace("'", "''") + "'"))


def downgrade():
    op.execute("""
        UPDATE ui_nav_items
        SET label = 'Depositions',
            rail_label = 'Depos',
            icon = 'mic',
            icon_emoji = '🎤',
            icon_svg = NULL
        WHERE nav_key = 'depositions' AND tenant_id IS NULL
    """)
