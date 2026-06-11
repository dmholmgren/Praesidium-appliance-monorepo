"""M-DESK C1 — desktop_refresh_tokens + documents.parent_doc_id UUID fix

Revision ID: 0009_m_desk_c1
Revises:     0008_matter_contact_proposals
Create Date: 2026-05-06

Two operations bundled because both are foundational to M-DESK C1
(Backend Foundation for the Praesidium Desktop Client) and both must be
in place before any desktop endpoint can be deployed:

  1. Create `desktop_refresh_tokens` — long-lived refresh-token store for
     the VSTO desktop client. Per-token row, hashed (never raw), with
     full revocation + last-used auditability. Separate from the auth
     `sessions` table because session cookies and refresh tokens are
     genuinely different lifetimes and use cases (Option A; chosen over
     overloading `credentials_vault` or `sessions`).

  2. Fix `documents.parent_doc_id` BIGINT -> UUID. The `documents.id`
     column is UUID, so `parent_doc_id` is currently a self-FK to the
     wrong type — version chains can never be written against the
     current schema. Fixed now while `documents` is empty (0 rows on
     appliance at time of this migration). USING NULL is sufficient
     because there is nothing to convert.

Conventions match prior migrations in the active chain (0001..0008):
  * tenant_id uses sa.String(36), not sa.CHAR(36).
  * UUID server_default uses uuid_generate_v4() (uuid-ossp extension)
    rather than gen_random_uuid() (pgcrypto), matching 0008.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# --- Alembic identifiers -----------------------------------------------------

revision = "0009_m_desk_c1"
down_revision = "0008_matter_contact_proposals"
branch_labels = None
depends_on = None


# --- Upgrade -----------------------------------------------------------------

def upgrade() -> None:
    # 1. desktop_refresh_tokens
    op.create_table(
        "desktop_refresh_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column(
            "token_hash",
            sa.String(64),
            nullable=False,
            unique=True,
            comment="SHA-256 of the refresh token. Raw token never persisted.",
        ),
        sa.Column(
            "jti",
            postgresql.UUID(as_uuid=False),
            nullable=False,
            unique=True,
            comment="JWT ID embedded in the access token issued from this refresh token.",
        ),
        sa.Column(
            "client",
            sa.String(64),
            nullable=False,
            comment="Client identifier, e.g. 'desktop-vsto-1.0.0'.",
        ),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(64), nullable=True),
    )

    # Look-ups by (user, tenant); revoked_at filters active-only via IS NULL.
    op.create_index(
        "ix_desktop_refresh_tokens_user_tenant",
        "desktop_refresh_tokens",
        ["user_id", "tenant_id", "revoked_at"],
    )

    # Cleanup job will sweep on expires_at.
    op.create_index(
        "ix_desktop_refresh_tokens_expires_at",
        "desktop_refresh_tokens",
        ["expires_at"],
    )

    # 2. documents.parent_doc_id BIGINT -> UUID
    # Safe because documents has 0 rows on the appliance at migration time.
    # USING NULL drops any prior bigint values (none exist).
    op.execute(
        "ALTER TABLE documents "
        "ALTER COLUMN parent_doc_id TYPE uuid USING NULL"
    )


# --- Downgrade ---------------------------------------------------------------

def downgrade() -> None:
    # Reverse the parent_doc_id change first (data drops to NULL again).
    op.execute(
        "ALTER TABLE documents "
        "ALTER COLUMN parent_doc_id TYPE bigint USING NULL"
    )

    op.drop_index(
        "ix_desktop_refresh_tokens_expires_at",
        table_name="desktop_refresh_tokens",
    )
    op.drop_index(
        "ix_desktop_refresh_tokens_user_tenant",
        table_name="desktop_refresh_tokens",
    )
    op.drop_table("desktop_refresh_tokens")
