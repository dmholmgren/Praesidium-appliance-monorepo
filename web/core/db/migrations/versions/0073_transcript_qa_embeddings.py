"""0073: transcript_qa_embeddings -- Layer-3 derivative over the Q&A primitive.

The embed stage of the depo DAG (qa_unit grain, narrow batched GPU lane) writes
one row per embedded chunk of a transcript_qa_unit, into the SAME 768-dim
ModernBERT space as the rest of the litigation corpus (caselaw opinions,
allegations, cause-of-action elements) -- so testimony kNNs directly against
pleaded-claim element exemplars (U10) and against other testimony (variance).

Row-per-chunk, mirroring reference_opinion_embeddings: a long answer is split
into multiple chunks (chunk_number 0..n); short Q&A exchanges are a single
chunk_0. Idempotent via (qa_unit_id, embedding_model, chunk_number).

Layer-3 re-embed advantage: a model swap = truncate this table + reset the embed
ledger rows + re-drain the GPU lane. Never re-ingest or re-segment.

HNSW is deferred per 0041/0043/0044 precedent (no ANN index over an empty
column): the embed pass builds it after bulk-loading vectors. The exact statement
is recorded in DEFERRED_HNSW and run by embed_qa.build_index().
"""
from alembic import op

revision = "0073_transcript_qa_embeddings"
down_revision = "0072_deposition_pipeline"
branch_labels = None
depends_on = None

# Built after the column is populated (embed_qa.build_index()). vector_cosine_ops
# because praesidium-embed emits normalized 768-d vectors.
DEFERRED_HNSW = [
    "CREATE INDEX IF NOT EXISTS ix_transcript_qa_embedding "
    "ON transcript_qa_embeddings USING hnsw (embedding vector_cosine_ops)",
]


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS transcript_qa_embeddings (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            qa_unit_id      uuid NOT NULL
                REFERENCES transcript_qa_units(id) ON DELETE CASCADE,
            transcript_id   uuid NOT NULL,
            tenant_id       char(36) NOT NULL,
            embedding_model varchar(100) NOT NULL,
            chunk_number    integer NOT NULL DEFAULT 0,
            chunk_text      text,
            embedding       vector(768),
            embedded_at     timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_transcript_qa_emb "
               "ON transcript_qa_embeddings (qa_unit_id, embedding_model, chunk_number)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_transcript_qa_emb_transcript "
               "ON transcript_qa_embeddings (transcript_id)")
    # NOTE: HNSW (DEFERRED_HNSW) intentionally NOT built here -- empty column.


def downgrade():
    op.execute("DROP TABLE IF EXISTS transcript_qa_embeddings")
