"""0098: appellate practice area (Scope v2 / BuildOrder C-1).

Relaxes practice_areas.default_matter_type CHECK to admit 'appellate' (the new
behavioral matter-type branch) and upserts the dedicated 'appellate_civil' code so
an appellate matter is selectable in the edit-matter modal and routes the matter
to the appellate surface (default_matter_type='appellate', intelligence_template=
'appellate').

The pre-existing 'appellate' row (default_matter_type='litigation') is the LEGACY
"appeals as litigation" slug and is intentionally left untouched -- changing it
could re-route existing litigation matters. New appellate matters use 'appellate_civil'.

No on-box practice_areas seed file exists (the v18.0 full-upsert seed is off-box), so
this migration is the on-box source of truth. The upsert is idempotent: if a future
seed also defines the row, ON CONFLICT keeps them reconciled.
"""
from alembic import op


revision = "0098_appellate_practice_area"
down_revision = "0097_record_gaps"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE practice_areas DROP CONSTRAINT IF EXISTS practice_areas_default_matter_type_check")
    op.execute(
        "ALTER TABLE practice_areas ADD CONSTRAINT practice_areas_default_matter_type_check "
        "CHECK (default_matter_type IN ('litigation','transactional','appellate'))"
    )
    op.execute(
        """
        INSERT INTO practice_areas
            (code, display_name, category, default_matter_type, intelligence_template, sort_order, is_active)
        VALUES
            ('appellate_civil', 'Appellate — Civil', 'Civil Litigation', 'appellate', 'appellate', 245, true)
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
    op.execute("DELETE FROM practice_areas WHERE code='appellate_civil'")
    op.execute("ALTER TABLE practice_areas DROP CONSTRAINT IF EXISTS practice_areas_default_matter_type_check")
    op.execute(
        "ALTER TABLE practice_areas ADD CONSTRAINT practice_areas_default_matter_type_check "
        "CHECK (default_matter_type IN ('litigation','transactional'))"
    )
