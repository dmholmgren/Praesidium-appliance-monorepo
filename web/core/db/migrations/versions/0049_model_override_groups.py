"""0049 model override + provisioned groups: per-matter model/provider switch.

Two coupled changes that make "switch model/provider within a step, override
per matter, from a pre-provisioned group" expressible as data:

  1. ai_model_routing gains matter_id -> the resolver gains a third, most-
     specific cascade level (matter > tenant > global), same shape as
     processing_thresholds. The published-unique index is rebuilt to include
     matter_id (COALESCE so NULL levels are distinct keys, not collapsed).

  2. model_provider_groups: tenant-admin-curated allow-lists of {provider,
     model} members. A step opts into overrides via its EXISTING override_policy
     jsonb ({"allow_override": true, "group_key": "..."}); a matter override is
     valid only if the chosen model is a member of that group. "Provisioned"
     means each member has creds in credentials_vault AND a row in the adapter
     MODEL_PRICING table, so a per-matter pick can't silently misbill. A step
     left at allow_override=false is locked -- this is how the local-only
     privilege boundary stays put.

Resolver (matter_id in the specificity ORDER BY) and the write-path group
validation are wired separately; this revision is schema only. Groups ship
empty -- tenant admin populates them (model strings are volatile, not migration
data).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0049_model_override_groups"
down_revision = "0048_processing_thresholds"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Per-matter override level on the routing table.
    op.execute("ALTER TABLE ai_model_routing ADD COLUMN IF NOT EXISTS matter_id uuid REFERENCES matters(id)")

    # Rebuild published-unique to include matter_id. COALESCE so NULL tenant/matter
    # levels are distinct keys rather than Postgres-distinct (the dedupe gap).
    op.execute("DROP INDEX IF EXISTS uq_ai_model_routing_tenant_module_purpose_published")
    op.execute(
        "CREATE UNIQUE INDEX uq_ai_model_routing_published "
        "ON ai_model_routing "
        "(COALESCE(tenant_id::text, '~global'), COALESCE(matter_id::text, '~all'), module, purpose) "
        "WHERE status = 'published'"
    )

    # 2. Pre-provisioned model/provider groups (the override allow-list).
    op.create_table(
        "model_provider_groups",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column("tenant_id", sa.CHAR(36), sa.ForeignKey("tenants.id"), nullable=True, index=True),  # NULL = platform-default group
        sa.Column("group_key", sa.String(64), nullable=False),
        sa.Column("label", sa.String(128), nullable=True),
        sa.Column("members", JSONB, nullable=False, server_default="[]"),   # [{"provider","model","label"}]
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="published"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.BigInteger, nullable=True),
        sa.Index("ix_model_provider_groups_lookup", "tenant_id", "group_key", "status"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_model_provider_groups_published "
        "ON model_provider_groups "
        "(COALESCE(tenant_id::text, '~global'), group_key) "
        "WHERE status = 'published'"
    )


def downgrade():
    op.drop_table("model_provider_groups")
    op.execute("DROP INDEX IF EXISTS uq_ai_model_routing_published")
    op.execute(
        "CREATE UNIQUE INDEX uq_ai_model_routing_tenant_module_purpose_published "
        "ON ai_model_routing (tenant_id, module, purpose) WHERE status = 'published'"
    )
    op.execute("ALTER TABLE ai_model_routing DROP COLUMN IF EXISTS matter_id")
