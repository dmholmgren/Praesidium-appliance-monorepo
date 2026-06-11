"""Portal magic links, folder scope, module access.

Revision ID: 0033_portal_magic_lnk
Revises: 0032_research_inbox
Create Date: 2026-05-19

NOTE: This migration was originally applied via docker cp and the .py
file was not persisted to the host bind mount. This stub restores the
Alembic revision chain. All three tables already exist in the database;
the CREATE TABLE statements use IF NOT EXISTS for idempotency.
"""

from alembic import op
import sqlalchemy as sa

revision = '0033_portal_magic_lnk'
down_revision = '0032_research_inbox'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS portal_magic_links (
            id BIGSERIAL PRIMARY KEY,
            tenant_id CHAR(36) NOT NULL,
            user_id BIGINT NOT NULL,
            token VARCHAR NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ,
            used_ip VARCHAR,
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS portal_module_access (
            id BIGSERIAL PRIMARY KEY,
            tenant_id CHAR(36) NOT NULL,
            user_id BIGINT NOT NULL,
            module_key VARCHAR NOT NULL,
            is_enabled BOOLEAN NOT NULL DEFAULT true,
            granted_by BIGINT,
            granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS portal_folder_scope (
            id BIGSERIAL PRIMARY KEY,
            scope_id BIGINT NOT NULL,
            folder_key VARCHAR NOT NULL,
            is_visible BOOLEAN NOT NULL DEFAULT true,
            granted_by BIGINT,
            granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))


def downgrade():
    op.drop_table('portal_folder_scope')
    op.drop_table('portal_module_access')
    op.drop_table('portal_magic_links')
