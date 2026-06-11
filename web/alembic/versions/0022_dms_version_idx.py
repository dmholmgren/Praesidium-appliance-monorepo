"""DMS version detection indexes

Revision ID: 0022_dms_version_idx
Revises: 0021_billing_settings
Create Date: 2026-05-14
"""
from alembic import op

revision = '0022_dms_version_idx'
down_revision = '0021_billing_settings'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_documents_matter_checksum
            ON documents (matter_id, checksum)
            WHERE checksum IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_documents_tenant_matter_path
            ON documents (matter_id, storage_path)
            WHERE storage_path IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_documents_matter_has_text
            ON documents (matter_id, created_at DESC)
            WHERE extracted_text IS NOT NULL
              AND LENGTH(extracted_text) >= 50
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_documents_parent_doc
            ON documents (parent_doc_id)
            WHERE parent_doc_id IS NOT NULL
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_documents_matter_checksum")
    op.execute("DROP INDEX IF EXISTS idx_documents_tenant_matter_path")
    op.execute("DROP INDEX IF EXISTS idx_documents_matter_has_text")
    op.execute("DROP INDEX IF EXISTS idx_documents_parent_doc")
