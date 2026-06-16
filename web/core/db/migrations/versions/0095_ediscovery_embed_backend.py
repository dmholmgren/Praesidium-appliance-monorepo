"""0095: ediscovery_embed_backend -- per-collection embedding backend switch.

Adds ediscovery_collections.embed_backend ('v100' default | 'runpod'). Selects
where a collection's chunks get embedded: 'runpod' routes to the burst shim
(RunPod serverless, V100 failover); anything else uses the local V100 service.

Codifies a column that was added live via raw DDL on the appliance (2026-06-15)
ahead of this migration. Idempotent (ADD COLUMN IF NOT EXISTS) so it no-ops on
the already-patched DB and creates the column on a fresh database.
"""
from alembic import op


revision = "0095_ediscovery_embed_backend"
down_revision = "0094_record_fact_element_links"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE ediscovery_collections "
        "ADD COLUMN IF NOT EXISTS embed_backend varchar(16) NOT NULL DEFAULT 'v100'"
    )


def downgrade():
    op.execute("ALTER TABLE ediscovery_collections DROP COLUMN IF EXISTS embed_backend")
