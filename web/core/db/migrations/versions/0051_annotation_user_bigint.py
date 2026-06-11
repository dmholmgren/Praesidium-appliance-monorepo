"""0051 annotation user columns -> bigint.

doc_annotations.created_by / updated_by were defined as uuid, but users.id is
bigint (the house convention -- cf. model_provider_groups.created_by in 0049).
Every annotation insert failed casting the integer user id to uuid. Retype both
columns to bigint. The table is empty at migration time, so USING NULL is
lossless.
"""
from alembic import op

revision = "0051_annotation_user_bigint"
down_revision = "0050_dfm_folderkey"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE doc_annotations ALTER COLUMN created_by TYPE bigint USING NULL::bigint")
    op.execute("ALTER TABLE doc_annotations ALTER COLUMN updated_by TYPE bigint USING NULL::bigint")


def downgrade():
    op.execute("ALTER TABLE doc_annotations ALTER COLUMN created_by TYPE uuid USING NULL::uuid")
    op.execute("ALTER TABLE doc_annotations ALTER COLUMN updated_by TYPE uuid USING NULL::uuid")
