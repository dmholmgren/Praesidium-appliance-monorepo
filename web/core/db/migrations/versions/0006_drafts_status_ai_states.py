"""Extend timesheet_drafts.status to allow ai_review + personal

Revision ID: 0006_drafts_status_ai_states
Revises: 0005_timesheet_ai_matches
Create Date: 2026-05-05

Adds two new valid values to the timesheet_drafts.status CHECK constraint:
  - 'ai_review' : Pass-2 matcher has assigned a candidate matter or
                  reviewed-and-flagged a draft; awaiting attorney approval.
  - 'personal'  : Pass-1 classifier has marked this draft as non-billable
                  personal activity. Surfaces in the Personal/Excluded tab.

Existing values preserved: pending, approved, rejected, pushed, edited.

The constraint must be DROPped and re-CREATEd (Postgres does not have
ALTER CONSTRAINT ... ADD VALUE for CHECK constraints).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0006_drafts_status_ai_states"
down_revision = "0005_timesheet_ai_matches"
branch_labels = None
depends_on = None


_OLD_VALUES = ("pending", "approved", "rejected", "pushed", "edited")
_NEW_VALUES = ("pending", "approved", "rejected", "pushed", "edited",
               "ai_review", "personal")
_CONSTRAINT_NAME = "ck_timesheet_drafts_status"


def _values_sql(values):
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    # Drop existing constraint
    op.execute(
        f"ALTER TABLE timesheet_drafts DROP CONSTRAINT {_CONSTRAINT_NAME}"
    )
    # Recreate with extended value set
    op.execute(
        f"ALTER TABLE timesheet_drafts "
        f"ADD CONSTRAINT {_CONSTRAINT_NAME} "
        f"CHECK (status IN ({_values_sql(_NEW_VALUES)}))"
    )


def downgrade() -> None:
    # Down-migrate any rows using the new values back to 'pending' so the
    # restored constraint doesn't reject existing data. We deliberately do
    # NOT delete those rows — they remain available for re-classification
    # if the migration is re-applied.
    op.execute(
        "UPDATE timesheet_drafts SET status = 'pending' "
        "WHERE status IN ('ai_review', 'personal')"
    )
    op.execute(
        f"ALTER TABLE timesheet_drafts DROP CONSTRAINT {_CONSTRAINT_NAME}"
    )
    op.execute(
        f"ALTER TABLE timesheet_drafts "
        f"ADD CONSTRAINT {_CONSTRAINT_NAME} "
        f"CHECK (status IN ({_values_sql(_OLD_VALUES)}))"
    )
