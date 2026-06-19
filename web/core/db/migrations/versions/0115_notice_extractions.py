"""0115: notice/order extractor output (Court/Hearing build, Step 2 §4.2).

One extractor serves reschedule capture, transcript routing, and the trial
dashboard. Given a court document the persisted classifier (0113) typed as
notice | order | scheduling_order | subpoena, it produces a structured record:
hearing_type, new_date, prior_date (if a reset), moving_party, plus court /
judge / cause_number. That structured product lands in hearing_notice_extractions
(this table, with the supersede/correct chain), and each extracted candidate date
is also written to hearing_signals (the §3 evidence ledger) so reconciliation has
auditable, idempotent, re-runnable evidence.

hearing_signals is extended here with the few columns the extractor needs to
stamp an evidence row to its producing extraction without bloating the ledger.
"""
from alembic import op


revision = "0115_notice_extractions"
down_revision = "0114_calendar_classify"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS hearing_notice_extractions (
            id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          char(36)    NOT NULL,
            dms_document_id    uuid        NOT NULL
                                   REFERENCES dms_documents(id) ON DELETE CASCADE,
            matter_id          uuid        REFERENCES matters(id) ON DELETE SET NULL,
            doc_type           varchar(24) NOT NULL,   -- classifier taxonomy code
            hearing_type       varchar(80),
            new_date           timestamptz,
            prior_date         timestamptz,
            is_reset           boolean     NOT NULL DEFAULT false,
            moving_party       varchar(255),
            court              varchar(255),
            judge              varchar(255),
            cause_number       varchar(100),
            all_dates          jsonb       NOT NULL DEFAULT '[]'::jsonb,
            confidence         numeric,
            method             varchar(16),            -- rule | ai | hybrid | manual
            model              varchar(100),
            extracted          jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- raw model output
            extractor_run_id   uuid        REFERENCES extraction_runs(id) ON DELETE SET NULL,
            superseded_by_id   uuid        REFERENCES hearing_notice_extractions(id)
                                   ON DELETE SET NULL,
            reviewed_by        bigint,
            reviewed_at        timestamptz,
            created_at         timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_notice_extr_doc "
               "ON hearing_notice_extractions (dms_document_id) "
               "WHERE superseded_by_id IS NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_notice_extr_matter "
               "ON hearing_notice_extractions (tenant_id, matter_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_notice_extr_reset "
               "ON hearing_notice_extractions (tenant_id, is_reset) "
               "WHERE is_reset")

    # link each ledger signal back to the extraction that produced it, and make
    # the signal idempotent per (document, candidate_date, signal_type).
    op.execute("ALTER TABLE hearing_signals "
               "ADD COLUMN IF NOT EXISTS source_document_id uuid "
               "REFERENCES dms_documents(id) ON DELETE CASCADE")
    op.execute("ALTER TABLE hearing_signals "
               "ADD COLUMN IF NOT EXISTS extraction_id uuid "
               "REFERENCES hearing_notice_extractions(id) ON DELETE CASCADE")
    op.execute("ALTER TABLE hearing_signals "
               "ADD COLUMN IF NOT EXISTS date_role varchar(16)")  # new | prior | filing
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_hearing_signal_doc_date "
               "ON hearing_signals (source_document_id, candidate_date, signal_type) "
               "WHERE source_document_id IS NOT NULL")

    op.execute("""
        INSERT INTO ai_model_routing
            (tenant_id, module, purpose, primary_model, fallback_model,
             max_tokens, status)
        SELECT NULL, 'classification', 'notice_extract',
               'claude-sonnet-4-6', 'claude-haiku-4-5-20251001', 300, 'published'
        WHERE NOT EXISTS (
            SELECT 1 FROM ai_model_routing
            WHERE module='classification' AND purpose='notice_extract'
              AND tenant_id IS NULL)
    """)


def downgrade():
    op.execute("DELETE FROM ai_model_routing WHERE module='classification' "
               "AND purpose='notice_extract'")
    op.execute("DROP INDEX IF EXISTS uq_hearing_signal_doc_date")
    op.execute("ALTER TABLE hearing_signals DROP COLUMN IF EXISTS date_role")
    op.execute("ALTER TABLE hearing_signals DROP COLUMN IF EXISTS extraction_id")
    op.execute("ALTER TABLE hearing_signals DROP COLUMN IF EXISTS source_document_id")
    op.execute("DROP TABLE IF EXISTS hearing_notice_extractions")
