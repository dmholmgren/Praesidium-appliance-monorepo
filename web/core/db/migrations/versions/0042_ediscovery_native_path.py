"""ediscovery_documents native_path column

Revision ID: 0042_ediscovery_native_path
Revises: 0041_dms_matter_rail_widgets
Create Date: 2026-04-18

Adds native_path column to ediscovery_documents to store the path to the
original native file (MP3, DOCX, MSG, etc.) from a Relativity production.

For opposing productions ingested with a DAT load file:
  file_path     -> IMAGES/ path (Relativity-produced PDF/TIFF)
  working_path  -> TEXT/ path (extracted text companion)
  native_path   -> NATIVES/ path (original file -- audio, Word, email, etc.)
"""

from alembic import op
from sqlalchemy import text

revision = '0042_ediscovery_native_path'
down_revision = '0041_dms_matter_rail_widgets'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("""
        ALTER TABLE ediscovery_documents
        ADD COLUMN IF NOT EXISTS native_path VARCHAR(2000);
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_ediscovery_native_path
        ON ediscovery_documents (tenant_id, native_path)
        WHERE native_path IS NOT NULL;
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP INDEX IF EXISTS idx_ediscovery_native_path;"))
    conn.execute(text(
        "ALTER TABLE ediscovery_documents DROP COLUMN IF EXISTS native_path;"
    ))
