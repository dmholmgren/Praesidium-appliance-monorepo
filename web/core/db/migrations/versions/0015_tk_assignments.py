"""Timekeeper matter assignments table.

Revision ID: 0015_tk_assignments
Revises: 0014_doc_annotations
"""
from alembic import op
import sqlalchemy as sa

revision = '0015_tk_assignments'
down_revision = '0014_doc_annotations'

def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS timekeeper_matter_assignments (
            id              BIGSERIAL PRIMARY KEY,
            tenant_id       TEXT NOT NULL,
            timekeeper_id   TEXT NOT NULL,
            matter_id       UUID NOT NULL REFERENCES matters(id),
            role            TEXT DEFAULT 'attorney',
            hourly_rate_override NUMERIC(10,2),
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(tenant_id, timekeeper_id, matter_id)
        );

        CREATE INDEX IF NOT EXISTS ix_tma_tenant_tk
            ON timekeeper_matter_assignments(tenant_id, timekeeper_id);
        CREATE INDEX IF NOT EXISTS ix_tma_tenant_matter
            ON timekeeper_matter_assignments(tenant_id, matter_id);
    """)

def downgrade():
    op.execute("DROP TABLE IF EXISTS timekeeper_matter_assignments CASCADE;")
