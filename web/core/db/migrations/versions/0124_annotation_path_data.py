"""0124: freehand path storage + 'drawing' annotation type.

Adds doc_annotations.path_data (jsonb) — an array of [x,y] points (percent of
page, 0-100) for freehand 'drawing' annotations, and widens the annotation_type
CHECK to allow 'drawing'. Text-box annotations reuse 'comment' with page_number
+ x/y/width/height + comment_text.
"""
from alembic import op


revision = "0124_annotation_path_data"
down_revision = "0123_exhibit_list_taxonomy"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE doc_annotations ADD COLUMN IF NOT EXISTS path_data jsonb")
    op.execute("ALTER TABLE doc_annotations DROP CONSTRAINT IF EXISTS chk_annotation_type")
    op.execute(
        "ALTER TABLE doc_annotations ADD CONSTRAINT chk_annotation_type "
        "CHECK (annotation_type IN ('redaction','highlight','comment','drawing'))"
    )


def downgrade():
    op.execute("ALTER TABLE doc_annotations DROP CONSTRAINT IF EXISTS chk_annotation_type")
    op.execute(
        "ALTER TABLE doc_annotations ADD CONSTRAINT chk_annotation_type "
        "CHECK (annotation_type IN ('redaction','highlight','comment'))"
    )
    op.execute("ALTER TABLE doc_annotations DROP COLUMN IF EXISTS path_data")
