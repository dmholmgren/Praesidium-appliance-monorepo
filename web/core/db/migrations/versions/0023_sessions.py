"""0023_sessions — create sessions table

Revision ID: 0023_sessions
Revises: 0022_billing_module
Create Date: 2026-04-06

Creates the sessions table required by ActivityMiddleware to resolve
tenant_id from the praesidium_session cookie. Absence of this table
causes 6 router test failures (test_m5i_issue_map x4, test_m5i_drift_detection x2).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic
revision = "0023_sessions"
down_revision = "0022_billing_module"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Create sessions table
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            token       VARCHAR(64) NOT NULL,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tenant_id   VARCHAR(36) NOT NULL,
            expires_at  TIMESTAMPTZ NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    # Unique index on token (enforces uniqueness + fast lookup)
    conn.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uix_sessions_token
        ON sessions (token)
    """))

    # Composite index for tenant + user lookups (ActivityMiddleware pattern)
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_sessions_tenant_user
        ON sessions (tenant_id, user_id)
    """))

    # Grant DML to application role
    conn.execute(sa.text("""
        GRANT SELECT, INSERT, UPDATE, DELETE ON sessions TO praesidium_db
    """))


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("DROP INDEX IF EXISTS ix_sessions_tenant_user"))
    conn.execute(sa.text("DROP INDEX IF EXISTS uix_sessions_token"))
    conn.execute(sa.text("DROP TABLE IF EXISTS sessions"))
