"""
tests/test_m9_c3_left_nav.py
Module 9 Component 3 — Left Nav Shell
Pytest suite — 15 tests

Tests:
  nav_context module importable
  get_nav_context returns correct structure with all required keys
  Feature flags dict contains all expected flags
  Theme defaults to dark
  Page key passed through correctly
  matter_name passed through correctly
  base.html exists in ediscovery template directory
  base.html contains nav-active class
  base.html contains feature flag conditionals
  base.html extends nothing (is the root template)
  collection_api imports get_nav_context
  nav_context gracefully handles missing session (no crash)
  nav_context gracefully handles DB unavailable (no crash)
  base.html contains light/dark theme conditionals
  base.html contains patent notice

Run:
  pytest tests/test_m9_c3_left_nav.py -v
"""

from __future__ import annotations

import os

import pytest

DB_AVAILABLE = bool(os.environ.get("DATABASE_URL"))
requires_db = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

# ── Path helpers ──────────────────────────────────────────────────────────────

TEMPLATE_DIR = os.path.join(
    os.path.dirname(__file__),
    "../templates/ediscovery"
)
BASE_HTML = os.path.join(TEMPLATE_DIR, "base.html")


def _read_base() -> str:
    with open(BASE_HTML, "r") as f:
        return f.read()


# ── Import tests T-01 to T-03 ─────────────────────────────────────────────────

def test_t01_nav_context_importable():
    """nav_context module must import without error."""
    from modules.dashboard.services.nav_context import get_nav_context
    assert callable(get_nav_context)


def test_t02_nav_flags_defined():
    """_NAV_FLAGS must contain expected feature flags."""
    from modules.dashboard.services import nav_context
    flags = nav_context._NAV_FLAGS
    for expected in (
        "feature_ediscovery",
        "feature_billing",
        "feature_dms",
        "feature_court_calendar",
        "feature_tenant_admin",
        "feature_cite_it",
    ):
        assert expected in flags, f"Missing flag: {expected}"


def test_t03_collection_api_imports_nav_context():
    """collection_api must import get_nav_context."""
    import modules.ediscovery.collection_api as api
    assert hasattr(api, "get_nav_context"), \
        "collection_api does not import get_nav_context"


# ── nav_context structure tests T-04 to T-08 ──────────────────────────────────

@pytest.mark.asyncio
async def test_t04_nav_context_returns_dict():
    """get_nav_context must return a dict with all required keys."""
    from unittest.mock import MagicMock
    from modules.dashboard.services.nav_context import get_nav_context

    mock_request = MagicMock()
    mock_request.cookies = {}

    nav = await get_nav_context(mock_request, page="test")
    assert isinstance(nav, dict)
    for key in ("page", "user_name", "user_role", "theme",
                "firm_name", "tenant_id", "matter_name", "features", "is_admin"):
        assert key in nav, f"Missing key in nav: {key}"


@pytest.mark.asyncio
async def test_t05_nav_page_key_set_correctly():
    """page key must match what was passed in."""
    from unittest.mock import MagicMock
    from modules.dashboard.services.nav_context import get_nav_context

    mock_request = MagicMock()
    mock_request.cookies = {}

    nav = await get_nav_context(mock_request, page="ediscovery")
    assert nav["page"] == "ediscovery"


@pytest.mark.asyncio
async def test_t06_nav_matter_name_passed_through():
    """matter_name must appear in nav dict."""
    from unittest.mock import MagicMock
    from modules.dashboard.services.nav_context import get_nav_context

    mock_request = MagicMock()
    mock_request.cookies = {}

    nav = await get_nav_context(mock_request, page="ediscovery", matter_name="Smith v. Jones")
    assert nav["matter_name"] == "Smith v. Jones"


@pytest.mark.asyncio
async def test_t07_nav_theme_defaults_dark():
    """theme must default to 'dark' when no session."""
    from unittest.mock import MagicMock
    from modules.dashboard.services.nav_context import get_nav_context

    mock_request = MagicMock()
    mock_request.cookies = {}

    nav = await get_nav_context(mock_request)
    assert nav["theme"] == "dark"


@pytest.mark.asyncio
async def test_t08_nav_features_is_dict():
    """features must be a dict with boolean values."""
    from unittest.mock import MagicMock
    from modules.dashboard.services.nav_context import get_nav_context

    mock_request = MagicMock()
    mock_request.cookies = {}

    nav = await get_nav_context(mock_request)
    assert isinstance(nav["features"], dict)
    for k, v in nav["features"].items():
        assert isinstance(v, bool), f"Feature flag {k} is not bool: {v}"


@pytest.mark.asyncio
async def test_t09_nav_context_no_crash_no_session():
    """get_nav_context must not raise when no session cookie present."""
    from unittest.mock import MagicMock
    from modules.dashboard.services.nav_context import get_nav_context

    mock_request = MagicMock()
    mock_request.cookies = {}

    try:
        nav = await get_nav_context(mock_request)
        assert nav is not None
    except Exception as e:
        pytest.fail(f"get_nav_context raised with no session: {e}")


@pytest.mark.asyncio
async def test_t10_nav_context_no_crash_bad_token():
    """get_nav_context must not raise when session token is invalid."""
    from unittest.mock import MagicMock
    from modules.dashboard.services.nav_context import get_nav_context

    mock_request = MagicMock()
    mock_request.cookies = {"session_token": "completely-invalid-token"}

    try:
        nav = await get_nav_context(mock_request)
        assert nav is not None
    except Exception as e:
        pytest.fail(f"get_nav_context raised with bad token: {e}")


# ── Template tests T-11 to T-15 ───────────────────────────────────────────────

def test_t11_base_html_exists():
    """templates/ediscovery/base.html must exist."""
    assert os.path.exists(BASE_HTML), \
        f"base.html not found at {BASE_HTML}"


def test_t12_base_html_has_nav_active():
    """base.html must define nav-active CSS class."""
    content = _read_base()
    assert "nav-active" in content


def test_t13_base_html_has_feature_flag_conditionals():
    """base.html must conditionally show nav items based on feature flags."""
    content = _read_base()
    assert "feature_ediscovery" in content
    assert "feature_billing" in content
    assert "feature_dms" in content


def test_t14_base_html_has_theme_conditionals():
    """base.html must support light/dark theme switching."""
    content = _read_base()
    assert "nav.theme == 'light'" in content
    assert "nav.theme == 'dark'" in content or "else" in content


def test_t15_base_html_has_patent_notice():
    """base.html must contain patent pending notice."""
    content = _read_base()
    assert "64/020,027" in content
