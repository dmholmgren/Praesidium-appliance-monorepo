"""ediscovery_010_drift

Revision ID: ediscovery_010
Revises: ediscovery_009
Create Date: 2026-03-31

drift_events table already exists from prior session (ediscovery_009_intelligence_layer.py).
This migration adds the wiam_surfaced column needed by drift detection subsystem.
Also adds an index for fast matter-scoped queries.

Chains from ediscovery_009 (eDiscovery branch). Do NOT chain from 0016_m8_ssl.
"""

from alembic import op
import sqlalchemy as sa

revision = "ediscovery_010"
down_revision = "ediscovery_009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add wiam_surfaced — True when magnitude > 0.6, flags event for WIAM surfacing
    op.add_column(
        "drift_events",
        sa.Column(
            "wiam_surfaced",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )

    # Fast matter-scoped queries (tenant + matter + created_at for timeline ordering)
    op.create_index(
        "ix_drift_events_matter",
        "drift_events",
        ["tenant_id", "matter_id", "created_at"],
    )

    # Grant to app user
    conn = op.get_bind()
    conn.execute(sa.text("GRANT SELECT, INSERT ON drift_events TO praesidium_db"))


def downgrade() -> None:
    op.drop_index("ix_drift_events_matter", table_name="drift_events")
    op.drop_column("drift_events", "wiam_surfaced")
