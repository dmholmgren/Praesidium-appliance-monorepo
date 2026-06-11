"""
0023_exhibit_nesting.py — Add parent_id for nested exhibits

Enables hierarchical exhibit structure:
  Motion (main_doc)
    Exhibit A — Declaration (Word, role=exhibit)
      Exhibit A-1 — Email chain (PDF, role=exhibit, parent_id -> A)
      Exhibit A-2 — Contract (PDF, role=exhibit, parent_id -> A)
    Exhibit B — Declaration (Word, role=exhibit)
      Exhibit B-1 — Invoice (PDF, role=exhibit, parent_id -> B)

Also adds position options: top-center, bottom-center for sticker/bates.
"""

revision = "0023_exhibit_nesting"
down_revision = "0022_matter_contacts_people"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade():
    # parent_id for nesting exhibits under declarations
    op.add_column("project_documents",
        sa.Column("parent_id", sa.dialects.postgresql.UUID(), nullable=True))

    op.create_foreign_key(
        "fk_project_documents_parent",
        "project_documents", "project_documents",
        ["parent_id"], ["id"],
        ondelete="SET NULL"
    )

    op.create_index(
        "ix_project_documents_parent_id",
        "project_documents", ["parent_id"],
        postgresql_where=sa.text("parent_id IS NOT NULL")
    )


def downgrade():
    op.drop_index("ix_project_documents_parent_id", "project_documents")
    op.drop_constraint("fk_project_documents_parent", "project_documents")
    op.drop_column("project_documents", "parent_id")
