"""0046 deadline_classification: classification provenance columns + review-queue indexes.

Tier-0 of the deadline classifier (exact lexicon match) needs somewhere to record what
typed a deadline and how confidently, on BOTH the live table and staging:

  deadlines           -- the 16 court-imposed Marcus rows (clean titles, exact-match 16/16).
                         Gets a CANONICAL deadline_type_id FK (non-destructive: the coarse
                         free-text deadline_type string is preserved as provenance).
  document_deadlines  -- staging. Already has deadline_type_id; gains classification_* +
                         classification_run_id so tiers 0-3 can stamp method/confidence/run.

Mirrors the onboarding pattern (status/confidence/who-when) and the house rule that no
migration destroys provenance -- we ADD a canonical FK rather than overwrite deadline_type.

asyncpg: one statement per op.execute(). All ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT
EXISTS so re-apply is safe.
"""
from alembic import op

revision = "0046_deadline_classification"
down_revision = "0045_deadline_type_lexicon"
branch_labels = None
depends_on = None


UPGRADE_STATEMENTS = [
    # --- live deadlines: canonical FK + classification provenance ---
    "ALTER TABLE deadlines ADD COLUMN IF NOT EXISTS deadline_type_id uuid "
    "REFERENCES deadline_type_library(id)",
    "ALTER TABLE deadlines ADD COLUMN IF NOT EXISTS classification_method varchar(20)",
    "ALTER TABLE deadlines ADD COLUMN IF NOT EXISTS classification_confidence numeric(4,3)",
    "ALTER TABLE deadlines ADD COLUMN IF NOT EXISTS classified_at timestamptz",

    # --- staging document_deadlines: classification provenance (deadline_type_id exists) ---
    "ALTER TABLE document_deadlines ADD COLUMN IF NOT EXISTS classification_method varchar(20)",
    "ALTER TABLE document_deadlines ADD COLUMN IF NOT EXISTS classification_confidence numeric(4,3)",
    "ALTER TABLE document_deadlines ADD COLUMN IF NOT EXISTS classified_at timestamptz",
    "ALTER TABLE document_deadlines ADD COLUMN IF NOT EXISTS classification_run_id uuid",

    # --- review queues: rows the higher tiers / a human still need to type ---
    "CREATE INDEX IF NOT EXISTS ix_deadlines_unclassified "
    "ON deadlines (matter_id) WHERE deadline_type_id IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_docdeadlines_unclassified "
    "ON document_deadlines (dms_document_id) WHERE deadline_type_id IS NULL",
]


DOWNGRADE_STATEMENTS = [
    "DROP INDEX IF EXISTS ix_docdeadlines_unclassified",
    "DROP INDEX IF EXISTS ix_deadlines_unclassified",
    "ALTER TABLE document_deadlines DROP COLUMN IF EXISTS classification_run_id",
    "ALTER TABLE document_deadlines DROP COLUMN IF EXISTS classified_at",
    "ALTER TABLE document_deadlines DROP COLUMN IF EXISTS classification_confidence",
    "ALTER TABLE document_deadlines DROP COLUMN IF EXISTS classification_method",
    "ALTER TABLE deadlines DROP COLUMN IF EXISTS classified_at",
    "ALTER TABLE deadlines DROP COLUMN IF EXISTS classification_confidence",
    "ALTER TABLE deadlines DROP COLUMN IF EXISTS classification_method",
    "ALTER TABLE deadlines DROP COLUMN IF EXISTS deadline_type_id",
]


def upgrade():
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade():
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
