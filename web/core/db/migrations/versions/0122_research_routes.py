"""research_routes — tenant-level research routing (where research queries go).

Lets each tenant select the research backend (CourtListener corpus, live
CourtListener API, matter DMS, Lexis/Westlaw, …) as CONFIG rather than code, so
the destination can change without re-encoding scripts. Resolution: a
tenant-specific row wins over the platform-default (tenant_id IS NULL) row for a
given route_key.

Seeds the platform default route 'default' -> 'courtlistener_corpus' (the local
72.5M reference_opinions ModernBERT-768 corpus; goes live once indexed).

Revision ID: 0122_research_routes
Revises: 0121_chat_attachments
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0122_research_routes"
down_revision = "0121_chat_attachments"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "research_routes",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.String(length=36)),          # NULL = platform default
        sa.Column("route_key", sa.String(length=40), nullable=False,
                  server_default=sa.text("'default'")),
        sa.Column("provider", sa.String(length=40), nullable=False),  # courtlistener_corpus|courtlistener_live|matter_dms|lexis|westlaw
        sa.Column("params", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    # one active route per (tenant-or-platform, route_key)
    op.execute(
        "CREATE UNIQUE INDEX ux_research_routes_key ON research_routes "
        "(COALESCE(tenant_id, '__platform__'), route_key)")

    # platform default -> the CourtListener corpus
    op.execute(
        "INSERT INTO research_routes (tenant_id, route_key, provider, params) "
        "VALUES (NULL, 'default', 'courtlistener_corpus', "
        "        '{\"note\": \"local reference_opinions ModernBERT-768 corpus\"}'::jsonb)")


def downgrade():
    op.drop_index("ux_research_routes_key", table_name="research_routes")
    op.drop_table("research_routes")
