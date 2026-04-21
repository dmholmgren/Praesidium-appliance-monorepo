"""0024_drafting_engine

Module 4 — Document Generation & Assembly
M5iii — Production-Aware Drafting (Layer 9)

Revision ID: 0024_drafting_engine
Revises: 0023_sessions
Create Date: 2026-04-07

DEPLOYMENT NOTE:
    This migration file is a STAMP ONLY. All DDL is executed directly on DB-01
    via psql as the postgres superuser to avoid asyncpg transaction failures.
    See deploy/0024_deploy.sql for the full DDL script.

    Deployment sequence:
        1. Copy this file to /app/core/db/migrations/versions/
        2. Run DDL on DB-01: sudo -u postgres psql praesidium_hjmm -f /tmp/0024_deploy.sql
        3. Stamp: docker exec praesidium-web alembic stamp 0024_drafting_engine
        4. Verify: docker exec praesidium-web alembic current
"""

from alembic import op

# revision identifiers
revision = '0024_drafting_engine'
down_revision = '0023_sessions'
branch_labels = None
depends_on = None


def upgrade():
    # DDL executed directly on DB-01 via psql — see deploy/0024_deploy.sql
    # This migration is a bookkeeping stamp only.
    pass


def downgrade():
    # Downgrade not supported for production deployments.
    # To remove: drop tables manually on DB-01 and alembic stamp 0023_sessions.
    pass
