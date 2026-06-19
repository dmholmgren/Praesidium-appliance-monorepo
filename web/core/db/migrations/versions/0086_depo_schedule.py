"""0085: depo_schedule -- minimal scheduling primitive for the Depositions homepage.

Tracks a deposition across its lifecycle and bridges the runtime + transcript objects.
viaticum_sessions has only runtime started_at/closed_at (no calendar fields), so the
Schedule panel needs its own substrate.

Lifecycle closure (one chain, three tables):
    depo_schedule (planned) -> viaticum_sessions (conducted) -> deposition_transcripts (ingested)
The Schedule panel renders depo_schedule rows; the Transcripts panel renders the
downstream end of the same chain via linked_transcript_id.

Built forward-compatible for the Phase 2 proposal engine (scope-and-hold): the engine
writes candidate slots into proposed_dates with status='proposing' -- no schema change
to the panel, because the full status enum and proposed_dates jsonb already exist here.
"""
from alembic import op


revision = "0086_depo_schedule"
down_revision = "0085_entry_type_registry"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS depo_schedule (
            id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id             char(36) NOT NULL,          -- TRIM in all comparisons
            matter_id             uuid NOT NULL,
            deponent              text,                       -- or witness_contact_id later
            depo_role             text,                       -- fact / expert / corporate_rep ...
            status                text NOT NULL DEFAULT 'requested',
                -- requested | proposing | noticed | confirmed | rescheduled | completed | cancelled
            proposed_dates        jsonb,                      -- Phase 2: [{start,end,score,conflicts}]
            scheduled_start       timestamptz,
            scheduled_end         timestamptz,
            duration_est_minutes  int,
            location              text,
            is_remote             boolean NOT NULL DEFAULT false,
            remote_url            text,
            noticing_party        text,
            defending_party       text,
            court_reporter        text,                       -- contact id later
            videographer          text,
            discovery_cutoff      date,                       -- denormalized from matter deadline
            linked_session_id     bigint,                     -- viaticum_sessions when conducted
            linked_transcript_id  uuid,                       -- deposition_transcripts when ingested
            notes                 text,
            created_by            bigint,
            created_at            timestamptz NOT NULL DEFAULT now(),
            updated_at            timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_depo_schedule_matter "
               "ON depo_schedule (tenant_id, matter_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_depo_schedule_start "
               "ON depo_schedule (tenant_id, scheduled_start)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_depo_schedule_transcript "
               "ON depo_schedule (linked_transcript_id) WHERE linked_transcript_id IS NOT NULL")


def downgrade():
    op.execute("DROP TABLE IF EXISTS depo_schedule")
