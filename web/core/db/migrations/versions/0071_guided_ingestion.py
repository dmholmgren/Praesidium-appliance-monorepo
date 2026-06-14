"""0071: guided ingestion - proposal staging + collection provenance.

The guided ingestion flow (observe -> propose -> confirm -> execute -> account)
stages a proposal before anything is ingested. collection_proposals holds the
WI-10 proposal contract {collections[], flagged[]} plus the scoped inventory,
so the human confirm step has an editable, provenance-bearing artifact. On
confirm, one ediscovery_collections row is created per logical unit, carrying
its custodian / bucket / originating proposal_id, then run_collection_full is
enqueued per unit (every unit gets the full DAG).
"""
from alembic import op

revision = "0071_guided_ingestion"
down_revision = "0070_ediscovery_manifests"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS collection_proposals (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     char(36) NOT NULL,
            matter_id     uuid NOT NULL,
            source_paths  jsonb NOT NULL DEFAULT '[]'::jsonb,
            inventory     jsonb NOT NULL DEFAULT '{}'::jsonb,
            proposal      jsonb NOT NULL DEFAULT '{}'::jsonb,
            status        varchar NOT NULL DEFAULT 'pending',
            origin        varchar,
            created_by    uuid,
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_collection_proposals_matter
            ON collection_proposals (tenant_id, matter_id, status, created_at DESC)
    """)
    # Provenance columns on collections created by a guided confirm.
    op.execute("ALTER TABLE ediscovery_collections "
               "ADD COLUMN IF NOT EXISTS custodian varchar")
    op.execute("ALTER TABLE ediscovery_collections "
               "ADD COLUMN IF NOT EXISTS bucket varchar")
    op.execute("ALTER TABLE ediscovery_collections "
               "ADD COLUMN IF NOT EXISTS proposal_id uuid")


def downgrade():
    op.execute("ALTER TABLE ediscovery_collections DROP COLUMN IF EXISTS proposal_id")
    op.execute("ALTER TABLE ediscovery_collections DROP COLUMN IF EXISTS bucket")
    op.execute("ALTER TABLE ediscovery_collections DROP COLUMN IF EXISTS custodian")
    op.execute("DROP INDEX IF EXISTS ix_collection_proposals_matter")
    op.execute("DROP TABLE IF EXISTS collection_proposals")
