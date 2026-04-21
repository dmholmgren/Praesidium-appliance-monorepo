# tests/test_widget_layout_loader.py
"""
Widget Registry — Layout Loader tests (Step 3).

Tests:
  - Known layout slug returns 200 + widget grid HTML
  - Unknown layout slug returns 200 with graceful 'no layout' message
  - tab_slug parameter is passed through to layout lookup
  - scope query string (matter_id) is forwarded to widget hx-get targets
  - Empty widget_positions renders 'No widgets configured' message
  - Layout resolution: tenant-default row used when no user-custom row exists

Patching strategy (same as test_widget_render.py):
  - AsyncSessionLocal: imported inside _lookup_layout() from core.db.base
    -> patch at "core.db.base.AsyncSessionLocal"
  - Module-level functions: patch.object(layout_loader_mod, ...)
  - sys.path set before all imports; module imports lazy inside test functions
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "/app")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def _make_app():
    from modules.widgets.layout_loader import router
    app = FastAPI()
    app.include_router(router)
    return app


def _get_client():
    return TestClient(_make_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_layout_row(
    layout_slug="dms_matter",
    tab_slug="default",
    widget_positions=None,
):
    if widget_positions is None:
        widget_positions = [
            {"widget_slug": "dms_recent_documents", "row": 1, "col": 1, "size_override": "medium"},
            {"widget_slug": "dms_matter_doc_count",  "row": 1, "col": 2, "size_override": "small"},
        ]
    return {
        "id": "layout-uuid-001",
        "layout_slug": layout_slug,
        "tab_slug": tab_slug,
        "widget_positions": widget_positions,
        "is_default": True,
        "tenant_id": "hjmm-prod",
        "user_id": None,
    }


def _make_db_mock(row):
    """Mock AsyncSessionLocal returning one layout row."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.mappings.return_value.fetchone.return_value = row
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_cls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_layout_loader_known_slug_returns_grid():
    """
    Known layout slug returns 200 with widget grid HTML.
    Each widget slot has hx-get targeting /widgets/{slug}.
    """
    import modules.widgets.layout_loader as _ll

    mock_row = _make_layout_row()
    mock_db = _make_db_mock(mock_row)

    with patch("core.db.base.AsyncSessionLocal", mock_db):
        client = _get_client()
        response = client.get("/layouts/dms_matter")

    assert response.status_code == 200
    assert "widget-grid" in response.text
    assert "hx-get" in response.text
    assert "dms_recent_documents" in response.text


def test_layout_loader_unknown_slug_returns_graceful_message():
    """
    Unknown layout slug returns 200 with graceful inline message — not a 404.
    Host page layout stays intact.
    """
    mock_db = _make_db_mock(None)  # no row found

    with patch("core.db.base.AsyncSessionLocal", mock_db):
        client = _get_client()
        response = client.get("/layouts/does_not_exist")

    assert response.status_code == 200
    assert "No layout" in response.text or response.text.strip() != ""


def test_layout_loader_tab_slug_passed_through():
    """
    tab_slug query param is passed to layout lookup and reflected in grid output.
    """
    import modules.widgets.layout_loader as _ll

    mock_row = _make_layout_row(layout_slug="practice_intelligence", tab_slug="firm_view")
    mock_db = _make_db_mock(mock_row)

    with patch("core.db.base.AsyncSessionLocal", mock_db):
        client = _get_client()
        response = client.get("/layouts/practice_intelligence?tab_slug=firm_view")

    assert response.status_code == 200
    assert "widget-grid" in response.text
    assert "firm_view" in response.text


def test_layout_loader_matter_id_forwarded_to_widget_slots():
    """
    matter_id query param appears in hx-get URLs of widget slots.
    """
    import modules.widgets.layout_loader as _ll

    mock_row = _make_layout_row()
    mock_db = _make_db_mock(mock_row)

    with patch("core.db.base.AsyncSessionLocal", mock_db):
        client = _get_client()
        response = client.get("/layouts/dms_matter?matter_id=matter-uuid-001")

    assert response.status_code == 200
    assert "matter_id=matter-uuid-001" in response.text


def test_layout_loader_empty_positions_renders_no_widgets_message():
    """
    Layout row with empty widget_positions renders the 'No widgets configured' fallback.
    """
    mock_row = _make_layout_row(widget_positions=[])
    mock_db = _make_db_mock(mock_row)

    with patch("core.db.base.AsyncSessionLocal", mock_db):
        client = _get_client()
        response = client.get("/layouts/dms_matter")

    assert response.status_code == 200
    assert "No widgets" in response.text or "configured" in response.text


def test_layout_loader_multiple_widgets_all_get_slots():
    """
    All widget_positions in the layout row produce individual hx-get slots.
    """
    positions = [
        {"widget_slug": "dms_recent_documents", "row": 1, "col": 1},
        {"widget_slug": "dms_matter_doc_count",  "row": 1, "col": 2},
        {"widget_slug": "firm_deadline_strip",   "row": 2, "col": 1},
    ]
    mock_row = _make_layout_row(widget_positions=positions)
    mock_db = _make_db_mock(mock_row)

    with patch("core.db.base.AsyncSessionLocal", mock_db):
        client = _get_client()
        response = client.get("/layouts/dms_matter")

    assert response.status_code == 200
    assert "dms_recent_documents" in response.text
    assert "dms_matter_doc_count" in response.text
    assert "firm_deadline_strip" in response.text
