"""0119: AI-guided backfill seeding sessions (Court/Hearing 6 / 10).

The seeding session is interactive and LLM-guided, and a trial-bound
reconstruction is only defensible if the conversation that produced it is
persisted (non-negotiable 3: chain-of-custody applies to reasoning). This table
is that record.

  seeding_sessions - one LIVE session per matter (superseded_by_id IS NULL).
    Re-seeding supersedes the prior session rather than overwriting it.
      status      collecting | proposed | narrated | ratified | superseded
      collectors  per-witness run summary (doc / calendar / time signal counts)
      proposal    snapshot of the proposed hearings + reschedule chains (the
                  confirmable set) BEFORE any commit to the hearings primitive
      transcript  append-only [{turn, role, text, at}] - the LLM narration and
                  every attorney revise note (the persisted reasoning)
      ratified    the attorney's per-proposal decisions + committed hearing ids

The commit itself still lands in the hearings / hearing_reschedules primitives
via the deterministic resolver path with provenance.locked = true.
"""
from alembic import op


revision = "0119_seeding_sessions"
down_revision = "0118_annotation_objects"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS seeding_sessions (
            id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       char(36)    NOT NULL,
            matter_id       uuid        NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
            status          varchar(16) NOT NULL DEFAULT 'collecting',
                -- collecting | proposed | narrated | ratified | superseded
            collectors      jsonb       NOT NULL DEFAULT '{}'::jsonb,
            proposal        jsonb       NOT NULL DEFAULT '{}'::jsonb,
            transcript      jsonb       NOT NULL DEFAULT '[]'::jsonb,
            narration       text,
            model           varchar(64),
            input_tokens    integer,
            output_tokens   integer,
            ratified        jsonb       NOT NULL DEFAULT '{}'::jsonb,
            created_by      bigint,
            ratified_by     bigint,
            superseded_by_id uuid       REFERENCES seeding_sessions(id) ON DELETE SET NULL,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now()
        );
    """)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_seeding_sessions_live_matter "
        "ON seeding_sessions (matter_id) WHERE superseded_by_id IS NULL;")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_seeding_sessions_matter "
        "ON seeding_sessions (matter_id, status);")
    # superuser-created table -> grant to the runtime role
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='praesidium_db') THEN
                GRANT ALL ON seeding_sessions TO praesidium_db;
            END IF;
        END $$;
    """)
    # routing for the seeding narration (the deliberate AI exception). Reuses
    # the case_seed tier ethos; sonnet primary, haiku fallback.
    op.execute("""
        INSERT INTO ai_model_routing
            (tenant_id, module, purpose, primary_model, fallback_model,
             max_tokens, version, status)
        SELECT NULL, 'intelligence', 'hearing_seed', 'claude-sonnet-4-6',
               'claude-haiku-4-5-20251001', 1500, 1, 'published'
        WHERE NOT EXISTS (
            SELECT 1 FROM ai_model_routing
            WHERE module='intelligence' AND purpose='hearing_seed'
              AND tenant_id IS NULL);
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS seeding_sessions;")
    op.execute("DELETE FROM ai_model_routing WHERE module='intelligence' "
               "AND purpose='hearing_seed' AND tenant_id IS NULL;")
