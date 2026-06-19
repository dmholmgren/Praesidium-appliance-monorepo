"""0125: document access log (Deal Center §1.10 / §4).

Append-only record of EVERY access to a shared/reviewed document — guest and
internal alike. Driver: deal post-mortems ask "did we get X / did we review
it / who looked at it and when?" — a complete access log must always answer it.

Logged server-side at the byte-serving chokepoint (eDiscovery file endpoint),
so it captures the deal-review viewer, the litigation review viewer, and (Unit
D) external guest reads through one hook. Append-only; never updated or deleted
in normal operation. Indexed by (tenant, matter, time) for the attorney report.
"""
from alembic import op


revision = "0125_document_access_log"
down_revision = "0124_annotation_path_data"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS document_access_log (
            id                     uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id              char(36)    NOT NULL,
            matter_id              uuid,       -- deal/matter the doc belongs to (nullable)
            ediscovery_document_id uuid,       -- the doc accessed (DD docs are eDiscovery docs)
            deal_room_document_id  uuid,       -- set when access is via a deal_room pointer (Unit D)
            dms_document_id        uuid,       -- set when access dereferences a DMS canonical doc
            collection_id          uuid,
            document_name          text,       -- denormalized for durable reporting
            actor_user_id          bigint,
            actor_email            varchar(320),
            actor_is_guest         boolean     NOT NULL DEFAULT false,
            action                 varchar(16) NOT NULL DEFAULT 'view',  -- view | open | download
            context                varchar(40),                          -- deal_review | ediscovery_review | portal | ...
            ip                     varchar(64),
            user_agent             text,
            accessed_at            timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_doc_access_matter_time "
               "ON document_access_log (tenant_id, matter_id, accessed_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_doc_access_edoc "
               "ON document_access_log (tenant_id, ediscovery_document_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_doc_access_actor "
               "ON document_access_log (tenant_id, actor_user_id, accessed_at DESC)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS document_access_log")
