"""rendition durability — produce-able PDF renditions for emboss/export

The render and OCR lanes build PDF renditions of non-PDF / scanned natives.
Those are exactly the PDFs the production export engine will Bates-emboss,
so they move from working/ (scratch semantics) to renditions/{lane}/ (durable,
backed up with the collection) and the path is stamped on the document row.
"""
from alembic import op
import sqlalchemy as sa

revision = "0066_rendition_path"
down_revision = "0065_onboarding_clusters"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ediscovery_documents",
                  sa.Column("rendition_path", sa.String(2000), nullable=True))
    op.add_column("ediscovery_documents",
                  sa.Column("rendition_kind", sa.String(12), nullable=True))
    op.create_index("ix_edd_rendition_kind", "ediscovery_documents",
                    ["collection_id", "rendition_kind"])


def downgrade():
    op.drop_index("ix_edd_rendition_kind", table_name="ediscovery_documents")
    op.drop_column("ediscovery_documents", "rendition_kind")
    op.drop_column("ediscovery_documents", "rendition_path")
