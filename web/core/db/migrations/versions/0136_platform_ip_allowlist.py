"""platform_ip_allowlist — appliance-wide source-IP/CIDR allowlist managed from
Platform Admin. Single source of truth that can be applied to two enforcement
surfaces: the nginx edge (MCP/admin vhosts) and the host firewall.

Platform-global (not tenant-scoped) — this is appliance infrastructure.
'target' selects where an entry applies: 'nginx' | 'firewall' | 'both'.

Revision: 0136_platform_ip_allowlist
Down:     0135_mcp_credentials
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0136_platform_ip_allowlist"
down_revision = "0135_mcp_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_ip_allowlist",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        # CIDR or single IP, validated in the app layer (ipaddress).
        sa.Column("cidr", sa.Text, nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        # 'nginx' | 'firewall' | 'both'
        sa.Column("target", sa.String(16), nullable=False, server_default="firewall"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_by", sa.BigInteger, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Prevent duplicate (cidr, target) pairs.
    op.create_index("ux_ip_allowlist_cidr_target", "platform_ip_allowlist",
                    ["cidr", "target"], unique=True)
    op.create_index("ix_ip_allowlist_active", "platform_ip_allowlist",
                    ["target"], postgresql_where=sa.text("is_active = true"))


def downgrade() -> None:
    op.drop_index("ix_ip_allowlist_active", table_name="platform_ip_allowlist")
    op.drop_index("ux_ip_allowlist_cidr_target", table_name="platform_ip_allowlist")
    op.drop_table("platform_ip_allowlist")
