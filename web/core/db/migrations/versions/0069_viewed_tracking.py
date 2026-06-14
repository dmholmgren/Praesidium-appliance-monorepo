"""0069: viewed tracking - decouple 'looked at' from responsiveness coding.

- ediscovery_documents.viewed_at / viewed_by: set automatically when a doc
  is opened in the review viewer. 'Reviewed' in the viewer now derives as:
  in a review batch -> batch workflow status; otherwise -> viewed_at set.
- Trigger: assigning a doc to a review batch clears viewed_at/viewed_by so
  the formal workflow owns review state from that point.
- Backfill: docs already coded (reviewed_at set) are marked viewed.
"""
from alembic import op

revision = "0069_viewed_tracking"
down_revision = "0068_doc_integrity_flags"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE ediscovery_documents
            ADD COLUMN IF NOT EXISTS viewed_at timestamptz,
            ADD COLUMN IF NOT EXISTS viewed_by bigint
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_rbd_document_id
            ON review_batch_documents (document_id)
    """)
    op.execute("""
        UPDATE ediscovery_documents
           SET viewed_at = reviewed_at
         WHERE reviewed_at IS NOT NULL AND viewed_at IS NULL
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION fn_rbd_unset_viewed() RETURNS trigger AS $$
        BEGIN
            IF NEW.document_source IS DISTINCT FROM 'dms' THEN
                UPDATE ediscovery_documents
                   SET viewed_at = NULL, viewed_by = NULL
                 WHERE id = NEW.document_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_rbd_unset_viewed ON review_batch_documents")
    op.execute("""
        CREATE TRIGGER trg_rbd_unset_viewed
            AFTER INSERT ON review_batch_documents
            FOR EACH ROW EXECUTE FUNCTION fn_rbd_unset_viewed()
    """)


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS trg_rbd_unset_viewed ON review_batch_documents")
    op.execute("DROP FUNCTION IF EXISTS fn_rbd_unset_viewed()")
    op.execute("DROP INDEX IF EXISTS ix_rbd_document_id")
    op.execute("ALTER TABLE ediscovery_documents DROP COLUMN IF EXISTS viewed_at")
    op.execute("ALTER TABLE ediscovery_documents DROP COLUMN IF EXISTS viewed_by")
