"""exhibit usages -- per-reference loci of each exhibit within the testimony (for the viewer)

(revision id kept <=32 chars for alembic_version.version_num.)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0101_exhibit_usages"
down_revision = "0100_exhibit_rr_pages"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "trial_exhibit_usages",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("exhibit_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("transcript_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("page", sa.Integer()),
        sa.Column("line", sa.Integer()),
        sa.Column("char_start", sa.Integer()),
        sa.Column("char_end", sa.Integer()),
        sa.Column("locus", sa.Text()),
        sa.Column("usage_kind", sa.Text()),
        sa.Column("snippet", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_teu_exhibit", "trial_exhibit_usages", ["exhibit_id"])
    op.create_index("ix_teu_transcript", "trial_exhibit_usages", ["transcript_id"])


def downgrade():
    op.drop_index("ix_teu_transcript", table_name="trial_exhibit_usages")
    op.drop_index("ix_teu_exhibit", table_name="trial_exhibit_usages")
    op.drop_table("trial_exhibit_usages")
