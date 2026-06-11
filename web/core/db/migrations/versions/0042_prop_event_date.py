"""0042 proposition event_date: queryable operative-fact chronology.

Pass-1 emits events (operative facts) into document_propositions. The chronology
("timeline becomes SQL") needs the event date as a real column, not buried in
proposition_text. Two columns, mirroring the partial-date pattern already used by
document_metadata (date_on_document) and document_deadlines (deadline_date):

  event_date     date  -- parsed where the written date is complete; NULL otherwise
  event_date_raw text  -- the date exactly as written (set for every event row)

Both nullable / no default => metadata-only ADD COLUMN, no table rewrite. Only
proposition_type='event' rows carry these; affirmative_defense / relief /
order_directive rows leave them NULL. Btree index on (tenant_id, event_date) is
built now while the table is effectively empty (propositions get populated by the
Pass-1 write mapper that follows this migration), so the index costs nothing.

asyncpg: one statement per op.execute(). All DDL guarded (IF [NOT] EXISTS) so
re-running is a no-op. Applied as praesidium (the app role owns the schema).
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0042_prop_event_date"
down_revision = "0041_case_intel"
branch_labels = None
depends_on = None


UPGRADE_STATEMENTS = [
    "ALTER TABLE IF EXISTS document_propositions ADD COLUMN IF NOT EXISTS event_date     date",
    "ALTER TABLE IF EXISTS document_propositions ADD COLUMN IF NOT EXISTS event_date_raw text",
    "CREATE INDEX IF NOT EXISTS ix_doc_prop_event_date ON document_propositions (tenant_id, event_date)",
]


DOWNGRADE_STATEMENTS = [
    "DROP INDEX IF EXISTS ix_doc_prop_event_date",
    "ALTER TABLE IF EXISTS document_propositions DROP COLUMN IF EXISTS event_date_raw",
    "ALTER TABLE IF EXISTS document_propositions DROP COLUMN IF EXISTS event_date",
]


def upgrade():
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade():
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
