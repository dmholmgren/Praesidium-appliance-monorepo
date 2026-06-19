"""0080: trial transcript substrate -- generalize the depo pipeline to trials.

Scope: Trial Transcript & Appellate Brief v1, Module A / Unit 1. The deposition
evidence pipeline (0072-0073) already holds a kind-agnostic spine: transcript_lines
(page:line addressing), transcript_qa_units (the embeddable exchange/span), and
deposition_stage_status (the ledger-as-queue DAG). A trial transcript is the same
animal -- just multi-volume, multi-witness, and event-typed. So we GENERALIZE in
place rather than fork (Open Decision #2: shared substrate, transcript_kind
discriminator) -- the depo module stays consumer #1, trial becomes consumer #2.

Additive + idempotent (ADD COLUMN IF NOT EXISTS): no rewrite of the live depo
tables, no data migration. Deltas vs the deposition pipeline:

  deposition_transcripts:
    transcript_kind  discriminator {deposition, trial, hearing}; default
                     'deposition' so every existing row keeps its identity.
    trial_id         groups the N volumes of one trial (-> trial_proceedings).
    volume           Reporter's Record volume number. Citations resolve as
                     '[volume] RR [page]:[line]'. Each RR volume is its own
                     transcript row -> the DAG fans out per volume (A3).
    trial_day        filing/proceeding day within the trial.
    title            human label for the volume (depo uses deponent).

  transcript_qa_units:
    event_type       trial event taxonomy on the span {voir_dire, opening,
                     direct, cross, redirect, recross, colloquy, objection,
                     ruling, bench_conference, charge, closing, verdict,
                     proceedings}. NULL for depositions (Q&A only).

  trial_proceedings (new parent):
    one row per trial = the handle Module B pulls the Reporter's Record by.
    The trial analog of viaticum_sessions, but Alembic-managed + uuid.
"""
from alembic import op


revision = "0080_trial_transcript_substrate"
down_revision = "0079_triage_assistant"
branch_labels = None
depends_on = None


def upgrade():
    # --- trial parent -------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS trial_proceedings (
            id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id              char(36) NOT NULL,
            matter_id              uuid,
            caption                text,
            cause_number           varchar(120),
            trial_court            varchar(200),
            court_of_appeals       varchar(200),
            appellate_cause_number varchar(120),
            date_start             date,
            date_end               date,
            notes                  text,
            created_at             timestamptz NOT NULL DEFAULT now(),
            updated_at             timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_trial_proceedings_matter "
               "ON trial_proceedings (matter_id)")

    # --- generalize deposition_transcripts ---------------------------------
    op.execute("ALTER TABLE deposition_transcripts "
               "ADD COLUMN IF NOT EXISTS transcript_kind varchar(20) "
               "NOT NULL DEFAULT 'deposition'")
    op.execute("ALTER TABLE deposition_transcripts "
               "ADD COLUMN IF NOT EXISTS trial_id uuid")
    op.execute("ALTER TABLE deposition_transcripts "
               "ADD COLUMN IF NOT EXISTS volume integer")
    op.execute("ALTER TABLE deposition_transcripts "
               "ADD COLUMN IF NOT EXISTS trial_day integer")
    op.execute("ALTER TABLE deposition_transcripts "
               "ADD COLUMN IF NOT EXISTS title text")
    op.execute("CREATE INDEX IF NOT EXISTS ix_deposition_transcripts_kind "
               "ON deposition_transcripts (transcript_kind)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_deposition_transcripts_trial "
               "ON deposition_transcripts (trial_id)")
    # FK is best-effort: keep volumes if a trial row is removed.
    op.execute("""
        DO $$ BEGIN
            ALTER TABLE deposition_transcripts
                ADD CONSTRAINT fk_deposition_transcripts_trial
                FOREIGN KEY (trial_id) REFERENCES trial_proceedings(id)
                ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)

    # --- event taxonomy on the span ----------------------------------------
    op.execute("ALTER TABLE transcript_qa_units "
               "ADD COLUMN IF NOT EXISTS event_type varchar(32)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_transcript_qa_units_event "
               "ON transcript_qa_units (transcript_id, event_type)")


def downgrade():
    op.execute("ALTER TABLE transcript_qa_units DROP COLUMN IF EXISTS event_type")
    op.execute("ALTER TABLE deposition_transcripts "
               "DROP CONSTRAINT IF EXISTS fk_deposition_transcripts_trial")
    op.execute("ALTER TABLE deposition_transcripts DROP COLUMN IF EXISTS title")
    op.execute("ALTER TABLE deposition_transcripts DROP COLUMN IF EXISTS trial_day")
    op.execute("ALTER TABLE deposition_transcripts DROP COLUMN IF EXISTS volume")
    op.execute("ALTER TABLE deposition_transcripts DROP COLUMN IF EXISTS trial_id")
    op.execute("ALTER TABLE deposition_transcripts "
               "DROP COLUMN IF EXISTS transcript_kind")
    op.execute("DROP TABLE IF EXISTS trial_proceedings")
