"""0132: formalize deposition_transcripts.video_status.

The video transcode stage (services/bundle_ingest.transcode_video) tracks
per-transcript progress in deposition_transcripts.video_status
(queued -> transcoding -> ready/failed). The column was first added to the live
DB via an ad-hoc ALTER; this migration makes it part of the schema so a
from-scratch rebuild has it. Idempotent (IF NOT EXISTS / IF EXISTS) so it is a
safe no-op on the already-patched live DB.
"""
from alembic import op

revision = "0132_depo_video_status"
down_revision = "0131_trial_analytics_nav_trim"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE deposition_transcripts "
        "ADD COLUMN IF NOT EXISTS video_status varchar"
    )


def downgrade():
    op.execute(
        "ALTER TABLE deposition_transcripts "
        "DROP COLUMN IF EXISTS video_status"
    )
