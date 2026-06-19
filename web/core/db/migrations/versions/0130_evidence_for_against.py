"""0129: evidence_for_against -- reusable "Evidence For & Against" widget substrate.

A generic, corpus-agnostic version of the appellate Record Fact-Element Classifier
(record_fact_element_links, 0094). The same shape -- a SPINE of claim-items on the
left, FOR / AGAINST record evidence on the right -- is needed across matters with
different inputs:

  * appellate : pleading -> causes of action + ELEMENTS, against the RECORD (CR+RR)
  * trial     : pleading -> causes/elements, against E-DISCOVERY
  * trial     : proposed FINDINGS OF FACT & CONCLUSIONS OF LAW, against E-DISCOVERY

Every corpus already embeds into the SAME ModernBERT-768 space (coa_elements,
document_sections, transcript_qa_embeddings, ediscovery_chunk_embeddings.embedding_768),
so one engine works for all: spine-item embedding -> top-K 768-neighbors in the chosen
corpus -> a FRONTIER (claude-opus-4-8) call classifies each neighbor FOR/AGAINST/NEUTRAL
with a pulled quote + cite + rationale.

This migration adds the three generic tables + the platform ai_model_routing row that
makes the for/against assessment a frontier (opus-4-8) call.

Revision ID: 0129_evidence_for_against
Revises: 0129_pst_import
"""
from alembic import op


revision = "0130_evidence_for_against"
down_revision = "0129_pst_import"
branch_labels = None
depends_on = None


def upgrade():
    # one instance per (matter, spine source, corpus)
    op.execute("""
        CREATE TABLE IF NOT EXISTS evidence_spines (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         varchar(64) NOT NULL,
            matter_id         uuid NOT NULL,
            appellate_case_id uuid,
            spine_kind        varchar(24) NOT NULL,   -- pleading_coa | ffcl | passage
            corpus            varchar(24) NOT NULL,   -- record | ediscovery | transcript | trial_evidence
            label             varchar(300),
            source_ref        jsonb NOT NULL DEFAULT '{}'::jsonb,
            status            varchar(16) NOT NULL DEFAULT 'new',  -- new|building|seeded|running|ready|error
            last_run_id       uuid,
            note              text,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_evspine_matter "
               "ON evidence_spines (matter_id, spine_kind, corpus)")

    # left-column rows: elements / findings / conclusions / passages
    op.execute("""
        CREATE TABLE IF NOT EXISTS evidence_spine_items (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            spine_id      uuid NOT NULL REFERENCES evidence_spines(id) ON DELETE CASCADE,
            group_label   varchar(300),               -- cause title | "Findings of Fact" | "Conclusions of Law"
            group_no      integer,
            item_no       integer,
            item_role     varchar(16) NOT NULL,       -- element | finding | conclusion | passage
            item_text     text NOT NULL,
            embedding     vector(768),
            source_kind   varchar(24),                -- coa_element | ffcl_para | passage
            source_ref_id uuid,
            attributes    jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at    timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_evitem_spine "
               "ON evidence_spine_items (spine_id, group_no, item_no)")

    # for/against evidence -- the frontier (opus-4-8) verdict per (item, evidence)
    op.execute("""
        CREATE TABLE IF NOT EXISTS evidence_links (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       varchar(64) NOT NULL,
            spine_item_id   uuid NOT NULL REFERENCES evidence_spine_items(id) ON DELETE CASCADE,
            corpus          varchar(24) NOT NULL,
            evidence_kind   varchar(24),              -- cr_section|rr_qa|edisc_chunk|transcript_qa|trial_exhibit
            evidence_ref_id uuid,
            relation        varchar(12) NOT NULL,     -- supports | undermines | neutral
            quote           text,
            rationale       text,
            cite            text,
            snippet         text,
            confidence      double precision,
            cosine          double precision,
            tier            smallint,                 -- 3 = frontier
            status          varchar(12) NOT NULL DEFAULT 'proposed',
            frontier_run_id uuid,
            created_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_evlink_item "
               "ON evidence_links (spine_item_id, relation, status)")

    # the frontier call: route module=intelligence purpose=evidence_for_against -> opus-4-8
    op.execute("""
        INSERT INTO ai_model_routing
            (id, tenant_id, module, purpose, primary_model, fallback_model, max_tokens,
             warning_thresholds, override_policy, version, status, created_at, updated_at)
        SELECT gen_random_uuid(), NULL, 'intelligence', 'evidence_for_against',
               'claude-opus-4-8', 'claude-sonnet-4-6', 4096,
               '[0.75, 0.90]', '{"allow_override": false}', 1, 'published', now(), now()
        WHERE NOT EXISTS (
            SELECT 1 FROM ai_model_routing
            WHERE module='intelligence' AND purpose='evidence_for_against' AND tenant_id IS NULL
        )
    """)


def downgrade():
    op.execute("DELETE FROM ai_model_routing WHERE module='intelligence' "
               "AND purpose='evidence_for_against' AND tenant_id IS NULL")
    op.execute("DROP TABLE IF EXISTS evidence_links")
    op.execute("DROP TABLE IF EXISTS evidence_spine_items")
    op.execute("DROP TABLE IF EXISTS evidence_spines")
