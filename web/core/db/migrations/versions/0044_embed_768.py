"""0044 embed_768: unify the firm/litigation embedding space on 768 (local ModernBERT).

Context: the canonical embedder is now Free-Law-Project/modernbert-embed-base_finetune_512
(768-dim), running locally on the V100 and verified cosine 0.999998 against the
CourtListener corpus (reference_opinion_embeddings, vector(768)). The whole point is
that firm/litigation text lands in the SAME space as the 100M-opinion caselaw corpus,
so discovery sections, allegations, and cause-of-action elements can kNN directly
against opinions and against each other.

The prior 1024 choice (0043 et al.) existed only because voyage-law-2 was the embedder.
That premise is retired. All open workstreams agree: collapse to 768. The ingestion
pipeline is being rebuilt, so the data primitives (and their embeddings) are re-derived
downstream -- existing vectors are discarded here with USING NULL, intentionally.

Scope:
  * Single-slot tables re-typed 1024/1536 -> 768 (vectors discarded; re-embedded later):
      allegations, authority_embeddings, canonical_entities, case_topics,
      causes_of_action, coa_elements, document_chunk_embeddings, document_sections,
      documents.embedding_vec, matter_intelligence_index, sali_concepts
  * Multi-slot chunk tables get a new embedding_768 slot (new ingestion writes it;
      legacy embedding_1024/_1536/_3072 stay for now, dropped in a later cleanup once
      the rebuilt pipeline is confirmed targeting _768):
      dms_chunk_embeddings, billing_chunk_embeddings,
      drafting_chunk_embeddings, email_chunk_embeddings

NOT touched (deliberate):
  * contract_corpus / contract_clause_primitives (1024, ~400K rows) -- ModernBERT is
      caselaw-tuned and weaker on contracts; the existing voyage vectors are good enough
      seed and contract retrieval is not the priority. Left in their 1024 space.

HNSW: an ALTER TYPE is blocked by an ANN index on the column, so the three existing
vector indexes on re-typed columns are dropped here. Per 0041/0043 precedent we do NOT
build ANN indexes over empty/NULL columns -- the embedding passes rebuild them after
populating vectors. Exact statements recorded in DEFERRED_HNSW below.

asyncpg: one statement per op.execute(). DROP/ADD are guarded; ALTER TYPE ... USING NULL
is safe to re-run (it just re-nulls a pre-population column).
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0044_embed_768"
down_revision = "0043_sali_concepts"
branch_labels = None
depends_on = None


# Recorded for the embedding passes to run once each column is populated. NOT run here
# (deferring ANN indexes over empty columns, per 0041/0043). vector_cosine_ops because
# the service emits normalized 768-d vectors.
DEFERRED_HNSW = [
    "CREATE INDEX IF NOT EXISTS ix_sali_concepts_embedding        ON sali_concepts            USING hnsw (embedding     vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS idx_ae_embedding_hnsw             ON authority_embeddings     USING hnsw (embedding     vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS idx_dce_embedding_hnsw            ON document_chunk_embeddings USING hnsw (embedding    vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS ix_allegations_embedding          ON allegations              USING hnsw (embedding     vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS ix_case_topics_embedding          ON case_topics              USING hnsw (embedding     vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS ix_causes_of_action_embedding     ON causes_of_action         USING hnsw (embedding     vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS ix_coa_elements_embedding         ON coa_elements             USING hnsw (embedding     vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS ix_canonical_entities_embedding   ON canonical_entities       USING hnsw (embedding     vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS ix_document_sections_embedding    ON document_sections        USING hnsw (embedding     vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS ix_documents_embedding_vec        ON documents                USING hnsw (embedding_vec vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS ix_mii_embedding                  ON matter_intelligence_index USING hnsw (embedding    vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS idx_dms_emb_768_hnsw              ON dms_chunk_embeddings      USING hnsw (embedding_768 vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS idx_billing_emb_768_hnsw          ON billing_chunk_embeddings  USING hnsw (embedding_768 vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS idx_drafting_emb_768_hnsw         ON drafting_chunk_embeddings USING hnsw (embedding_768 vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS idx_email_emb_768_hnsw            ON email_chunk_embeddings    USING hnsw (embedding_768 vector_cosine_ops)",
]


UPGRADE_STATEMENTS = [
    # --- 1. drop ANN indexes on columns whose dimension changes (else ALTER TYPE errors)
    "DROP INDEX IF EXISTS ix_sali_concepts_embedding",
    "DROP INDEX IF EXISTS idx_ae_embedding_hnsw",
    "DROP INDEX IF EXISTS idx_dce_embedding_hnsw",

    # --- 2. re-type single-slot columns to 768 (discard prior vectors; re-derived later)
    "ALTER TABLE allegations             ALTER COLUMN embedding     TYPE vector(768) USING NULL::vector(768)",
    "ALTER TABLE authority_embeddings    ALTER COLUMN embedding     TYPE vector(768) USING NULL::vector(768)",
    "ALTER TABLE canonical_entities      ALTER COLUMN embedding     TYPE vector(768) USING NULL::vector(768)",
    "ALTER TABLE case_topics             ALTER COLUMN embedding     TYPE vector(768) USING NULL::vector(768)",
    "ALTER TABLE causes_of_action        ALTER COLUMN embedding     TYPE vector(768) USING NULL::vector(768)",
    "ALTER TABLE coa_elements            ALTER COLUMN embedding     TYPE vector(768) USING NULL::vector(768)",
    "ALTER TABLE document_chunk_embeddings ALTER COLUMN embedding   TYPE vector(768) USING NULL::vector(768)",
    "ALTER TABLE document_sections       ALTER COLUMN embedding     TYPE vector(768) USING NULL::vector(768)",
    "ALTER TABLE documents               ALTER COLUMN embedding_vec TYPE vector(768) USING NULL::vector(768)",
    "ALTER TABLE matter_intelligence_index ALTER COLUMN embedding   TYPE vector(768) USING NULL::vector(768)",
    "ALTER TABLE sali_concepts           ALTER COLUMN embedding     TYPE vector(768) USING NULL::vector(768)",

    # --- 3. add 768 slot to multi-slot chunk tables (new ingestion writes here)
    "ALTER TABLE dms_chunk_embeddings      ADD COLUMN IF NOT EXISTS embedding_768 vector(768)",
    "ALTER TABLE billing_chunk_embeddings  ADD COLUMN IF NOT EXISTS embedding_768 vector(768)",
    "ALTER TABLE drafting_chunk_embeddings ADD COLUMN IF NOT EXISTS embedding_768 vector(768)",
    "ALTER TABLE email_chunk_embeddings    ADD COLUMN IF NOT EXISTS embedding_768 vector(768)",
]


DOWNGRADE_STATEMENTS = [
    "ALTER TABLE dms_chunk_embeddings      DROP COLUMN IF EXISTS embedding_768",
    "ALTER TABLE billing_chunk_embeddings  DROP COLUMN IF EXISTS embedding_768",
    "ALTER TABLE drafting_chunk_embeddings DROP COLUMN IF EXISTS embedding_768",
    "ALTER TABLE email_chunk_embeddings    DROP COLUMN IF EXISTS embedding_768",
    # restore original dimensions (1024 unless noted 1536)
    "ALTER TABLE allegations             ALTER COLUMN embedding     TYPE vector(1024) USING NULL::vector(1024)",
    "ALTER TABLE canonical_entities      ALTER COLUMN embedding     TYPE vector(1024) USING NULL::vector(1024)",
    "ALTER TABLE case_topics             ALTER COLUMN embedding     TYPE vector(1024) USING NULL::vector(1024)",
    "ALTER TABLE causes_of_action        ALTER COLUMN embedding     TYPE vector(1024) USING NULL::vector(1024)",
    "ALTER TABLE coa_elements            ALTER COLUMN embedding     TYPE vector(1024) USING NULL::vector(1024)",
    "ALTER TABLE document_sections       ALTER COLUMN embedding     TYPE vector(1024) USING NULL::vector(1024)",
    "ALTER TABLE sali_concepts           ALTER COLUMN embedding     TYPE vector(1024) USING NULL::vector(1024)",
    "ALTER TABLE authority_embeddings    ALTER COLUMN embedding     TYPE vector(1536) USING NULL::vector(1536)",
    "ALTER TABLE document_chunk_embeddings ALTER COLUMN embedding   TYPE vector(1536) USING NULL::vector(1536)",
    "ALTER TABLE documents               ALTER COLUMN embedding_vec TYPE vector(1536) USING NULL::vector(1536)",
    "ALTER TABLE matter_intelligence_index ALTER COLUMN embedding   TYPE vector(1536) USING NULL::vector(1536)",
]


def upgrade():
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade():
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
