"""0016_m8_ssl — Add SSL columns to tenants table

Revision ID: 0016_m8_ssl
Revises: 0015_m9_timesheet
Create Date: 2026-03-31
"""

from alembic import op
import sqlalchemy as sa

revision = "0016_m8_ssl"
down_revision = "0015_m9_timesheet"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("ssl_mode", sa.Text(), nullable=False, server_default="letsencrypt"),
    )
    op.add_column(
        "tenants",
        sa.Column("ssl_cert_path", sa.Text(), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("ssl_key_path", sa.Text(), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("ssl_domain", sa.Text(), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "deployment_channel",
            sa.Text(),
            nullable=False,
            server_default="none",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "deployment_channel")
    op.drop_column("tenants", "ssl_domain")
    op.drop_column("tenants", "ssl_key_path")
    op.drop_column("tenants", "ssl_cert_path")
    op.drop_column("tenants", "ssl_mode")
