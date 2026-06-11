"""widget_registry and layout_registry tables

Revision ID: 0026_widget_registry
Revises: 0025_merge_heads
Create Date: 2026-04-10

DDL NOTE: Tables were created directly on DB-01 via psql (0026_widget_registry.sql)
because CREATE TABLE + CREATE INDEX in the same asyncpg transaction fails.
This file is a stamp-only stub — no upgrade/downgrade DDL.

After deploying 0026_widget_registry.sql on DB-01, stamp with:
    docker exec praesidium-web alembic stamp 0026_widget_registry
Then verify:
    docker exec praesidium-web alembic current
    # Expected: 0026_widget_registry (head)
"""

from alembic import op

# revision identifiers
revision = '0026_widget_registry'
down_revision = '0025_merge_heads'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # DDL executed directly on DB-01 via 0026_widget_registry.sql
    # Tables: widget_registry, layout_registry
    # Grants: SELECT, INSERT, UPDATE, DELETE ON both tables TO praesidium_db
    pass


def downgrade() -> None:
    # To roll back manually on DB-01:
    #   DROP TABLE IF EXISTS layout_registry;
    #   DROP TABLE IF EXISTS widget_registry;
    pass
