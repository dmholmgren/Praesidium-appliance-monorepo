"""Deduplicate ts_slips, fix unique constraint, add promotion infrastructure.

1. Deletes 71,000 duplicate rows from ts_slips (keeps earliest imported_at per source_slip_id)
2. Drops the broken content_hash-based unique index
3. Adds a proper UNIQUE constraint on (tenant_id, source_id, source_slip_id)
4. Adds legacy_source_id partial unique index on time_entries for upsert performance
5. Adds legacy_source_id partial unique index on invoices for upsert performance
6. Adds ts_promotion_hwm table to track nightly promotion watermark

Revision ID: 0017_ts_slips_dedupe_promote
Revises: 0016_chat_history_drafting
"""

from alembic import op
import sqlalchemy as sa

revision = '0017_ts_slips_dedupe_promote'
down_revision = '0016_chat_history_drafting'
branch_labels = None
depends_on = None


def upgrade():
    # ── Step 1: Delete duplicate ts_slips rows ─────────────────────────────
    # Every dupe is exactly 2x — keep the row with the earliest imported_at
    # (the original April 20 batch), delete the May 11 re-import.
    op.execute("""
        DELETE FROM ts_slips
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY tenant_id, source_slip_id
                           ORDER BY imported_at ASC
                       ) AS rn
                FROM ts_slips
            ) ranked
            WHERE rn > 1
        )
    """)

    # ── Step 2: Drop the broken content_hash unique constraint ────────────
    # This was created as a UNIQUE CONSTRAINT (not just an index), so
    # drop_index fails with DependentObjectsStillExistError.
    op.drop_constraint('uq_ts_slips_dedup', 'ts_slips', type_='unique')

    # ── Step 3: Add proper natural-key unique constraint ───────────────────
    # source_slip_id = Firebird RECORDID — the true identity of each slip
    op.create_unique_constraint(
        'uq_ts_slips_natural_key',
        'ts_slips',
        ['tenant_id', 'source_id', 'source_slip_id']
    )

    # ── Step 4: Indexes for time_entries promotion upsert ──────────────────
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_time_entries_legacy
        ON time_entries (tenant_id, source_system, legacy_source_id)
        WHERE legacy_source_id IS NOT NULL
    """)

    # ── Step 5: Indexes for invoices promotion upsert ──────────────────────
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_legacy
        ON invoices (tenant_id, source_system, legacy_source_id)
        WHERE legacy_source_id IS NOT NULL
    """)

    # ── Step 6: Promotion watermark table ──────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS ts_promotion_hwm (
            tenant_id           VARCHAR(36) NOT NULL,
            entity              VARCHAR(50) NOT NULL,
            last_promoted_at    TIMESTAMPTZ,
            last_source_slip_id TEXT,
            rows_promoted       BIGINT DEFAULT 0,
            updated_at          TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (tenant_id, entity)
        )
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS ts_promotion_hwm")
    op.execute("DROP INDEX IF EXISTS uq_invoices_legacy")
    op.execute("DROP INDEX IF EXISTS uq_time_entries_legacy")
    op.drop_constraint('uq_ts_slips_natural_key', 'ts_slips', type_='unique')
    # Recreate original (broken) index — content_hash column still exists
    op.create_unique_constraint(
        'uq_ts_slips_dedup',
        'ts_slips',
        ['tenant_id', 'source_id', 'content_hash']
    )
