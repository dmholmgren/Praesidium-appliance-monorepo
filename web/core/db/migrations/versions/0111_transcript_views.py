"""0111: per-user transcript view tracking for the "My Recent Transcripts" panel.

One row per (tenant, user, transcript); each open bumps viewed_at and view_count.
The Transcripts landing's "recent" feed reads this filtered to the current user,
ordered by viewed_at DESC. Recorded server-side by the /depositions/transcript/{id}
route, so it captures every page open without a client round-trip.
"""
from alembic import op


revision = "0111_transcript_views"
down_revision = "0110_transcripts_nav_rename"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS transcript_views (
            tenant_id       varchar     NOT NULL,
            user_id         bigint      NOT NULL,
            transcript_id   uuid        NOT NULL,
            view_count      integer     NOT NULL DEFAULT 1,
            first_viewed_at timestamptz NOT NULL DEFAULT now(),
            viewed_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, user_id, transcript_id)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_transcript_views_user_recent
        ON transcript_views (tenant_id, user_id, viewed_at DESC)
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS transcript_views")
