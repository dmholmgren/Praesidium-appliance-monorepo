# tests/test_dms_widgets.py
"""
DMS Widget data source tests — Step 4.

Tests the four data source functions in modules.dms.services.dms_service.
Each function receives a scope dict and returns a template context dict.
No HTTP layer — tests call the functions directly with a mocked DB session.

Patching: AsyncSessionLocal imported inside each function from core.db.base.
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/app")

TENANT_ID = "hjmm-prod"
MATTER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

SCOPE = {
    "tenant_id": TENANT_ID,
    "user_id": 1,
    "matter_id": MATTER_ID,
    "attorney_id": None,
    "date_from": None,
    "date_to": None,
    "request": MagicMock(),
}


def _make_db_mock(rows_by_call=None):
    """
    Build a mock AsyncSessionLocal that returns different rows per execute() call.
    rows_by_call: list of return values, one per execute() call in order.
    If None, returns empty result for all calls.
    """
    call_count = {"n": 0}
    rows_by_call = rows_by_call or []

    async def mock_execute(query, params=None):
        idx = call_count["n"]
        call_count["n"] += 1
        result = MagicMock()
        if idx < len(rows_by_call):
            rv = rows_by_call[idx]
        else:
            rv = None

        if isinstance(rv, list):
            result.mappings.return_value.fetchall.return_value = rv
            result.mappings.return_value.fetchone.return_value = rv[0] if rv else None
            result.fetchall.return_value = rv
            result.fetchone.return_value = rv[0] if rv else None
            result.scalar.return_value = rv[0] if rv else 0
        elif rv is None:
            result.mappings.return_value.fetchall.return_value = []
            result.mappings.return_value.fetchone.return_value = None
            result.fetchall.return_value = []
            result.fetchone.return_value = None
            result.scalar.return_value = 0
        else:
            # Single scalar or mapping
            result.mappings.return_value.fetchone.return_value = rv
            result.fetchone.return_value = rv
            result.scalar.return_value = rv

        return result

    mock_session = AsyncMock()
    mock_session.execute = mock_execute

    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_cls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_get_matter_document_count_returns_counts():
    """get_matter_document_count returns native_count, folder_count, total_size_str."""
    import modules.dms.services.dms_service as svc

    # Call order: 1) doc count+size, 2) folder count, 3) matter name
    dc_row = MagicMock()
    dc_row.__getitem__ = lambda self, k: {"cnt": 42, "total_size": 1024 * 1024 * 5}[k]
    dc_row.keys = lambda: ["cnt", "total_size"]

    folder_mock = MagicMock()
    folder_mock.scalar.return_value = 7

    matter_mock = MagicMock()
    matter_mock.fetchone.return_value = ("Regent (Collin County)",)

    call_count = {"n": 0}

    async def mock_execute(query, params=None):
        n = call_count["n"]
        call_count["n"] += 1
        result = MagicMock()
        if n == 0:
            result.mappings.return_value.fetchone.return_value = dc_row
        elif n == 1:
            result.scalar.return_value = 7
        else:
            result.fetchone.return_value = ("Regent (Collin County)",)
        return result

    mock_session = AsyncMock()
    mock_session.execute = mock_execute
    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    import asyncio
    with patch("core.db.base.AsyncSessionLocal", mock_cls):
        result = asyncio.get_event_loop().run_until_complete(
            svc.get_matter_document_count(SCOPE)
        )

    assert result["native_count"] == 42
    assert result["folder_count"] == 7
    assert result["matter_name"] == "Regent (Collin County)"
    assert "MB" in result["total_size_str"] or "KB" in result["total_size_str"]
    assert "error" not in result


def test_get_document_activity_returns_list():
    """get_document_activity returns activity list with required keys."""
    import modules.dms.services.dms_service as svc
    from datetime import datetime

    doc_row = {
        "id": "doc-001",
        "file_name": "Complaint.pdf",
        "doc_type": "PDF",
        "status": "active",
        "created_at": datetime(2026, 1, 1, 12, 0, 0),
        "updated_at": datetime(2026, 4, 1, 15, 30, 0),
        "storage_path": "/matters/001/Complaint.pdf",
    }

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.mappings.return_value.fetchall.return_value = [doc_row]
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    import asyncio
    with patch("core.db.base.AsyncSessionLocal", mock_cls):
        result = asyncio.get_event_loop().run_until_complete(
            svc.get_document_activity(SCOPE)
        )

    assert "activity" in result
    assert len(result["activity"]) == 1
    item = result["activity"][0]
    assert item["file_name"] == "Complaint.pdf"
    assert item["action"] in ("Added", "Modified")
    assert "updated_str" in item
    assert "icon" in item
    assert "error" not in result


def test_get_checked_out_documents_returns_feature_pending():
    """get_checked_out_documents returns feature_pending=True (checkout not yet built)."""
    import modules.dms.services.dms_service as svc
    import asyncio

    result = asyncio.get_event_loop().run_until_complete(
        svc.get_checked_out_documents(SCOPE)
    )

    assert result["feature_pending"] is True
    assert result["checked_out"] == []
    assert result["matter_id"] == MATTER_ID


def test_get_folder_health_returns_folders():
    """get_folder_health returns folders list with root_label and file_count."""
    import modules.dms.services.dms_service as svc

    folder_row = {
        "id": "folder-001",
        "folder_path": "Clients/Regent/Pleadings",
        "disk_root": "\\\\server\\clients",
        "file_count": 12,
    }

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.mappings.return_value.fetchall.return_value = [folder_row]
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    import asyncio
    with patch("core.db.base.AsyncSessionLocal", mock_cls):
        result = asyncio.get_event_loop().run_until_complete(
            svc.get_folder_health(SCOPE)
        )

    assert "folders" in result
    assert result["total_folders"] == 1
    folder = result["folders"][0]
    assert folder["file_count"] == 12
    assert folder["short_name"] == "Pleadings"
    assert folder["root_label"] in ("Clients", "Docsend", "Praesidium")
    assert "error" not in result
