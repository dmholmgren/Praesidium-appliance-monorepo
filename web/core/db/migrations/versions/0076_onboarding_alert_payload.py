"""0076: onboarding_alerts payload + live-alert upsert key (shared alert infra).

Lays the infra the onboarding-alert system needs (per the v1 handoff): a jsonb
payload so an alert carries its exact targets, and a partial-unique index so a
re-sync refreshes the live alert in place while a dismissed one stays dismissed.

This is the same infra the eDiscovery onboarding-alert generator expects; the
deposition pipeline plugs into it as a new alert_type (DEPOSITION_TRANSCRIPT_
PENDING). Everything here is idempotent (IF NOT EXISTS) and uses the handoff's
exact names, so it composes safely whether the discovery generator's migration
lands before or after this one.
"""
from alembic import op

revision = "0076_onboarding_alert_payload"
down_revision = "0075_depo_designations_clips"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE onboarding_alerts "
               "ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb")
    # one live alert per (tenant, matter, type); dismissed rows excluded so a
    # cleared alert can recur later without colliding.
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_onb_alert_live "
               "ON onboarding_alerts (tenant_id, matter_id, alert_type) "
               "WHERE status <> 'dismissed'")


def downgrade():
    # leave payload/index in place if another stream relies on them; only drop
    # what this migration is uniquely responsible for is unsafe to assume, so
    # this downgrade is intentionally conservative (no-op on the shared infra).
    pass
