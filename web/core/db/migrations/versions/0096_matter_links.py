"""0096: matter_links -- generic matter-to-matter relationship registry.

Backs the appellate-surface trial-matter projection (Scope v2 C3 / BuildOrder
Step 3): an appellate matter links to its parent trial matter via
relation='appeal_of' (from=appeal, to=trial). The on-disk folder "06 - Trial
Matter DMS Folder Link" is a read-only symlink projection of the trial matter;
this row is the DB side so the surface knows folder 06 is a nested, read-only
view of another matter (no copy -- provenance preserved).

Deliberately generic (not appeal-specific) so it also covers consolidated /
severed / related-matter links later. record designation is NOT modeled here --
record_documents already serves as the closed citable record set (C4).

Idempotent (IF NOT EXISTS) so it no-ops on an already-patched DB.
"""
from alembic import op


revision = "0096_matter_links"
down_revision = "0095_ediscovery_embed_backend"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS matter_links (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     varchar(64) NOT NULL,
            from_matter_id uuid NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
            to_matter_id   uuid NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
            relation      varchar(32) NOT NULL,
            note          text,
            created_by    text NOT NULL DEFAULT 'system',
            created_at    timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_matter_links_triple "
        "ON matter_links (from_matter_id, to_matter_id, relation)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_matter_links_from ON matter_links (from_matter_id, relation)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_matter_links_to ON matter_links (to_matter_id, relation)"
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS matter_links")
