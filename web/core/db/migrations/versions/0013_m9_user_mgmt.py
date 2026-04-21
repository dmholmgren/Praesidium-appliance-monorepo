"""Module 9 Component 2 — User Management schema additions

Adds theme_preference and last_active_at to the users table.
Uses ADD COLUMN IF NOT EXISTS — safe to run even if columns were added
manually during prior debugging sessions.

Revision ID: 0013_m9_user_mgmt
Revises: 0012_s20_pass2_tables
Create Date: 2026-03-31

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from alembic import op
import sqlalchemy as sa

revision = "0013_m9_user_mgmt"
down_revision = "0012_s20_pass2_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # theme_preference — 'dark' | 'light' — default dark (platform default)
    conn.execute(sa.text("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS theme_preference VARCHAR(10)
        NOT NULL DEFAULT 'dark'
    """))

    # last_active_at — updated on each authenticated request
    conn.execute(sa.text("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMPTZ NULL
    """))

    # user_preferences JSONB — extensible bag for future per-user settings
    conn.execute(sa.text("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS user_preferences JSONB
        NOT NULL DEFAULT '{}'
    """))

    # CHECK constraint on theme_preference
    conn.execute(sa.text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_users_theme_preference'
            ) THEN
                ALTER TABLE users
                ADD CONSTRAINT ck_users_theme_preference
                CHECK (theme_preference IN ('dark', 'light'));
            END IF;
        END$$
    """))

    # Index for fast tenant+active user queries
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_users_tenant_is_active
        ON users (tenant_id, is_active)
    """))

    # Index for last_active_at sort
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_users_last_active_at
        ON users (last_active_at DESC NULLS LAST)
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(
        "ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_theme_preference"
    ))
    conn.execute(sa.text(
        "DROP INDEX IF EXISTS ix_users_tenant_is_active"
    ))
    conn.execute(sa.text(
        "DROP INDEX IF EXISTS ix_users_last_active_at"
    ))
    conn.execute(sa.text(
        "ALTER TABLE users DROP COLUMN IF EXISTS theme_preference"
    ))
    conn.execute(sa.text(
        "ALTER TABLE users DROP COLUMN IF EXISTS last_active_at"
    ))
    conn.execute(sa.text(
        "ALTER TABLE users DROP COLUMN IF EXISTS user_preferences"
    ))
