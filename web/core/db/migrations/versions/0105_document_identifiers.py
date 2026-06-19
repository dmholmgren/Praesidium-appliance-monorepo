"""document_identifiers -- one document, many context IDs.

A single document carries different identities depending on where it surfaces:
  Bates WEIR000123  ->  depo Exhibit 14  ->  MSJ Exhibit B  ->  trial PX-23

This is the resolution target the §6 exhibit-reference extractor resolves *to*:
a hyperlink in a pleading/motion body ("see Ex. B") looks up the id_value here
and lands on the underlying document_id (reference, no copy -- S3-005).

Net-new per Scope v2 §2.2 / build-sequence step 1. Backfilled idempotently from
the already-built exhibit registers (trial_exhibits, deposition_exhibit_links).

(revision id kept <=32 chars for alembic_version.version_num.)

Revision ID: 0105_document_identifiers
Revises: 0104_ds_parent_idx
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0105_document_identifiers"
down_revision = "0104_ds_parent_idx"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "document_identifiers",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("matter_id", postgresql.UUID(as_uuid=False)),
        # the canonical document this identity points at (no-copy reference).
        sa.Column("document_id", postgresql.UUID(as_uuid=False)),
        sa.Column("document_source", sa.String(length=20)),   # dms | ediscovery | external
        # what flavour of identity this is, and its literal token.
        sa.Column("id_kind", sa.String(length=30), nullable=False),  # bates|depo_exhibit|motion_exhibit|trial_exhibit|other
        sa.Column("id_value", sa.Text(), nullable=False),           # 'PX-23', 'Exhibit 14', 'Ex. B', 'WEIR000123'
        sa.Column("context_ref", sa.Text()),                        # transcript_id / trial_id / motion doc id / free label
        # provenance so a backfill can supersede-not-duplicate.
        sa.Column("source_table", sa.String(length=40)),
        sa.Column("source_id", postgresql.UUID(as_uuid=False)),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_docident_document", "document_identifiers", ["document_id"])
    op.create_index("ix_docident_matter_kind", "document_identifiers", ["matter_id", "id_kind"])
    op.create_index("ix_docident_value", "document_identifiers", ["id_value"])
    # idempotent backfill key: one identity per (source row, kind).
    op.create_index("ux_docident_source", "document_identifiers",
                    ["source_table", "source_id", "id_kind"], unique=True)

    conn = op.get_bind()

    # Backfill 1 -- trial exhibit register (PX-/DX- style trial identities).
    conn.execute(sa.text("""
        INSERT INTO document_identifiers
            (tenant_id, matter_id, document_id, document_source,
             id_kind, id_value, context_ref, source_table, source_id)
        SELECT te.tenant_id, te.matter_id, te.document_id, te.document_source,
               'trial_exhibit', te.exhibit_number, te.trial_id::text,
               'trial_exhibits', te.id
        FROM trial_exhibits te
        WHERE te.exhibit_number IS NOT NULL AND btrim(te.exhibit_number) <> ''
          AND NOT EXISTS (
              SELECT 1 FROM document_identifiers di
              WHERE di.source_table = 'trial_exhibits'
                AND di.source_id = te.id AND di.id_kind = 'trial_exhibit')
    """))

    # Backfill 2 -- deposition exhibit links (depo Exhibit N identities).
    conn.execute(sa.text("""
        INSERT INTO document_identifiers
            (tenant_id, matter_id, document_id, document_source,
             id_kind, id_value, context_ref, source_table, source_id)
        SELECT el.tenant_id, el.matter_id, el.document_id, el.document_source,
               'depo_exhibit', el.exhibit_number, el.transcript_id::text,
               'deposition_exhibit_links', el.id
        FROM deposition_exhibit_links el
        WHERE el.exhibit_number IS NOT NULL AND btrim(el.exhibit_number) <> ''
          AND NOT EXISTS (
              SELECT 1 FROM document_identifiers di
              WHERE di.source_table = 'deposition_exhibit_links'
                AND di.source_id = el.id AND di.id_kind = 'depo_exhibit')
    """))


def downgrade():
    op.drop_index("ux_docident_source", table_name="document_identifiers")
    op.drop_index("ix_docident_value", table_name="document_identifiers")
    op.drop_index("ix_docident_matter_kind", table_name="document_identifiers")
    op.drop_index("ix_docident_document", table_name="document_identifiers")
    op.drop_table("document_identifiers")
