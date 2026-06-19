"""documents.legal_category + legal_meta — legal document classification on the
curated registry the user-facing surfaces actually read.

The persisted classifier (classification_results) is keyed on dms_documents, a
registry DISJOINT from the curated `documents` the DMS/Trial Center read — so its
classifications never reach those surfaces. This adds the legal type directly to
`documents` (the unified-ingest legal_category design) so Trial Center
Pleadings/Motions and other surfaces can project it. Nullable ADD COLUMN =
instant metadata-only on the 245k-row table.

Revision ID: 0126_documents_legal_category
Revises: 0125_document_access_log
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0126_documents_legal_category"
down_revision = "0125_document_access_log"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("legal_category", sa.String(length=50)))
    op.add_column("documents", sa.Column("legal_meta", postgresql.JSONB()))
    op.create_index("ix_documents_matter_legalcat", "documents",
                    ["matter_id", "legal_category"])


def downgrade():
    op.drop_index("ix_documents_matter_legalcat", table_name="documents")
    op.drop_column("documents", "legal_meta")
    op.drop_column("documents", "legal_category")
