# tests/test_ediscovery_landing.py
"""
eDiscovery landing page tests.

Tests:
  - GET /ediscovery returns 200 (not 302 redirect)
  - ediscovery_home.html template exists
  - Template has tab controls and HTMX layout loader
  - collections route still registered under same router
"""

import sys
sys.path.insert(0, "/app")


def test_ediscovery_home_route_is_not_redirect():
    """GET /ediscovery is registered as a real route, not a redirect."""
    from modules.ediscovery.routes import router
    routes = {getattr(r, "path", "") for r in router.routes}
    # Check direct routes and included sub-routers
    all_paths = set()
    for r in router.routes:
        all_paths.add(getattr(r, "path", ""))
    assert "/ediscovery" in all_paths, f"GET /ediscovery not found. Routes: {all_paths}"


def test_ediscovery_home_template_exists():
    """ediscovery_home.html is present in ediscovery templates."""
    import os
    path = "/app/modules/ediscovery/templates/ediscovery/ediscovery_home.html"
    assert os.path.exists(path), f"Template missing: {path}"


def test_ediscovery_home_template_has_tabs_and_layout_loader():
    """Template has tab controls and loads ediscovery_firm layout via HTMX."""
    content = open(
        "/app/modules/ediscovery/templates/ediscovery/ediscovery_home.html"
    ).read()
    assert "edisco-tab" in content
    assert "switchEdiscoTab" in content
    assert "/layouts/ediscovery_firm" in content
    assert "collections" in content
    assert "overview" in content


def test_collections_route_still_registered():
    """Collections sub-router still accessible under main ediscovery router."""
    from modules.ediscovery.routes import router
    # The collections router is included — verify it hasn't been dropped
    assert len(router.routes) > 1, "Only one route registered — collections router may be missing"


def test_ediscovery_home_no_redirect():
    """routes/__init__.py does not contain a redirect to /dashboard."""
    content = open("/app/modules/ediscovery/routes/__init__.py").read()
    assert 'RedirectResponse' not in content, \
        "RedirectResponse still in routes/__init__.py — redirect not removed"
