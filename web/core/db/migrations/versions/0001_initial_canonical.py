"""Praesidium Series 2.0 — Canonical Initial Schema

Revision ID: 0001_initial_canonical
Revises: (none — this is the new baseline)
Create Date: 2026-04-28

This migration replaces all prior migrations 0001 through 0063 inclusive.
The companion 0001_initial_canonical.sql file contains the full schema DDL
generated from HJMM production (praesidium_hjmm @ 10.10.60.11) on
2026-04-28, which was at alembic head 0063_permissions_center at the time
of generation.

WHY A STAMP-ONLY STUB
=====================
Alembic + asyncpg cannot run mixed CREATE TABLE + CREATE INDEX in a single
transaction (asyncpg DDL transaction limitation, established as a
documented constraint in this codebase). The canonical migration creates
281 tables and 574 indexes; running that through op.execute() inside the
alembic transaction would fail.

Established codebase pattern (e.g., 0026_widget_registry): for migrations
that exceed asyncpg's DDL transaction limits, run the SQL directly on the
database host via psql, then `alembic stamp` to mark it applied.

DEPLOYMENT PROCEDURE
====================
On a FRESH database (no schema, no alembic_version):

  Step 1. As postgres superuser, ensure extensions exist:
    sudo -u postgres psql -d praesidium <<'EOF'
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
    CREATE EXTENSION IF NOT EXISTS btree_gin;
    CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
    CREATE EXTENSION IF NOT EXISTS unaccent;
    EOF

  Step 2. As praesidium_db owner (or via sudo -u postgres), apply the
  canonical schema:
    psql -d praesidium -U praesidium_db -f \\
      core/db/migrations/versions/0001_initial_canonical.sql

  Step 3. Stamp alembic to mark this migration applied:
    docker exec praesidium-web alembic stamp 0001_initial_canonical
    docker exec praesidium-web alembic current
    # Expected: 0001_initial_canonical (head)

ON HJMM PRODUCTION
==================
HJMM is already at the canonical state (it is the source). To reconcile
production's alembic_version with the canonical migration tree:

  Step 1. Verify production schema matches the .sql file (no DDL drift
  since 2026-04-28 generation date).
  Step 2. Update alembic_version row directly:
    UPDATE alembic_version SET version_num = '0001_initial_canonical';

  This is non-destructive: the schema is unchanged. Only alembic's
  bookkeeping is updated.

DOWNGRADE
=========
This is a baseline migration. The downgrade procedure is a full schema
drop — not a reverse migration. Use when rebuilding the database from
scratch:

    sudo -u postgres psql -d praesidium <<'EOF'
    DROP SCHEMA public CASCADE;
    CREATE SCHEMA public;
    -- Then re-create extensions (Step 1 above) and re-apply Step 2.
    EOF

ARCHIVED MIGRATIONS
===================
The 64 prior migrations (0001 through 0063) and the eDiscovery branch
migrations (ediscovery_007 through ediscovery_012, plus ediscovery_s20_base)
have been moved to:
    core/db/migrations/versions/_archived_2026-04-28/

They are preserved for historical reference. They are not part of the
active migration chain. Future migrations chain forward from
0001_initial_canonical.
"""
from alembic import op  # noqa: F401  (kept for symmetry with other migrations)
import sqlalchemy as sa  # noqa: F401

# revision identifiers
revision = "0001_initial_canonical"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op. Schema is created by 0001_initial_canonical.sql, applied via
    psql out-of-band before this migration is stamped. See module docstring
    for the deployment procedure."""
    pass


def downgrade() -> None:
    """No-op. To roll back, drop and recreate the public schema as documented
    in the module docstring. This is intentionally not automated — destroying
    a 281-table schema with live data is not something an alembic downgrade
    should perform silently."""
    pass
