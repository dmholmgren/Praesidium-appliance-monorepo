"""dms_folder_matches: folder-level unique key

Was UNIQUE(tenant_id, matter_id) -- enforced one folder per matter, which blocks
multi-folder-per-matter and the Legacy-General catch-all (many folders -> one
matter). Replace with UNIQUE(tenant_id, folder_path, matter_id): a folder->matter
pair is unique, a matter may own many folders, and a folder may carry history
(rejected from A, later proposed to Legacy-General).
"""
from alembic import op

revision = "0050_dfm_folderkey"
down_revision = "0049_model_override_groups"
branch_labels = None
depends_on = None

OLD = "dms_folder_matches_tenant_id_matter_id_key"
NEW = "dms_folder_matches_folder_matter_key"


def upgrade():
    op.drop_constraint(OLD, "dms_folder_matches", type_="unique")
    op.create_unique_constraint(
        NEW, "dms_folder_matches", ["tenant_id", "folder_path", "matter_id"]
    )


def downgrade():
    op.drop_constraint(NEW, "dms_folder_matches", type_="unique")
    op.create_unique_constraint(
        OLD, "dms_folder_matches", ["tenant_id", "matter_id"]
    )
