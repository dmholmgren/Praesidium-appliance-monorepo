"""0093: promote Depositions to a top-level module in the global nav rail.

Inserts a platform-default ui_nav_items row (tenant_id NULL) between eDiscovery
(display_order 700) and Billing (800) at 750. Lands on the global /depositions
home (lists matters with deposition activity), which drills into the matter-scoped
2x3 homepage at /depositions/home/{matter_id}. Mirrors the eDiscovery global->matter
pattern. Rail renders icon_emoji first, so 🎤 is enough.
"""
from alembic import op


revision = "0093_depositions_nav_item"
down_revision = "0092_backfill_deadlines_to_tasks"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        INSERT INTO ui_nav_items
            (nav_key, label, rail_label, icon, icon_emoji, url_path,
             display_order, section, page_key, is_active, tenant_id)
        SELECT 'depositions', 'Depositions', 'Depos', 'mic', '🎤', '/depositions',
               750, 'main', 'depositions', true, NULL
        WHERE NOT EXISTS (
            SELECT 1 FROM ui_nav_items WHERE nav_key = 'depositions' AND tenant_id IS NULL
        )
    """)


def downgrade():
    op.execute("DELETE FROM ui_nav_items WHERE nav_key = 'depositions' AND tenant_id IS NULL")
