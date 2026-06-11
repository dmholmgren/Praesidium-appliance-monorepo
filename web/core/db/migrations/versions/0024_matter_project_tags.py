"""matter project tags

Revision ID: 0024_matter_project_tags
Revises: 0023_exhibit_nesting
Create Date: 2026-05-16

Three-tier tag taxonomy:
  - Issue Tags (tag_set='issue'): substance - what the doc is about
  - Project Tags (tag_set='project'): intent - what to do with the doc
  - Task Tags (tag_set='task'): project-internal refinement

Schema changes:
  - tags.matter_id UUID NULL (FK matters)
  - tags.project_id UUID NULL (FK projects)
  - document_tags.matter_id UUID NULL (FK matters)
  - project_tag_scopes junction table
  - Backfill: existing tags -> tag_set='issue', matter_id from collection
  - Indexes for matter/project/routing lookups
"""

revision = '0024_matter_project_tags'
down_revision = '0023_exhibit_nesting'
branch_labels = None
depends_on = None


def upgrade():
    # DDL executed directly via psql on host
    pass


def downgrade():
    # Reverse: drop new objects
    from alembic import op
    op.drop_table('project_tag_scopes')
    op.drop_column('document_tags', 'matter_id')
    op.drop_column('tags', 'project_id')
    op.drop_column('tags', 'matter_id')
    op.execute("UPDATE tags SET tag_set = NULL WHERE tag_set = 'issue'")
