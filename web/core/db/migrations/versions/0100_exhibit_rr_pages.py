"""exhibit rr page ranges -- link trial_exhibits to their pages in the RR exhibit volume

(revision id kept <=32 chars for alembic_version.version_num.)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0100_exhibit_rr_pages"
down_revision = "0099_appellate_pa_siblings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("trial_exhibits", sa.Column("rr_record_id", postgresql.UUID(as_uuid=False), nullable=True))
    op.add_column("trial_exhibits", sa.Column("rr_page_first", sa.Integer(), nullable=True))
    op.add_column("trial_exhibits", sa.Column("rr_page_last", sa.Integer(), nullable=True))
    op.add_column("trial_exhibits", sa.Column("rr_page_label", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("trial_exhibits", "rr_page_label")
    op.drop_column("trial_exhibits", "rr_page_last")
    op.drop_column("trial_exhibits", "rr_page_first")
    op.drop_column("trial_exhibits", "rr_record_id")
