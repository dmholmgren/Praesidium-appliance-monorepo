"""Index the self-referential FK document_sections.parent_section_id.

document_sections (~93M rows / 38GB) has a self-FK parent_section_id -> id with no
index on the REFERENCING column. Postgres enforces the FK with a per-deleted-row
RI trigger ("SELECT 1 ... WHERE parent_section_id = $1 FOR KEY SHARE"), which without
this index is a full sequential scan -- so any DELETE/supersede of a section row
(e.g. cr_parser's idempotent delete-before-write) stalls for minutes. Partial index
(parent_section_id IS NOT NULL) keeps it small: only sectioned docs with a parent.

Built CONCURRENTLY so it never blocks the live segmentation/extraction workload.
Idempotent.

Revision ID: 0104_ds_parent_idx
Revises: 0103_appellate_matter_tabs
"""
from __future__ import annotations

from alembic import op

revision = "0104_ds_parent_idx"
down_revision = "0103_appellate_matter_tabs"
branch_labels = None
depends_on = None

INDEX = "ix_document_sections_parent"


def upgrade() -> None:
    # CONCURRENTLY cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
            "ON document_sections (parent_section_id) "
            "WHERE parent_section_id IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
