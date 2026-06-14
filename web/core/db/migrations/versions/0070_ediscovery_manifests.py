"""0070: ediscovery_manifests - immutable terminal manifest for ingestion completeness.

One append-only row per verify run per collection. Records doc/source counts,
per-stage ledger snapshot, failed-extraction scan, reconciliation result, and a
sha256 of the canonical content (tamper-evident). UPDATE/DELETE are blocked by a
trigger so each manifest is an immutable chain-of-custody artifact.
"""
from alembic import op

revision = "0070_ediscovery_manifests"
down_revision = "0069_viewed_tracking"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS ediscovery_manifests (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     char(36) NOT NULL,
            collection_id uuid NOT NULL,
            created_at    timestamptz NOT NULL DEFAULT now(),
            created_by    varchar,
            doc_count     integer NOT NULL,
            source_count  integer,
            deferred_ocr  integer NOT NULL DEFAULT 0,
            failed_count  integer NOT NULL DEFAULT 0,
            stage_counts  jsonb NOT NULL,
            failed_sample jsonb,
            discrepancies jsonb,
            reconciled    boolean NOT NULL,
            manifest_sha  char(64) NOT NULL
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_edisc_manifests_coll
            ON ediscovery_manifests (collection_id, created_at DESC)
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION fn_edisc_manifest_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'ediscovery_manifests is append-only (chain of custody)';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_edisc_manifest_immutable ON ediscovery_manifests")
    op.execute("""
        CREATE TRIGGER trg_edisc_manifest_immutable
            BEFORE UPDATE OR DELETE ON ediscovery_manifests
            FOR EACH ROW EXECUTE FUNCTION fn_edisc_manifest_immutable()
    """)


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS trg_edisc_manifest_immutable ON ediscovery_manifests")
    op.execute("DROP FUNCTION IF EXISTS fn_edisc_manifest_immutable()")
    op.execute("DROP INDEX IF EXISTS ix_edisc_manifests_coll")
    op.execute("DROP TABLE IF EXISTS ediscovery_manifests")
