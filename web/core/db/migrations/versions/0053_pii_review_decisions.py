"""0053 pii_review_decisions — PII review correction layer (type/value-level).

The PII rollup consolidates document_entities on (pii_type, normalized_value),
so review decisions key at the VALUE level (one row per distinct value) or the
TYPE level (normalized_value NULL = "exclude the whole type from redaction").
This is the apply-target the React PII tab writes to; the (future) redaction-set
emitter reads `redact` to decide what to mask.

Decisions, not raw-row mutations: re-running extraction re-creates document_entities
rows, but a decision on a value/type persists across runs (rejoined on
pii_type + normalized_value). Same primitive-not-chunk logic as the rest of the spine.

Idempotent (IF NOT EXISTS). One statement per op.execute() — asyncpg env.
"""
from alembic import op

revision = "0053_pii_review_decisions"
down_revision = "0052_geometry_substrate"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS pii_review_decisions (
          id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id        char(36) NOT NULL,
          pii_type         varchar NOT NULL,
          normalized_value varchar,
          decision         varchar NOT NULL,
          redact           boolean NOT NULL DEFAULT true,
          reviewed_by      bigint,
          reviewed_at      timestamptz NOT NULL DEFAULT now(),
          review_notes     text,
          created_at       timestamptz NOT NULL DEFAULT now(),
          updated_at       timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_pii_decision_value
          ON pii_review_decisions (tenant_id, pii_type, normalized_value)
          WHERE normalized_value IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_pii_decision_type
          ON pii_review_decisions (tenant_id, pii_type)
          WHERE normalized_value IS NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_pii_decision_lookup
          ON pii_review_decisions (tenant_id, pii_type, normalized_value)
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS pii_review_decisions")
