"""depo sync metadata: forced-alignment provenance for the U9 sync lane.

Records how a transcript's line-level timecodes were produced (aeneas / whisper
/ manual), the coverage achieved, and when. Per-line provenance lets the
correction UX distinguish auto-aligned lines from manually-set anchors and
interpolated gaps.

Revision: 0082_depo_sync_meta
"""
from alembic import op

revision = "0082_depo_sync_meta"
down_revision = "0081_appellate_record"
branch_labels = None
depends_on = None


def upgrade():
    # transcript-level: how/when timecodes were produced
    op.execute("ALTER TABLE deposition_transcripts ADD COLUMN IF NOT EXISTS sync_engine text")
    op.execute("ALTER TABLE deposition_transcripts ADD COLUMN IF NOT EXISTS sync_coverage real")
    op.execute("ALTER TABLE deposition_transcripts ADD COLUMN IF NOT EXISTS synced_at timestamptz")
    # per-line provenance: aeneas | whisper | manual | interp (NULL = not timecoded)
    op.execute("ALTER TABLE transcript_lines ADD COLUMN IF NOT EXISTS timecode_source varchar(16)")


def downgrade():
    op.execute("ALTER TABLE transcript_lines DROP COLUMN IF EXISTS timecode_source")
    op.execute("ALTER TABLE deposition_transcripts DROP COLUMN IF EXISTS synced_at")
    op.execute("ALTER TABLE deposition_transcripts DROP COLUMN IF EXISTS sync_coverage")
    op.execute("ALTER TABLE deposition_transcripts DROP COLUMN IF EXISTS sync_engine")
