"""0074: deposition_exhibit_links -- page:line-anchored, no-copy exhibit refs.

Scope §3/§4: an exhibit used in a deposition is a METADATA reference into an
existing DMS / eDiscovery document, anchored at the transcript page:line where it
was marked or first discussed. We never copy the document (the "island model" the
competitors are stuck in -- §4); we point at it.

Why a new table rather than extending viaticum_exhibits: viaticum_exhibits is
session-grain live-presentation staging with a NOT NULL session_id (the S3-006
built module). This relation is a different grain -- transcript page:line-anchored
doc references that exist whether or not a live session has been created. We bridge
to the live-presentation row via viaticum_exhibit_id when an exhibit is also staged
for presentation, so the two stay linked without mutating the live table.

The exhibit_number / exhibit_label are captured (denormalized) at link time so the
cross-depo prior-exhibit library (§4) is corpus-agnostic -- it never has to join
into DMS vs eDiscovery to render. document_id is a loose ref (no FK) for the same
reason: it may point into either corpus.
"""
from alembic import op

revision = "0074_deposition_exhibits"
down_revision = "0073_transcript_qa_embeddings"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS deposition_exhibit_links (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           char(36) NOT NULL,
            matter_id           uuid,
            transcript_id       uuid
                REFERENCES deposition_transcripts(id) ON DELETE CASCADE,
            session_id          bigint,
            viaticum_exhibit_id bigint,
            document_id         uuid,
            document_source     varchar,
            exhibit_number      varchar,
            exhibit_label       varchar,
            anchor_page         integer,
            anchor_line         integer,
            qa_unit_id          uuid,
            marked_by           varchar,
            notes               text,
            created_by          bigint,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_depo_exh_transcript "
               "ON deposition_exhibit_links (tenant_id, transcript_id, anchor_page, anchor_line)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_depo_exh_matter "
               "ON deposition_exhibit_links (tenant_id, matter_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_depo_exh_document "
               "ON deposition_exhibit_links (tenant_id, document_id)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS deposition_exhibit_links")
