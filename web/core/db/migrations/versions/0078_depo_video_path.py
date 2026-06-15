"""0078 add source-video columns to deposition_transcripts (U7 clip render)

The clip renderer needs to know where a transcript's synced video lives.
deposition_transcripts already carries has_video/has_timecodes flags and
source_file_path (the transcript file) but no video location; depo_clips
points at a video_asset_id whose asset table was never built. Rather than a
full asset table (premature -- one video per transcript today), record the
path directly. Additive + nullable, safe to apply live.
"""
from alembic import op

revision = "0078_depo_video_path"
down_revision = "0077_triage_turns"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE deposition_transcripts ADD COLUMN IF NOT EXISTS video_path text")
    op.execute("ALTER TABLE deposition_transcripts ADD COLUMN IF NOT EXISTS video_duration_ms bigint")


def downgrade():
    op.execute("ALTER TABLE deposition_transcripts DROP COLUMN IF EXISTS video_duration_ms")
    op.execute("ALTER TABLE deposition_transcripts DROP COLUMN IF EXISTS video_path")
