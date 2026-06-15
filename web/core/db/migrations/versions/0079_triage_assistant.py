"""0079: store the assistant side of each guided-ingest chat turn.

triage_turns captured the human instruction + resulting actions but discarded
the model's reply and reasoning — so the conversation only ever lived in the
browser. Keep both: assistant_reply (the concise answer shown to the attorney)
and assistant_thinking (the model's reasoning), making the chat fully
reconstructable from the DB and the reasoning itself a learnable signal.

Chained after 0078_depo_video_path to keep a single linear head (both it and
the original 0077 branch off the same parent).

Idempotent; safe to re-run.
"""
from alembic import op

revision = "0079_triage_assistant"
down_revision = "0078_depo_video_path"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE triage_turns "
               "ADD COLUMN IF NOT EXISTS assistant_reply text")
    op.execute("ALTER TABLE triage_turns "
               "ADD COLUMN IF NOT EXISTS assistant_thinking text")


def downgrade():
    op.execute("ALTER TABLE triage_turns DROP COLUMN IF EXISTS assistant_thinking")
    op.execute("ALTER TABLE triage_turns DROP COLUMN IF EXISTS assistant_reply")
