"""0052 geometry substrate — Document Geometry Plumbing Contract v1.1, Slice G1.

Coordinate substrate so every viewer overlay (redaction, spine-highlight, search,
translation) resolves through one query:
  doc_layout_tokens  — page_bbox <-> char-span bridge (the keystone, §2)
  doc_layout_cells   — the 'cell' kind for xlsx/csv (§2)
  text_alignments    — §7 span-map primitive (translation + ocr_reconcile)
  geometry_kind      — per-document renderer/source discriminator

Keyed by (corpus, doc_id): ediscovery_documents.document_id / dms_document_id are
100% NULL in prod (1.5M rows), so unification on documents.id is unavailable now.
doc_id is uuid across ediscovery_documents / documents / dms_documents.

This migration backfills the alembic chain for DDL that was hand-applied at deploy.
Idempotent (IF NOT EXISTS), matching the 0036 precedent. Statements are one per
op.execute() — the alembic env runs on asyncpg, which rejects multi-statement execute().
"""
from alembic import op

revision = "0052_geometry_substrate"
down_revision = "0051_annotation_user_bigint"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS doc_layout_tokens (
          id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id   char(36) NOT NULL,
          corpus      text NOT NULL,
          doc_id      uuid NOT NULL,
          rendition   text NOT NULL,
          page_number int NOT NULL,
          page_width  numeric NOT NULL,
          page_height numeric NOT NULL,
          x numeric NOT NULL, y numeric NOT NULL, w numeric NOT NULL, h numeric NOT NULL,
          char_start  int NOT NULL,
          char_end    int NOT NULL,
          unit        text NOT NULL DEFAULT 'word',
          text        text,
          source      text NOT NULL,
          confidence  numeric,
          created_at  timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_layout_doc_page ON doc_layout_tokens (corpus, doc_id, rendition, page_number)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_layout_doc_span ON doc_layout_tokens (corpus, doc_id, rendition, char_start, char_end)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS doc_layout_cells (
          id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id  char(36) NOT NULL,
          corpus     text NOT NULL,
          doc_id     uuid NOT NULL,
          sheet      text NOT NULL,
          row_idx    int NOT NULL,
          col_idx    int NOT NULL,
          a1_range   text,
          char_start int NOT NULL,
          char_end   int NOT NULL,
          text       text,
          created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_layout_cells_doc ON doc_layout_cells (corpus, doc_id, sheet)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_layout_cells_span ON doc_layout_cells (corpus, doc_id, char_start, char_end)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS text_alignments (
          id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id   char(36) NOT NULL,
          corpus      text NOT NULL,
          doc_id      uuid NOT NULL,
          rel         text NOT NULL,
          canon_start int NOT NULL,
          canon_end   int NOT NULL,
          alt_start   int NOT NULL,
          alt_end     int NOT NULL,
          created_at  timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_text_align_doc_rel ON text_alignments (corpus, doc_id, rel)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_text_align_canon ON text_alignments (corpus, doc_id, rel, canon_start, canon_end)")

    op.execute("ALTER TABLE ediscovery_documents ADD COLUMN IF NOT EXISTS geometry_kind varchar(12)")
    op.execute("ALTER TABLE documents            ADD COLUMN IF NOT EXISTS geometry_kind varchar(12)")
    op.execute("ALTER TABLE dms_documents        ADD COLUMN IF NOT EXISTS geometry_kind varchar(12)")


def downgrade():
    op.execute("ALTER TABLE dms_documents        DROP COLUMN IF EXISTS geometry_kind")
    op.execute("ALTER TABLE documents            DROP COLUMN IF EXISTS geometry_kind")
    op.execute("ALTER TABLE ediscovery_documents DROP COLUMN IF EXISTS geometry_kind")
    op.execute("DROP TABLE IF EXISTS text_alignments")
    op.execute("DROP TABLE IF EXISTS doc_layout_cells")
    op.execute("DROP TABLE IF EXISTS doc_layout_tokens")
