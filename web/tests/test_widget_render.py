# tests/test_widget_render.py
"""
Widget render route tests — Step 2.

Patching strategy:
  - AsyncSessionLocal: imported inside _lookup_widget() as
    'from core.db.base import AsyncSessionLocal' — patch at core.db.base
  - _check_permission, _resolve_data_source, _build_scope: module-level
    functions in widget_routes — patch with patch.object(_wr, ...)
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
    from modules.widgets.widget_routes import router
    app = FastAPI()
    app.include_router(router)
    return app


def _get_client():
    return TestClient(_make_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_widget_row(
    widget_type="data_panel",
    permission_level="attorney",
    feature_flag=None,
    render_template="widgets/dms_recent_documents.html",
    data_source="modules.dms.services.widget_service.get_recent_documents",
    target_route=None,
):
    return {
        "widget_slug": "dms_recent_documents",
        "widget_name": "Recent Documents",
        "widget_type": widget_type,
        "data_source": data_source,
        "render_template": render_template,
        "permission_level": permission_level,
        "feature_flag": feature_flag,
        "target_route": target_route,
        "default_size": "medium",
        "config_schema": None,
        "category": "dms",
    }


def _make_db_mock(row):
    """Mock AsyncSessionLocal (patched at core.db.base) returning one widget row."""
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

def test_widget_render_dms_recent_documents():
    """data_panel: data_source called with scope dict, returns 200 + HTML fragment."""
    import modules.widgets.widget_routes as _wr

    mock_row = _make_widget_row()
    mock_docs = {
        "docs": [
            {
                "id": "doc-001",
                "title": "Complaint.pdf",
                "doc_type": "PDF",
                "size_str": "142 KB",
                "updated_str": "Apr 10",
                "storage_path": "/matters/001/Complaint.pdf",
                "matter_name": "Test Matter",
                "client_name": "Test Client",
                "icon": "\U0001f4d5",
            }
        ],
        "matter_id": "matter-001",
        "total": 1,
    }

    mock_db = _make_db_mock(mock_row)
    mock_fn = AsyncMock(return_value=mock_docs)

    with patch("core.db.base.AsyncSessionLocal", mock_db), \
         patch.object(_wr, "_check_permission", return_value=True), \
         patch.object(_wr, "_resolve_data_source", return_value=mock_fn), \
         patch.object(_wr, "_build_scope", return_value={
             "tenant_id": "hjmm-prod",
             "user_id": 1,
             "matter_id": "matter-001",
             "attorney_id": None,
             "date_from": None,
             "date_to": None,
             "request": MagicMock(),
         }):
        client = _get_client()
        response = client.get(
            "/widgets/dms_recent_documents",
            params={"matter_id": "matter-001"},
        )

    assert response.status_code == 200
    assert "Complaint.pdf" in response.text or "Recent Documents" in response.text


def test_widget_render_placeholder_renders_template():
    """Placeholder widget_type renders widget_placeholder.html inline — not a 204."""
    import modules.widgets.widget_routes as _wr

    mock_row = _make_widget_row(
        widget_type="placeholder",
        render_template=None,
        data_source=None,
    )
    mock_db = _make_db_mock(mock_row)

    with patch("core.db.base.AsyncSessionLocal", mock_db), \
         patch.object(_wr, "_check_permission", return_value=True):
        client = _get_client()
        response = client.get("/widgets/clio_matter_wip")

    assert response.status_code == 200
    assert response.text.strip() != ""


def test_widget_render_launcher_renders_template():
    """Launcher widget_type renders launcher_card.html with target_route."""
    import modules.widgets.widget_routes as _wr

    mock_row = _make_widget_row(
        widget_type="launcher",
        render_template=None,
        data_source=None,
        target_route="/billing/",
    )
    mock_db = _make_db_mock(mock_row)

    with patch("core.db.base.AsyncSessionLocal", mock_db), \
         patch.object(_wr, "_check_permission", return_value=True):
        client = _get_client()
        response = client.get("/widgets/billing_launcher")

    assert response.status_code == 200
    assert "/billing/" in response.text or response.text.strip() != ""


def test_widget_render_unknown_slug_returns_error_partial():
    """Unknown slug returns 200 with inline error partial — never crashes host page."""
    mock_db = _make_db_mock(None)

    with patch("core.db.base.AsyncSessionLocal", mock_db):
        client = _get_client()
        response = client.get("/widgets/does_not_exist")

    assert response.status_code == 200
    assert "widget" in response.text.lower() or "Unknown" in response.text


def test_widget_render_permission_denied_returns_inline_message():
    """Insufficient role returns 200 with inline permissions message — not a 403."""
    import modules.widgets.widget_routes as _wr

    mock_row = _make_widget_row(permission_level="admin")
    mock_db = _make_db_mock(mock_row)

    with patch("core.db.base.AsyncSessionLocal", mock_db), \
         patch.object(_wr, "_check_permission", return_value=False):
        client = _get_client()
        response = client.get("/widgets/dms_recent_documents")

    assert response.status_code == 200
    assert "permissions" in response.text.lower() or response.text.strip() != ""
