"""0118: portable annotation objects + markup sets.

Make doc_annotations a POLYMORPHIC, reusable object store so highlights /
redactions / comments authored in any viewer (ediscovery, DMS / deal center,
appellate record + exhibit PDFs, page:line transcript) become first-class
objects keyed by (source_type, source_id) — independent of any one viewer — so
they can be filtered per user, embossed into a flattened PDF, and later pushed
to the trial-presentation viewer.

  source_type / source_id  — the polymorphic target:
      ediscovery -> ediscovery_documents.id
      dms        -> documents.id
      record     -> record_documents.id (appellate RR/CR/exhibit PDFs)
      transcript -> transcripts.id      (page:line text; page_number+text range)
  document_id / collection_id are relaxed to NULL (kept populated for the legacy
  ediscovery + dms routers; the generic API keys solely on source_type+source_id).

  markup_sets — a named, reusable grouping of annotations on one source
  (doc_annotations.markup_set_id -> markup_sets.id). kind:
      live     — the editable working layer (implicit; usually no set row)
      saved    — a named snapshot the author froze
      embossed — a flattened PDF was burned; embossed_document_id points at it
  This is the object the trial viewer will subscribe to.
"""
from alembic import op


revision = "0118_annotation_objects"
down_revision = "0117_routing_alerts"
branch_labels = None
depends_on = None


def upgrade():
    # ── doc_annotations -> polymorphic ──────────────────────────────────
    op.execute("ALTER TABLE doc_annotations ADD COLUMN IF NOT EXISTS source_type varchar(32)")
    op.execute("ALTER TABLE doc_annotations ADD COLUMN IF NOT EXISTS source_id   text")
    # relax legacy NOT NULLs so non-ediscovery sources can be stored
    op.execute("ALTER TABLE doc_annotations ALTER COLUMN collection_id DROP NOT NULL")
    op.execute("ALTER TABLE doc_annotations ALTER COLUMN document_id   DROP NOT NULL")
    # every existing row was an ediscovery document annotation
    op.execute("""
        UPDATE doc_annotations
           SET source_type = 'ediscovery',
               source_id   = document_id::text
         WHERE source_type IS NULL
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_doc_ann_source "
               "ON doc_annotations (tenant_id, source_type, source_id) "
               "WHERE deleted_at IS NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_doc_ann_markup_set "
               "ON doc_annotations (markup_set_id) WHERE deleted_at IS NULL")

    # ── markup_sets — reusable annotation objects + embossed pointer ─────
    op.execute("""
        CREATE TABLE IF NOT EXISTS markup_sets (
            id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id            char(36)    NOT NULL,
            source_type          varchar(32) NOT NULL,
            source_id            text        NOT NULL,
            name                 varchar(255) NOT NULL,
            kind                 varchar(16) NOT NULL DEFAULT 'saved',
                -- live | saved | embossed
            scope                varchar(16) NOT NULL DEFAULT 'shared',
                -- personal | shared
            embossed_document_id uuid,        -- DMS documents.id of burned PDF
            embossed_path        text,        -- on-disk path when not a DMS doc
            page_count           integer,
            owner_user_id        bigint,
            owner_name           varchar(200),
            meta                 jsonb       NOT NULL DEFAULT '{}'::jsonb,
            created_at           timestamptz NOT NULL DEFAULT now(),
            updated_at           timestamptz NOT NULL DEFAULT now(),
            deleted_at           timestamptz
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_markup_sets_source "
               "ON markup_sets (tenant_id, source_type, source_id) "
               "WHERE deleted_at IS NULL")


def downgrade():
    op.execute("DROP TABLE IF EXISTS markup_sets")
    op.execute("DROP INDEX IF EXISTS ix_doc_ann_markup_set")
    op.execute("DROP INDEX IF EXISTS ix_doc_ann_source")
    op.execute("ALTER TABLE doc_annotations DROP COLUMN IF EXISTS source_id")
    op.execute("ALTER TABLE doc_annotations DROP COLUMN IF EXISTS source_type")
