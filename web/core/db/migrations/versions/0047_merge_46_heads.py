"""0047 merge: heal the two-head branch created at 0046.

Two parallel workstreams each branched off 0045_deadline_type_lexicon:
  - 0046_onboarding_matching     (APPLIED; current stamped head)
  - 0046_deadline_classification (UNAPPLIED here; idempotent ADD COLUMN IF NOT EXISTS)

This is a PURE merge revision -- no DDL. Schema evolution lives in its own
revisions per house rule; a merge only reconverges the chain. Running
`alembic upgrade head` will apply the unapplied deadline_classification
branch first (safe -- all IF NOT EXISTS), then stamp this merge, leaving a
single linear head for 0048+ to build on.

Root-cause note for the chain going forward: colliding 0046s came from
independent chats each grabbing "the next integer." Standing rule -- check
`alembic_current` + the versions dir on disk before numbering a migration.
"""
from alembic import op  # noqa: F401

# revision identifiers, used by Alembic.
revision = "0047_merge_46_heads"
down_revision = ("0046_onboarding_matching", "0046_deadline_classification")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
