"""onboarding clusters — C1 exclusion fixture for legacy bulk import

Cluster substrate: skip record for matter_sync, data source for the
exclusions UI, observation substrate for the AI-guided onboarding session.
Invariant: copied + clustered = enumerated, per matter.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0065_onboarding_clusters"
down_revision = "0064_stage_ledger_queue"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "onboarding_clusters",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", UUID(as_uuid=True), nullable=True),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("cluster_type", sa.String(32), nullable=False),
        sa.Column("detected_by", sa.String(32), nullable=True),
        sa.Column("evidence", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("collection_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ux_onb_clusters_tenant_root", "onboarding_clusters",
                    ["tenant_id", "root_path"], unique=True)
    op.create_index("ix_onb_clusters_matter", "onboarding_clusters",
                    ["tenant_id", "matter_id"])
    op.create_index("ix_onb_clusters_status", "onboarding_clusters", ["status"])


def downgrade():
    op.drop_table("onboarding_clusters")
