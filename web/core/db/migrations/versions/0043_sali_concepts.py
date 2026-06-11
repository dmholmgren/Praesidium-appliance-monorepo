"""0043 sali_concepts: canonical SALI LMSS reference table (Postgres = system of record).

SALI currently lives only in the taxonomy-redis HNSW store. That inverts the
"everything is data / Postgres is the SoR" doctrine: the canonical codes belong
in Postgres, with the Redis vector index (and now pgvector) as a *regenerable
projection*. This migration creates the canonical home; the loader lifts the
codes out of Redis into here, and the embedding pass fills `embedding`.

Cross-tenant reference layer -- NO tenant_id (same pattern as reference_courts /
reference_opinions in 0059). One row per SALI concept, keyed by its stable IRI.

Embedding space: vector(1024) == voyage-law-2, matching document_sections,
allegations, case_topics, causes_of_action, coa_elements. This is the load-bearing
choice: the whole draft-then-edit pipeline is cross-kNN in ONE space
  - allegation.embedding   (1024)  -> kNN -> sali_concepts.embedding (1024)   [Job A draft]
  - discovery section.embedding (1024) -> kNN -> grounded exemplars (1024)     [Job B bulk]
A dimension mismatch here silently breaks both. 1024 is mandatory, not a default.

`branch` is the facet pre-filter (SALI top-level: 'area_of_law', 'document_type',
'industry', 'player', ...). The issue/case work filters branch='area_of_law' so it
never retrieves the doctype noise; the existing doctype classifier filters
branch='document_type'. One table, both axes, separated by a cheap btree.

Polyhierarchy: SALI concepts can have multiple broaders, so `parent_iris text[]`
rather than a single self-FK -- and no hard FK at all, matching 0041's precedent
of plain columns + index to avoid insert-ordering coupling during bulk load.

HNSW index DEFERRED (0041 precedent): an ANN index over an empty/NULL embedding
column is dead weight. The embedding pass builds it after populating vectors. The
exact statement is recorded below and in DEFERRED_HNSW for that pass to run:

    CREATE INDEX IF NOT EXISTS ix_sali_concepts_embedding
        ON sali_concepts USING hnsw (embedding vector_cosine_ops);

asyncpg: one statement per op.execute(). All DDL guarded (IF [NOT] EXISTS) so
re-running is a no-op. Applied as praesidium (the app role owns the schema).
Assumes the `vector` type is present (pgvector 0.8.2).
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0043_sali_concepts"
down_revision = "0042_prop_event_date"
branch_labels = None
depends_on = None


# Recorded for the embedding pass to run once `embedding` is populated. NOT run
# in this migration (deferring an ANN index over an empty column, per 0041).
DEFERRED_HNSW = (
    "CREATE INDEX IF NOT EXISTS ix_sali_concepts_embedding "
    "ON sali_concepts USING hnsw (embedding vector_cosine_ops)"
)


UPGRADE_STATEMENTS = [
    # ---- canonical table -------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS sali_concepts (
        iri            text PRIMARY KEY,                 -- stable SALI IRI
        sali_code      text,                             -- short code, if any
        pref_label     text NOT NULL,
        alt_labels     text[],                           -- synonyms / hidden labels
        definition     text,                             -- load-bearing: embedded with the label
        branch         text NOT NULL,                    -- facet: area_of_law | document_type | ...
        parent_iris    text[],                           -- polyhierarchy (no hard FK by design)
        depth          integer,
        is_active      boolean NOT NULL DEFAULT true,
        embedding      vector(1024),                     -- voyage-law-2 space; filled by embedding pass
        embedded_at    timestamptz,
        source_version text,                             -- provenance (LMSS release / redis-lift tag)
        attributes     jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at     timestamptz NOT NULL DEFAULT now(),
        updated_at     timestamptz NOT NULL DEFAULT now()
    )
    """,

    # ---- lookup / pre-filter indexes (btree + GIN) -----------------------
    # branch is THE pre-filter for retrieval (area_of_law vs document_type).
    "CREATE INDEX IF NOT EXISTS ix_sali_concepts_branch   ON sali_concepts (branch)",
    "CREATE INDEX IF NOT EXISTS ix_sali_concepts_active   ON sali_concepts (branch, is_active)",
    "CREATE INDEX IF NOT EXISTS ix_sali_concepts_code     ON sali_concepts (sali_code)",
    # array membership: synonym hard-match and hierarchy navigation.
    "CREATE INDEX IF NOT EXISTS ix_sali_concepts_altlabels ON sali_concepts USING gin (alt_labels)",
    "CREATE INDEX IF NOT EXISTS ix_sali_concepts_parents   ON sali_concepts USING gin (parent_iris)",

    # ---- ownership -------------------------------------------------------
    "GRANT ALL ON sali_concepts TO praesidium",

    # NOTE: HNSW index (DEFERRED_HNSW above) is intentionally NOT created here.
    # The embedding pass builds it after populating `embedding`.
]


DOWNGRADE_STATEMENTS = [
    # HNSW (if the embedding pass created it) drops with the table; explicit
    # for clarity in case the table drop is ever reordered.
    "DROP INDEX IF EXISTS ix_sali_concepts_embedding",
    "DROP TABLE IF EXISTS sali_concepts",
]


def upgrade():
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade():
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
