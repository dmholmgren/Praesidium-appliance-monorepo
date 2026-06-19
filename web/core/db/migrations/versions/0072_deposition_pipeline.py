"""0072: deposition evidence pipeline -- transcript primitives + stage ledger.

Depositions become first-class processed evidence (scope v2). This lays the
Layer-0/1 substrate the depo DAG (modules/depositions/jobs/depo_dag.py) drains
over -- the same ledger-as-queue shape as the eDiscovery pipeline:

  deposition_transcripts   §0 canonical-text record (immutable once written), one
                           per transcript file, linked to a viaticum_sessions
                           parent (the existing depo session record verified live
                           at build time; session_type defaults to 'deposition').
  transcript_lines         page:line -> char-offset addressing layer. timecode_ms
                           is filled later by the sync lane; the text is never
                           rewritten (§0).
  transcript_qa_units      Layer-1 semantic primitive: one Q&A exchange.
  deposition_stage_status  ledger-as-queue with the 0064 treatment (priority,
                           pending/running partial indexes, fillfactor 85 +
                           aggressive autovacuum). Grain is the transcript for
                           ingest/segment/sync and the qa_unit for embed (fan-out)
                           -> two partial unique indexes, one per grain, for
                           idempotent re-seed.

Binds to the schema verified live (head 0071): the depo parent is
viaticum_sessions (bigint id), so session_id is bigint -- there is no
deposition_sessions table. The transcript_qa_embeddings table is added in the U2
migration alongside the embed stage.

Idempotent: all CREATE ... IF NOT EXISTS / reloptions are safe to re-apply.
"""
from alembic import op

revision = "0072_deposition_pipeline"
down_revision = "0071_guided_ingestion"
branch_labels = None
depends_on = None


def upgrade():
    # ---- deposition_transcripts (§0 canonical record) ----------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS deposition_transcripts (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        char(36) NOT NULL,
            session_id       bigint,
            matter_id        uuid,
            deponent         varchar,
            source_format    varchar NOT NULL,
            canonical_text   text,
            page_first       integer,
            page_last        integer,
            line_count       integer,
            qa_count         integer,
            has_timecodes    boolean NOT NULL DEFAULT false,
            has_video        boolean NOT NULL DEFAULT false,
            is_scanned       boolean NOT NULL DEFAULT false,
            needs_conversion boolean NOT NULL DEFAULT false,
            source_file_path text,
            sha256           varchar(64),
            status           varchar NOT NULL DEFAULT 'pending',
            imported_at      timestamptz NOT NULL DEFAULT now(),
            imported_by      bigint,
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_depo_transcripts_session "
               "ON deposition_transcripts (tenant_id, session_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_depo_transcripts_matter "
               "ON deposition_transcripts (tenant_id, matter_id)")
    # one transcript per (tenant, file content): idempotent re-registration
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_depo_transcripts_sha "
               "ON deposition_transcripts (tenant_id, sha256) "
               "WHERE sha256 IS NOT NULL")

    # ---- transcript_lines (addressing layer) -------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS transcript_lines (
            transcript_id uuid NOT NULL
                REFERENCES deposition_transcripts(id) ON DELETE CASCADE,
            tenant_id     char(36) NOT NULL,
            page          integer NOT NULL,
            line          integer NOT NULL,
            char_start    integer NOT NULL,
            char_end      integer NOT NULL,
            speaker       varchar,
            qa_role       varchar,
            text          text,
            timecode_ms   bigint,
            PRIMARY KEY (transcript_id, page, line)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_transcript_lines_char "
               "ON transcript_lines (transcript_id, char_start)")

    # ---- transcript_qa_units (Layer-1 primitive) ---------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS transcript_qa_units (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            transcript_id uuid NOT NULL
                REFERENCES deposition_transcripts(id) ON DELETE CASCADE,
            tenant_id     char(36) NOT NULL,
            session_id    bigint,
            seq           integer NOT NULL,
            examiner      varchar,
            witness       varchar,
            q_start_page  integer,
            q_start_line  integer,
            a_end_page    integer,
            a_end_line    integer,
            char_start    integer,
            char_end      integer,
            question_text text,
            answer_text   text,
            is_colloquy   boolean NOT NULL DEFAULT false,
            topic_code    varchar,
            created_at    timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_transcript_qa_seq "
               "ON transcript_qa_units (transcript_id, seq)")

    # ---- deposition_stage_status (ledger-as-queue, 0064 treatment) ---------
    op.execute("""
        CREATE TABLE IF NOT EXISTS deposition_stage_status (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     char(36) NOT NULL,
            session_id    bigint,
            transcript_id uuid,
            qa_unit_id    uuid,
            matter_id     uuid,
            stage         varchar NOT NULL,
            state         varchar NOT NULL DEFAULT 'pending',
            attempt       integer NOT NULL DEFAULT 0,
            priority      integer NOT NULL DEFAULT 100,
            worker_id     varchar,
            input_hash    varchar,
            started_at    timestamptz,
            finished_at   timestamptz,
            duration_ms   integer,
            error_class   varchar,
            error_message text,
            error_detail  jsonb,
            updated_at    timestamptz NOT NULL DEFAULT now()
        )
    """)
    # idempotent re-seed, per grain (transcript for ingest/segment/sync;
    # qa_unit for embed). Two partial unique indexes, one per grain.
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_depo_stage_transcript "
               "ON deposition_stage_status (tenant_id, transcript_id, stage) "
               "WHERE qa_unit_id IS NULL")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_depo_stage_qaunit "
               "ON deposition_stage_status (tenant_id, qa_unit_id, stage) "
               "WHERE qa_unit_id IS NOT NULL")
    # claim scan (SKIP LOCKED) + stale-claim reaper (0064)
    op.execute("CREATE INDEX IF NOT EXISTS ix_depo_stage_claim "
               "ON deposition_stage_status (stage, priority, updated_at) "
               "WHERE state = 'pending'")
    op.execute("CREATE INDEX IF NOT EXISTS ix_depo_stage_reap "
               "ON deposition_stage_status (started_at) "
               "WHERE state = 'running'")
    # high-churn queue rows: HOT updates + early vacuum keep bloat down
    op.execute(
        "ALTER TABLE deposition_stage_status SET ("
        "autovacuum_vacuum_scale_factor = 0.01, "
        "autovacuum_vacuum_cost_delay = 0, "
        "autovacuum_analyze_scale_factor = 0.02, "
        "fillfactor = 85)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS deposition_stage_status")
    op.execute("DROP TABLE IF EXISTS transcript_qa_units")
    op.execute("DROP TABLE IF EXISTS transcript_lines")
    op.execute("DROP TABLE IF EXISTS deposition_transcripts")
