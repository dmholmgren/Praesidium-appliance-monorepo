"""record_excerpts + brief_doc_versions (Module B / Unit 11 brief workspace).

record_excerpts  -- passages a drafter collects from the appellate record viewer
                    (RR transcript text or a CR/exhibit locus). Source of the
                    left "Record Excerpts" list and the insert-at-cursor quotes.

brief_doc_versions -- snapshot ledger for the working brief DOCX. A row is written
                    on each deliberate Save / Save Version (and on reattach), so the
                    workspace can show a version timeline, diff, and restore between
                    edits/authors. The OnlyOffice changes zip is kept alongside for
                    per-author change inspection.

Revision ID: 0107_brief_workspace_tables
Revises: 0106_exhibit_objections
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0107_brief_workspace_tables"
down_revision = "0106_exhibit_objections"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "record_excerpts",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("appellate_case_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("record_document_id", postgresql.UUID(as_uuid=False)),
        sa.Column("record_kind", sa.String(length=20)),        # RR | CR | SUPP_RR | ...
        sa.Column("locus", sa.Text()),                         # cite, e.g. "RR 41:1-6" / "CR 50"
        sa.Column("page_first", sa.Integer()),
        sa.Column("page_last", sa.Integer()),
        sa.Column("start_line", sa.Integer()),
        sa.Column("end_line", sa.Integer()),
        sa.Column("text", sa.Text()),
        sa.Column("color", sa.String(length=20)),
        sa.Column("note", sa.Text()),
        sa.Column("created_by", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_record_excerpts_case", "record_excerpts", ["appellate_case_id"])

    op.create_table(
        "brief_doc_versions",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("appellate_case_id", postgresql.UUID(as_uuid=False)),
        sa.Column("document_id", postgresql.UUID(as_uuid=False)),
        sa.Column("storage_path", sa.Text()),                  # snapshot DOCX on disk
        sa.Column("changes_path", sa.Text()),                  # OnlyOffice changes zip (per-author)
        sa.Column("author_user_id", sa.Integer()),
        sa.Column("author_name", sa.Text()),
        sa.Column("label", sa.Text()),
        sa.Column("kind", sa.String(length=20), nullable=False,
                  server_default=sa.text("'save'")),           # autosave|save|version|reattach
        sa.Column("word_count", sa.Integer()),
        sa.Column("file_size", sa.Integer()),
        sa.Column("checksum", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_brief_versions_case", "brief_doc_versions",
                    ["appellate_case_id", "document_id"])


def downgrade():
    op.drop_index("ix_brief_versions_case", table_name="brief_doc_versions")
    op.drop_table("brief_doc_versions")
    op.drop_index("ix_record_excerpts_case", table_name="record_excerpts")
    op.drop_table("record_excerpts")
