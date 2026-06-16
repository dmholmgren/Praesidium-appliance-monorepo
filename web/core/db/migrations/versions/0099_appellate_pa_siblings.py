"""0099: appellate practice areas -- criminal + original proceeding (Scope v2 C-1).

Rounds out the appellate branch begun in 0098 (appellate_civil) with the two
sibling codes named in the scope, each default_matter_type='appellate',
intelligence_template='appellate':

  appellate_criminal            -> "Appellate — Criminal"            (category Criminal)
  original_proceeding_mandamus  -> "Original Proceeding (Mandamus)"  (category Civil Litigation)

The default_matter_type CHECK already admits 'appellate' (0098). Idempotent upsert.
(revision id kept <=32 chars for alembic_version.version_num.)
"""
from alembic import op


revision = "0099_appellate_pa_siblings"
down_revision = "0098_appellate_practice_area"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        INSERT INTO practice_areas
            (code, display_name, category, default_matter_type, intelligence_template, sort_order, is_active)
        VALUES
            ('appellate_criminal', 'Appellate — Criminal', 'Criminal', 'appellate', 'appellate', 575, true),
            ('original_proceeding_mandamus', 'Original Proceeding (Mandamus)', 'Civil Litigation', 'appellate', 'appellate', 246, true)
        ON CONFLICT (code) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            category = EXCLUDED.category,
            default_matter_type = EXCLUDED.default_matter_type,
            intelligence_template = EXCLUDED.intelligence_template,
            sort_order = EXCLUDED.sort_order,
            is_active = true
        """
    )


def downgrade():
    op.execute("DELETE FROM practice_areas WHERE code IN ('appellate_criminal','original_proceeding_mandamus')")
