"""Billing tab strip → canonical layout_tabs registry.

The live billing tab bar (billing/layout.html) was hardcoded. Rather than a
parallel table, point it at the existing layout_tabs registry (the same table
that drives matter_detail tabs and the /api/v1/nav-tabs API). The pre-existing
layout_slug='billing' rows were a stale `tab_scope` set (timekeepers/templates/
settings) that nothing rendered on desktop; replace them with the real
`navigate` strip — tab_slug values match the routes' bill_tab so the active
tab highlights, target_ref carries each link's URL.

Revision: 0137_billing_layout_tabs
Down:     0136_platform_ip_allowlist
"""
from alembic import op

revision = "0137_billing_layout_tabs"
down_revision = "0136_platform_ip_allowlist"
branch_labels = None
depends_on = None

# (tab_slug, display_name, url, order)
_BILLING = [
    ("overview",  "Overview",   "/billing",           10),
    ("clients",   "Clients",    "/billing/clients",   20),
    ("contacts",  "Contacts",   "/contacts",          30),
    ("timesheet", "Timesheet",  "/billing/timesheet", 40),
    ("run_bills", "Bill Runs",  "/billing/run-bills", 50),
    ("invoices",  "Past Bills", "/billing/invoices",  60),
    ("trust",     "Trust",      "/billing/trust",     70),
    ("reports",   "Reports",    "/billing/reports",   80),
]

# Original stale rows (for downgrade restore): tab_scope, no URL.
_ORIG = [
    ("overview", "Overview", 1), ("clients", "Clients", 2),
    ("timekeepers", "Timekeepers", 3), ("bill_runs", "Bill Runs", 4),
    ("trust", "Trust Accounts", 5), ("reports", "Reports", 6),
    ("templates", "Templates", 7), ("settings", "Settings", 8),
]


def upgrade() -> None:
    op.execute("DELETE FROM layout_tabs WHERE layout_slug = 'billing' AND tenant_id IS NULL")
    for slug, name, url, order in _BILLING:
        op.execute(
            "INSERT INTO layout_tabs "
            "(layout_slug, tab_slug, display_name, display_order, permission_level, "
            " is_visible, target_type, target_ref, is_platform_standard, is_active, "
            " separator_before, tenant_id) VALUES "
            f"('billing', '{slug}', '{name}', {order}, 'attorney', true, 'navigate', "
            f"'{url}', true, true, false, NULL)"
        )


def downgrade() -> None:
    op.execute("DELETE FROM layout_tabs WHERE layout_slug = 'billing' AND tenant_id IS NULL")
    for slug, name, order in _ORIG:
        op.execute(
            "INSERT INTO layout_tabs "
            "(layout_slug, tab_slug, display_name, display_order, permission_level, "
            " is_visible, target_type, target_ref, is_platform_standard, is_active, "
            " separator_before, tenant_id) VALUES "
            f"('billing', '{slug}', '{name}', {order}, 'attorney', true, 'tab_scope', "
            f"NULL, true, true, false, NULL)"
        )
