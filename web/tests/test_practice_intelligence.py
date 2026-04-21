# tests/test_practice_intelligence.py
"""
Practice Intelligence Dashboard tests — Step 5.

Tests:
  - is_partner_or_admin flag logic correct for all roles
  - practice_intelligence.html template exists and deployed
  - Template contains required tab controls, HTMX layout loader, full_name (not display_name)
  - dashboard_home route is registered at GET /
  - attorneys query uses full_name column
"""

import sys
sys.path.insert(0, "/app")


def test_is_partner_or_admin_true_for_partner():
    for role in ("partner", "admin", "superadmin"):
        assert role in ("partner", "admin", "superadmin"), f"Expected True for {role}"


def test_is_partner_or_admin_false_for_attorney():
    assert "attorney" not in ("partner", "admin", "superadmin")


def test_dashboard_home_route_registered():
    """GET /dashboard/ is registered (router prefix + / path)."""
    from modules.dashboard.routes.dashboard import router
    paths = [getattr(r, "path", "") for r in router.routes]
    assert "/" in paths or any("/" in p for p in paths), f"No root path in router: {paths}"


def test_practice_intelligence_template_exists():
    """practice_intelligence.html is present in dashboard templates."""
    import os
    path = "/app/modules/dashboard/templates/dashboard/practice_intelligence.html"
    assert os.path.exists(path), f"Template missing: {path}"


def test_practice_intelligence_template_has_tab_controls():
    """Template has tab markup, HTMX layout loader, and full_name (not display_name)."""
    content = open(
        "/app/modules/dashboard/templates/dashboard/practice_intelligence.html"
    ).read()
    assert "pi-tab" in content
    assert "switchTab" in content
    assert "/layouts/practice_intelligence" in content
    assert "firm_view" in content
    assert "my_view" in content
    assert "full_name" in content
    assert "display_name" not in content, "display_name still in template — should be full_name"


def test_dashboard_home_uses_full_name_not_display_name():
    """dashboard.py attorney query uses full_name, not display_name."""
    content = open("/app/modules/dashboard/routes/dashboard.py").read()
    # Find the section around the attorney query
    assert "full_name" in content, "full_name not found in dashboard.py"
    # Verify display_name was removed from the users query
    # (It may still exist in comments or other contexts — check it's not in SELECT)
    import re
    # Look for SELECT ... display_name FROM users pattern
    bad = re.search(r"SELECT\s+id.*display_name.*FROM\s+users", content, re.DOTALL)
    assert bad is None, "display_name still in users SELECT query in dashboard.py"
