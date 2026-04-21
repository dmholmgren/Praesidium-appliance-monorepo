"""ediscovery_documents text_path column

Revision ID: 0043_ediscovery_text_path
Revises: 0042_ediscovery_native_path
Create Date: 2026-04-18

Adds text_path column to ediscovery_documents to store the path to the
Relativity/Concordance TEXT companion file for a Bates record.

  text_path  -> TEXT/ path (extracted text companion from load file)

Column was added via DDL on 2026-04-18 during DAT-aware ingest pipeline
development. This migration makes it official and adds the index.
"""

from alembic import op
from sqlalchemy import text

revision = '0043_ediscovery_text_path'
down_revision = '0042_ediscovery_native_path'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(text("""
        ALTER TABLE ediscovery_documents
        ADD COLUMN IF NOT EXISTS text_path VARCHAR(2000);
    """))

    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_ediscovery_text_path
        ON ediscovery_documents (tenant_id, text_path)
        WHERE text_path IS NOT NULL;
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP INDEX IF EXISTS idx_ediscovery_text_path;"))
    conn.execute(text(
        "ALTER TABLE ediscovery_documents DROP COLUMN IF EXISTS text_path;"
    ))
