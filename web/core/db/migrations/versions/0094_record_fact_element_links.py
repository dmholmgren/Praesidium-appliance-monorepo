"""0094: record_fact_element_links -- Record Fact-Element Classifier / U9.2.

The classifier's output primitive (mirrors `allegations`' provenance/status discipline,
but over the CLOSED appellate record). One row links a record FACT UNIT -- a CR
paragraph (document_sections.cr_para) or an RR Q&A unit (transcript_qa_units) -- to a
`coa_elements` row of the frame-locked spine (U9.1), with the relation, the cascade
tier that assigned it (0 deterministic / 1 embedding / 2 frontier), confidence, the
record cite, and provenance offsets. Accepted links roll up into
coa_elements.supporting_evidence / undermining_evidence (U9.4) -> the element-coverage
/ legal-sufficiency matrix.
"""
from alembic import op


revision = "0094_record_fact_element_links"
down_revision = "0093_depositions_nav_item"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS record_fact_element_links (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          char(36) NOT NULL,
            matter_id          uuid,
            appellate_case_id  uuid,
            fact_kind          varchar(16),     -- cr_section | rr_qa
            fact_ref_id        uuid,            -- document_sections.id OR transcript_qa_units.id
            coa_element_id     uuid REFERENCES coa_elements(id) ON DELETE CASCADE,
            cause_of_action_id uuid,            -- denormalized for query
            relation           varchar(12) NOT NULL DEFAULT 'supports',  -- supports|undermines|neutral
            confidence         double precision,
            margin             double precision,    -- top1 - top2 element similarity
            tier               smallint,            -- 0 | 1 | 2
            needs_escalation   boolean NOT NULL DEFAULT false,
            sali_iri           text,
            rationale          text,                -- T2 frontier rationale
            record_cite        text,                -- '[vol] RR p:l' | 'CR p'
            char_start         integer, char_end integer,
            status             varchar(12) NOT NULL DEFAULT 'proposed',  -- proposed|accepted|rejected
            classify_run_id    uuid,
            superseded_by_run_id uuid,
            created_at         timestamptz NOT NULL DEFAULT now(),
            updated_at         timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_rfel_element "
               "ON record_fact_element_links (matter_id, coa_element_id, relation)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_rfel_fact "
               "ON record_fact_element_links (matter_id, fact_ref_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_rfel_escalate "
               "ON record_fact_element_links (appellate_case_id, tier, needs_escalation)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_rfel_cause "
               "ON record_fact_element_links (cause_of_action_id, relation)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS record_fact_element_links")
