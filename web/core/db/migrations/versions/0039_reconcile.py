"""0039 reconcile -- restore a single Alembic head after the 0038 freeze.

Everything between 0038_party_model (late Apr 2026) and 2026-06 -- the property
intelligence module, billing, email integration, custodian collections, the
scraper columns, etc. -- was applied to the live appliance as raw SQL with no
revision files. The chain and the DB were both frozen at 0038 while the real
schema moved on. This revision puts a recorded head back on the chain so that
0040 (and everything after) is tracked.

LIVE APPLIANCE: stamp only -- the post-0038 schema is already present, so we
record this revision without executing anything:

    docker exec praesidium-web alembic stamp 0039_reconcile

TIER-2 (must precede provisioning any FRESH tenant): replace the no-op upgrade()
below with the faithful post-0038 schema, so a from-scratch `alembic upgrade head`
reproduces the real schema. The authoritative snapshot has been preserved next to
this file as `0039_reconcile.sql` (schema-only pg_dump, 2026-06-02). Until that
wiring is done, a fresh build via the chain will build through 0038 + 0040 only
and will NOT include the post-0038 raw-SQL objects (property, billing, email...).
This is harmless for the running appliance; it only matters for new-tenant builds.
"""
from alembic import op  # noqa: F401

# revision identifiers, used by Alembic.
revision = "0039_reconcile"
down_revision = "0038_party_model"
branch_labels = None
depends_on = None


def upgrade():
    # No-op on the live appliance (stamped). See module docstring for the
    # Tier-2 faithful-baseline task and the preserved 0039_reconcile.sql snapshot.
    pass


def downgrade():
    pass
