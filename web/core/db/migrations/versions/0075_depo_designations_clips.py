"""0075: depo_designations + depo_clips -- deterministic page:line designations
and clips-as-objects (scope §3, §5-§7).

A designation is a deterministic page:line span over a transcript (Layer-1) with
an issue code / color / note and a designating party + type (affirmative / counter
/ objection). char_start/char_end are derived from transcript_lines at create/edit
time so the excerpt and any downstream report compile from offsets, not copy-paste.

A clip is the projection of a designation through transcript_lines.timecode_ms into
a video in/out (video transcripts only). It carries object_uuid so it can become a
reusable, provenance-bearing object across matter surfaces (U7). render_status:
  awaiting_sync  -> has_video but no timecodes yet (the sync lane, U9, fills them)
  pending        -> in/out resolved, not yet rendered
  rendered       -> rendered_path populated
Editing the designation's page:line re-derives the clip in/out (and, in U7,
auto-splits when interior lines are removed).
"""
from alembic import op

revision = "0075_depo_designations_clips"
down_revision = "0074_deposition_exhibits"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS depo_designations (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         char(36) NOT NULL,
            matter_id         uuid,
            session_id        bigint,
            transcript_id     uuid NOT NULL
                REFERENCES deposition_transcripts(id) ON DELETE CASCADE,
            designating_party varchar,
            designation_type  varchar NOT NULL DEFAULT 'affirmative',
            start_page        integer NOT NULL,
            start_line        integer NOT NULL,
            end_page          integer NOT NULL,
            end_line          integer NOT NULL,
            char_start        integer,
            char_end          integer,
            excerpt_text      text,
            issue_code        varchar,
            color             varchar,
            note              text,
            created_by        bigint,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_depo_desig_transcript "
               "ON depo_designations (tenant_id, transcript_id, start_page, start_line)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_depo_desig_matter "
               "ON depo_designations (tenant_id, matter_id)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS depo_clips (
            id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id      char(36) NOT NULL,
            designation_id uuid NOT NULL
                REFERENCES depo_designations(id) ON DELETE CASCADE,
            session_id     bigint,
            transcript_id  uuid,
            video_asset_id varchar,
            in_ms          bigint,
            out_ms         bigint,
            rendered_path  text,
            render_status  varchar NOT NULL DEFAULT 'awaiting_sync',
            object_uuid    uuid NOT NULL DEFAULT gen_random_uuid(),
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_depo_clips_designation "
               "ON depo_clips (designation_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_depo_clips_object "
               "ON depo_clips (object_uuid)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS depo_clips")
    op.execute("DROP TABLE IF EXISTS depo_designations")
