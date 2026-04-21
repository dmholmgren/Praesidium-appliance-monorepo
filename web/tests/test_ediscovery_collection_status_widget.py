# tests/test_ediscovery_collection_status_widget.py
"""
ediscovery_collection_status widget tests.

Tests:
  - get_collection_summary returns correct structure
  - Empty tenant returns graceful empty state
  - by_status aggregation is correct
  - template exists and has required markup
  - registry row has render_template and data_source set
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch
sys.path.insert(0, "/app")

TENANT_ID = "hjmm-prod"
SCOPE = {"tenant_id": TENANT_ID, "matter_id": None, "limit": 10}


def _make_db_mock(rows):
    mock_session = AsyncMock()

    call_count = [0]
    async def mock_execute(query, params=None):
        call_count[0] += 1
        result = MagicMock()
        if call_count[0] == 1:
            # First call: status counts
            result.fetchall.return_value = rows["counts"]
        else:
            # Second call: collection rows
            mock_mappings = MagicMock()
            mock_mappings.fetchall.return_value = rows["collections"]
            result.mappings.return_value = mock_mappings
        return result

    mock_session.execute = mock_execute
    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_cls


def test_get_collection_summary_empty():
    """Empty tenant returns valid empty structure."""
    import asyncio
    from modules.ediscovery.services.collection_service import get_collection_summary

    mock_db = _make_db_mock({"counts": [], "collections": []})
    with patch("core.db.base.AsyncSessionLocal", mock_db):
        result = asyncio.get_event_loop().run_until_complete(
            get_collection_summary(SCOPE)
        )

    assert result["total"] == 0
    assert result["by_status"] == {}
    assert result["collections"] == []
    assert result["has_active"] is False
    assert "error" not in result


def test_get_collection_summary_with_data():
    """Returns correct aggregations when collections exist."""
    import asyncio
    from datetime import datetime, timezone
    from modules.ediscovery.services.collection_service import get_collection_summary

    from unittest.mock import MagicMock
    counts_rows = [("review_ready", 2), ("collecting", 1)]

    col_row = MagicMock()
    col_row.__iter__ = MagicMock(return_value=iter([]))
    col_mapping = {
        "id": "abc-123",
        "collection_name": "Test Collection",
        "status": "review_ready",
        "source_type": "opposing_production",
        "source_party": "Plaintiff",
        "total_docs": 100,
        "reviewed_docs": 40,
        "processed_docs": 100,
        "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
        "matter_name": "Test Litigation",
        "matter_number": "TEST-LIT-001",
    }

    mock_col = MagicMock()
    mock_col.__getitem__ = lambda self, k: col_mapping[k]
    mock_col.keys = lambda: col_mapping.keys()
    mock_col.get = lambda k, d=None: col_mapping.get(k, d)

    mock_db = _make_db_mock({"counts": counts_rows, "collections": [col_mapping]})

    # Override second execute to return proper mapping
    async def custom_execute(query, params=None):
        custom_execute.call_count = getattr(custom_execute, 'call_count', 0) + 1
        result = MagicMock()
        if custom_execute.call_count == 1:
            result.fetchall.return_value = counts_rows
        else:
            mock_mappings = MagicMock()
            mock_mappings.fetchall.return_value = [col_mapping]
            result.mappings.return_value = mock_mappings
        return result

    mock_session = AsyncMock()
    mock_session.execute = custom_execute
    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("core.db.base.AsyncSessionLocal", mock_cls):
        result = asyncio.get_event_loop().run_until_complete(
            get_collection_summary(SCOPE)
        )

    assert result["total"] == 3
    assert result["by_status"]["review_ready"] == 2
    assert result["by_status"]["collecting"] == 1
    assert result["has_active"] is True


def test_ediscovery_collection_status_template_exists():
    """Template file is present in widget templates directory."""
    import os
    path = "/app/modules/widgets/templates/widgets/ediscovery_collection_status.html"
    assert os.path.exists(path), f"Template missing: {path}"


def test_ediscovery_collection_status_template_markup():
    """Template has required status badges and collection list markup."""
    content = open(
        "/app/modules/widgets/templates/widgets/ediscovery_collection_status.html"
    ).read()
    assert "collections" in content
    assert "collection_name" in content
    assert "review_ready" in content
    assert "total_docs" in content
    assert "/ediscovery/collections" in content


def test_ediscovery_collection_status_registry_activated():
    """widget_registry row has render_template and data_source set."""
    import asyncio
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal

    async def check():
        async with AsyncSessionLocal() as s:
            r = await s.execute(text(
                "SELECT render_template, data_source FROM widget_registry "
                "WHERE widget_slug = 'ediscovery_collection_status'"
            ))
            row = r.fetchone()
            assert row is not None
            assert row[0] is not None, "render_template is NULL"
            assert row[1] is not None, "data_source is NULL"

    asyncio.get_event_loop().run_until_complete(check())
