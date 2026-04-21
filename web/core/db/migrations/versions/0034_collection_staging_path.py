"""0034_collection_staging_path

Adds staging_path column to collection_documents.

staging_path stores the location of the uploaded file on the shared
/mnt/praesidium/staging mount, readable by both WEB-01 and PROC-01
RQ workers. Replaces the previous pattern of passing a local /tmp/
path as an RQ job argument (which failed cross-container).

Column is nullable — NULL after successful ingestion or discard.

Revision ID: 0034_collection_staging_path
Revises: 0033_search_term_proposals
"""
from alembic import op
import sqlalchemy as sa

revision = '0034_collection_staging_path'
down_revision = '0033_search_term_proposals'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Column may already exist if added via direct ALTER TABLE during hotfix
    op.execute("""
        ALTER TABLE collection_documents
        ADD COLUMN IF NOT EXISTS staging_path TEXT
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE collection_documents
        DROP COLUMN IF EXISTS staging_path
    """)
