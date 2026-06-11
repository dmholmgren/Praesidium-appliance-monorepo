"""0041 case-intelligence layer: embeddings, COA uuid recreate, proof-of-claim graph.

Builds on 0040 (canonical spine). Three blocks, ordered by risk:

  1. ADDITIVE -- embedding columns (safe; pgvector restored post-rebuild):
       canonical_entities.embedding/embedded_at, document_sections.embedding/embedded_at
       (ADD COLUMN nullable / no default == metadata-only, no table rewrite even on
        the ~54k document_sections rows.)

  2. DESTRUCTIVE -- causes_of_action / coa_elements bigint -> uuid recreate.
       *** DROPS the existing causes_of_action rows (3,390 at write time) and
           coa_elements (0). Safe ONLY because the universal-pipeline reparse
           regenerates these primitives. Do NOT apply until you have accepted that. ***
       Only inbound FK is coa_elements -> causes_of_action (both recreated together);
       no external table or view references either, so the drop is clean.
       Recreated to standard: uuid PK, cause_library_id FK, section_id, source_doc_id,
       confidence, attribution, run-versioning (extraction_run_id/superseded_by_run_id),
       embedding, attributes jsonb.

  3. NET-NEW -- the case-intelligence / proof-of-claim graph (matter-scoped, seed
     run-versioned, FK the *recreated* coa_elements):
       case_topics            -- CASE axis: per-matter disputed topics (frontier-derived)
       allegations            -- bridge primitive: case_topic -> allegation -> coa_element
       document_topic_segments-- multi-axis overlay (ACT / LAW / CASE), polymorphic
                                 primitive_type/primitive_id like the 0040 chunk linkage

Run-versioning naming encodes the two-pipeline split on purpose:
  - universal-pipeline primitives (causes_of_action, coa_elements) carry
    `extraction_run_id` (same column name document_sections uses).
  - matter-intelligence / seed products (case_topics, allegations,
    document_topic_segments) carry `seed_run_id` -- "seed-apply tags record the
    seed version" so a drift refresh re-tags only affected docs.

Provenance / run / section / source columns follow the 0040 precedent: plain `uuid`
+ index, NO hard FK (avoids insert-ordering coupling during bulk reparse). Only the
intra-graph structural links and library links are real FKs.

NOT in this migration (pure data, deferred to a backfill once the local model can
embed): contacts -> spine seed + dedup hygiene run.

asyncpg: one statement per op.execute(); a DO $$..$$ block counts as one statement.
All DDL is guarded (IF [NOT] EXISTS) so re-running is a no-op, EXCEPT the block-2
DROP/CREATE recreate, which is intentionally destructive-by-design.

Assumes the `vector` type is present (pgvector 0.8.2 reinstalled post-rebuild).
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0041_case_intel"
down_revision = "0040_canonical_spine"
branch_labels = None
depends_on = None


UPGRADE_STATEMENTS = [
    # ====================================================================
    # 1. ADDITIVE -- embedding columns (deferred out of 0040 when pgvector
    #    was down). Nullable / no default => metadata-only, no rewrite.
    # ====================================================================
    "ALTER TABLE IF EXISTS canonical_entities ADD COLUMN IF NOT EXISTS embedding   vector(1024)",
    "ALTER TABLE IF EXISTS canonical_entities ADD COLUMN IF NOT EXISTS embedded_at timestamptz",
    "ALTER TABLE IF EXISTS document_sections  ADD COLUMN IF NOT EXISTS embedding   vector(1024)",
    "ALTER TABLE IF EXISTS document_sections  ADD COLUMN IF NOT EXISTS embedded_at timestamptz",
    # ANN (hnsw) indexes intentionally deferred until the columns are populated
    # by the embedding pipeline -- built empty here would just be dead weight.

    # ====================================================================
    # 2. DESTRUCTIVE -- causes_of_action / coa_elements bigint -> uuid recreate.
    #    Child dropped first. Owned sequences drop with the tables.
    # ====================================================================
    "DROP TABLE IF EXISTS coa_elements",
    "DROP TABLE IF EXISTS causes_of_action",
    """
    CREATE TABLE causes_of_action (
        id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id           text NOT NULL,
        matter_id           uuid NOT NULL,
        cause_library_id    uuid REFERENCES cause_of_action_library(id),
        section_id          uuid,
        source_doc_id       uuid,
        title               varchar(500) NOT NULL,
        count_number        integer,
        status              varchar(50) DEFAULT 'not_developed',
        ai_summary          text,
        confidence          double precision,
        attribution         varchar(32),
        extraction_run_id   uuid,
        superseded_by_run_id uuid,
        embedding           vector(1024),
        embedded_at         timestamptz,
        attributes          jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at          timestamptz NOT NULL DEFAULT now(),
        updated_at          timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_causes_of_action_matter   ON causes_of_action (tenant_id, matter_id)",
    "CREATE INDEX ix_causes_of_action_library  ON causes_of_action (cause_library_id)",
    "CREATE INDEX ix_causes_of_action_section  ON causes_of_action (section_id)",
    "CREATE INDEX ix_causes_of_action_run      ON causes_of_action (extraction_run_id)",
    """
    CREATE TABLE coa_elements (
        id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id                text NOT NULL,
        cause_of_action_id       uuid NOT NULL REFERENCES causes_of_action(id) ON DELETE CASCADE,
        element_name             varchar(500) NOT NULL,
        status                   varchar(50) DEFAULT 'not_developed',
        supporting_evidence      jsonb,
        undermining_evidence     jsonb,
        discovery_gaps           jsonb,
        pending_motions          jsonb,
        section_id               uuid,
        source_doc_id            uuid,
        confidence               double precision,
        attribution              varchar(32),
        extraction_run_id        uuid,
        superseded_by_run_id     uuid,
        embedding                vector(1024),
        attributes               jsonb NOT NULL DEFAULT '{}'::jsonb,
        attorney_override_status varchar(50),
        attorney_override_note   text,
        overridden_by            bigint,
        overridden_at            timestamptz,
        created_at               timestamptz NOT NULL DEFAULT now(),
        updated_at               timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_coa_elements_coa     ON coa_elements (cause_of_action_id)",
    "CREATE INDEX ix_coa_elements_section ON coa_elements (section_id)",
    "CREATE INDEX ix_coa_elements_run     ON coa_elements (extraction_run_id)",
    "GRANT ALL ON causes_of_action TO praesidium",
    "GRANT ALL ON coa_elements     TO praesidium",

    # ====================================================================
    # 3. NET-NEW -- case-intelligence / proof-of-claim graph
    # ====================================================================

    # ---- 3a. case_topics (CASE axis) -----------------------------------
    """
    CREATE TABLE IF NOT EXISTS case_topics (
        id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id            text NOT NULL,
        matter_id            uuid NOT NULL,
        topic_label          text NOT NULL,
        topic_type           varchar(48),
        description          text,
        status               varchar(32) NOT NULL DEFAULT 'proposed',
        confidence           double precision,
        attribution          varchar(32),
        seed_run_id          uuid,
        superseded_by_run_id uuid,
        embedding            vector(1024),
        attributes           jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at           timestamptz NOT NULL DEFAULT now(),
        updated_at           timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_case_topics_matter ON case_topics (tenant_id, matter_id)",
    "CREATE INDEX IF NOT EXISTS ix_case_topics_run    ON case_topics (seed_run_id)",

    # ---- 3b. allegations (bridge primitive) ----------------------------
    #   proof-of-claim graph: document -> allegation -> coa_element -> cause
    """
    CREATE TABLE IF NOT EXISTS allegations (
        id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id            text NOT NULL,
        matter_id            uuid NOT NULL,
        case_topic_id        uuid REFERENCES case_topics(id) ON DELETE SET NULL,
        coa_element_id       uuid REFERENCES coa_elements(id) ON DELETE SET NULL,
        allegation_text      text NOT NULL,
        allegation_type      varchar(48),
        source_doc_id        uuid,
        section_id           uuid,
        source_char_start    integer,
        source_char_end      integer,
        status               varchar(32) NOT NULL DEFAULT 'proposed',
        confidence           double precision,
        attribution          varchar(32),
        seed_run_id          uuid,
        superseded_by_run_id uuid,
        embedding            vector(1024),
        attributes           jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at           timestamptz NOT NULL DEFAULT now(),
        updated_at           timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_allegations_matter  ON allegations (tenant_id, matter_id)",
    "CREATE INDEX IF NOT EXISTS ix_allegations_topic   ON allegations (case_topic_id)",
    "CREATE INDEX IF NOT EXISTS ix_allegations_element ON allegations (coa_element_id)",
    "CREATE INDEX IF NOT EXISTS ix_allegations_section ON allegations (section_id)",
    "CREATE INDEX IF NOT EXISTS ix_allegations_run     ON allegations (seed_run_id)",

    # ---- 3c. document_topic_segments (multi-axis overlay) --------------
    #   One segment is taggable on up to one tag per orthogonal axis:
    #     ACT  -> act_code (UTBMS)
    #     LAW  -> cause/clause library FK + raw SALI/FOLIO IRI (litigation/transactional fork)
    #     CASE -> case_topic_id
    #   Polymorphic primitive_type/primitive_id mirrors the 0040 chunk linkage.
    """
    CREATE TABLE IF NOT EXISTS document_topic_segments (
        id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id             text NOT NULL,
        matter_id             uuid NOT NULL,
        source_doc_id         uuid,
        section_id            uuid,
        primitive_type        varchar(32),
        primitive_id          uuid,
        segment_index         integer,
        char_start            integer,
        char_end              integer,
        act_code              varchar(32),
        law_cause_library_id  uuid REFERENCES cause_of_action_library(id),
        law_clause_library_id uuid REFERENCES clause_type_library(id),
        law_sali_iri          text,
        law_folio_iri         text,
        case_topic_id         uuid REFERENCES case_topics(id) ON DELETE SET NULL,
        confidence            double precision,
        attribution           varchar(32),
        seed_run_id           uuid,
        superseded_by_run_id  uuid,
        attributes            jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at            timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_doc_topic_seg_matter    ON document_topic_segments (tenant_id, matter_id)",
    "CREATE INDEX IF NOT EXISTS ix_doc_topic_seg_primitive ON document_topic_segments (primitive_type, primitive_id)",
    "CREATE INDEX IF NOT EXISTS ix_doc_topic_seg_section   ON document_topic_segments (section_id)",
    "CREATE INDEX IF NOT EXISTS ix_doc_topic_seg_topic     ON document_topic_segments (case_topic_id)",
    "CREATE INDEX IF NOT EXISTS ix_doc_topic_seg_run       ON document_topic_segments (seed_run_id)",

    "GRANT ALL ON case_topics             TO praesidium",
    "GRANT ALL ON allegations             TO praesidium",
    "GRANT ALL ON document_topic_segments TO praesidium",
]


DOWNGRADE_STATEMENTS = [
    # ---- 3. drop net-new graph -----------------------------------------
    "DROP TABLE IF EXISTS document_topic_segments",
    "DROP TABLE IF EXISTS allegations",
    "DROP TABLE IF EXISTS case_topics",

    # ---- 2. restore prior (bigint) shape of the recreated tables -------
    #   Data is NOT restored (it was disposable / reparse-regenerated). This
    #   only re-establishes the pre-0041 schema shape so the chain is reversible.
    "DROP TABLE IF EXISTS coa_elements",
    "DROP TABLE IF EXISTS causes_of_action",
    """
    CREATE TABLE causes_of_action (
        id           bigserial PRIMARY KEY,
        tenant_id    varchar(36) NOT NULL,
        matter_id    uuid NOT NULL,
        title        varchar(500) NOT NULL,
        count_number integer,
        status       varchar(50) DEFAULT 'not_developed',
        ai_summary   text,
        created_at   timestamp NOT NULL DEFAULT now(),
        updated_at   timestamp NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE coa_elements (
        id                       bigserial PRIMARY KEY,
        tenant_id                varchar(36) NOT NULL,
        cause_of_action_id       bigint NOT NULL REFERENCES causes_of_action(id),
        element_name             varchar(500) NOT NULL,
        status                   varchar(50) DEFAULT 'not_developed',
        supporting_evidence      jsonb,
        undermining_evidence     jsonb,
        discovery_gaps           jsonb,
        pending_motions          jsonb,
        attorney_override_status varchar(50),
        attorney_override_note   text,
        overridden_by            bigint,
        overridden_at            timestamp,
        created_at               timestamp NOT NULL DEFAULT now(),
        updated_at               timestamp NOT NULL DEFAULT now()
    )
    """,
    "GRANT ALL ON causes_of_action TO praesidium",
    "GRANT ALL ON coa_elements     TO praesidium",
    "GRANT ALL ON SEQUENCE causes_of_action_id_seq TO praesidium",
    "GRANT ALL ON SEQUENCE coa_elements_id_seq     TO praesidium",

    # ---- 1. drop embedding columns -------------------------------------
    "ALTER TABLE IF EXISTS document_sections  DROP COLUMN IF EXISTS embedded_at",
    "ALTER TABLE IF EXISTS document_sections  DROP COLUMN IF EXISTS embedding",
    "ALTER TABLE IF EXISTS canonical_entities DROP COLUMN IF EXISTS embedded_at",
    "ALTER TABLE IF EXISTS canonical_entities DROP COLUMN IF EXISTS embedding",
]


def upgrade():
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade():
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
