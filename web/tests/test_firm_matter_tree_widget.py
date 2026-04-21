# tests/test_firm_matter_tree_widget.py
"""
firm_matter_tree widget data source tests.

Tests:
  - get_firm_matter_tree returns clients list with matters nested
  - Clients sorted alphabetically
  - doc_count included per matter
  - Empty tenant returns empty structure gracefully
  - template exists and has required markup
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch
sys.path.insert(0, "/app")

TENANT_ID = "hjmm-prod"
SCOPE = {
    "tenant_id": TENANT_ID,
    "user_id": 1,
    "matter_id": None,
    "attorney_id": None,
    "date_from": None,
    "date_to": None,
    "request": MagicMock(),
}


def _make_db_mock(rows):
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.mappings.return_value.fetchall.return_value = rows
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_cls


def test_get_firm_matter_tree_returns_clients():
    """Returns nested client/matter structure with doc counts."""
    import asyncio
    from modules.dms.services.widget_service import get_firm_matter_tree

    rows = [
        {"id": "m-001", "matter_name": "Regent v. Jones",
         "matter_number": "7020.361", "status": "active",
         "client_id": "c-001", "client_name": "Kirksey-Regent", "doc_count": 42},
        {"id": "m-002", "matter_name": "Smith Arbitration",
         "matter_number": "7021.001", "status": "active",
         "client_id": "c-002", "client_name": "Smith Industries", "doc_count": 7},
        {"id": "m-003", "matter_name": "Asset Purchase",
         "matter_number": "7021.002", "status": "active",
         "client_id": "c-002", "client_name": "Smith Industries", "doc_count": 0},
    ]

    mock_db = _make_db_mock(rows)
    with patch("core.db.base.AsyncSessionLocal", mock_db):
        result = asyncio.get_event_loop().run_until_complete(
            get_firm_matter_tree(SCOPE)
        )

    assert result["total_clients"] == 2
    assert result["total_matters"] == 3
    assert len(result["clients"]) == 2

    # Clients sorted alphabetically
    names = [c["client_name"] for c in result["clients"]]
    assert names == sorted(names)

    # Smith has 2 matters
    smith = next(c for c in result["clients"] if "Smith" in c["client_name"])
    assert len(smith["matters"]) == 2

    # doc_count preserved
    regent = next(c for c in result["clients"] if "Kirksey" in c["client_name"])
    assert regent["matters"][0]["doc_count"] == 42
    assert "error" not in result


def test_get_firm_matter_tree_empty_returns_gracefully():
    """Empty result returns valid empty structure."""
    import asyncio
    from modules.dms.services.widget_service import get_firm_matter_tree

    mock_db = _make_db_mock([])
    with patch("core.db.base.AsyncSessionLocal", mock_db):
        result = asyncio.get_event_loop().run_until_complete(
            get_firm_matter_tree(SCOPE)
        )

    assert result["clients"] == []
    assert result["total_matters"] == 0
    assert result["total_clients"] == 0
    assert "error" not in result


def test_firm_matter_tree_template_exists():
    """firm_matter_tree.html is present in widget templates."""
    import os
    path = "/app/modules/widgets/templates/widgets/firm_matter_tree.html"
    assert os.path.exists(path), f"Template missing: {path}"


def test_firm_matter_tree_template_has_required_markup():
    """Template has client groups, matter rows, filter input, and DMS link."""
    content = open(
        "/app/modules/widgets/templates/widgets/firm_matter_tree.html"
    ).read()
    assert "fmt-client-group" in content
    assert "fmt-matter-item" in content or "fmt-matter-row" in content
    assert "filterMatterTree" in content
    assert "toggleFmtClient" in content
    assert "/dms/matter/" in content
    assert "/dms/" in content


def test_firm_matter_tree_registry_activated():
    """widget_registry row has render_template and data_source set."""
    import asyncio
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal

    async def check():
        async with AsyncSessionLocal() as s:
            r = await s.execute(text(
                "SELECT render_template, data_source FROM widget_registry "
                "WHERE widget_slug = 'firm_matter_tree'"
            ))
            row = r.fetchone()
            assert row is not None, "firm_matter_tree not in widget_registry"
            assert row[0] is not None, "render_template is NULL — run activation SQL"
            assert row[1] is not None, "data_source is NULL — run activation SQL"

    asyncio.get_event_loop().run_until_complete(check())
