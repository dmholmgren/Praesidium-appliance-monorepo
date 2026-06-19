"""MCP credentials — issue/revoke multiple bearer tokens per user for the
Praesidium MCP resource servers (mcp:user / mcp:admin scopes).

Models the proven desktop_refresh_tokens pattern (SHA-256 hashed secret,
jti, revoked_at/revoke_reason) but generalized for MCP and explicitly
MULTI-TOKEN: no unique constraint on (tenant_id, user_id), so a user may
hold many concurrently-valid credentials, each independently revocable.

Raw secret never stored — only token_hash (SHA-256 hex, 64 chars).
jti is embedded in issued JWTs so the resource-server TokenVerifier can
reject a revoked credential before its exp.

Revision: 0135_mcp_credentials
Down:     0134_trial_presentation
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0135_mcp_credentials"
down_revision = "0134_trial_presentation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_credentials",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True, server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        # Human-readable name so multiple tokens are distinguishable.
        sa.Column("label", sa.String(120), nullable=False),
        # 'mcp:user' or 'mcp:admin' — which resource server this may call.
        sa.Column("scope", sa.String(32), nullable=False),
        # 'api_key' (header-capable clients / in-app chat) or 'oauth' (slice 3).
        sa.Column("kind", sa.String(16), nullable=False, server_default="api_key"),
        # SHA-256 hex of the opaque secret. Raw secret is shown once, never stored.
        sa.Column("token_hash", sa.String(64), nullable=False),
        # Embedded in issued JWTs; revocation check key.
        sa.Column("jti", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", sa.BigInteger, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        # NULL = non-expiring; set a timestamp for a TTL'd credential.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(64), nullable=True),
    )
    # Unique per-secret and per-jti (fast verifier lookups).
    op.create_index(
        "ux_mcp_credentials_token_hash", "mcp_credentials", ["token_hash"], unique=True,
    )
    op.create_index(
        "ux_mcp_credentials_jti", "mcp_credentials", ["jti"], unique=True,
    )
    # NON-unique: many tokens per (tenant, user) — the multi-token guarantee.
    op.create_index(
        "ix_mcp_credentials_tenant_user", "mcp_credentials", ["tenant_id", "user_id"],
    )
    # Active-by-scope listing for the verifier / admin page.
    op.create_index(
        "ix_mcp_credentials_active", "mcp_credentials", ["scope"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_credentials_active", table_name="mcp_credentials")
    op.drop_index("ix_mcp_credentials_tenant_user", table_name="mcp_credentials")
    op.drop_index("ux_mcp_credentials_jti", table_name="mcp_credentials")
    op.drop_index("ux_mcp_credentials_token_hash", table_name="mcp_credentials")
    op.drop_table("mcp_credentials")
