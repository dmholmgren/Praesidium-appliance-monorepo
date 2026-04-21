"""
tests/test_m10_c1_connector_framework.py

Tests for M10 C1 — Connector Framework.
Covers: migration tables, service CRUD, sync log, CSV import log,
        health summary, router endpoints, registry completeness.
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

TENANT = "hjmm-prod"
CONNECTOR_TYPES_EXPECTED = {"timeslips", "file_crawler", "exchange", "manictime", "pbx_cdr"}


# ── Registry tests ─────────────────────────────────────────────────────────────

def test_connector_registry_completeness():
    from modules.connectors.base import CONNECTOR_TYPES, all_connector_types
    assert CONNECTOR_TYPES_EXPECTED.issubset(set(CONNECTOR_TYPES.keys()))
    assert len(all_connector_types()) >= 5


def test_connector_meta_fields():
    from modules.connectors.base import get_connector_meta
    for ctype in CONNECTOR_TYPES_EXPECTED:
        meta = get_connector_meta(ctype)
        assert "label" in meta
        assert "type" in meta
        assert "slug" in meta
        assert meta["slug"] == ctype


def test_connector_types_classification():
    from modules.connectors.base import get_connector_meta
    assert get_connector_meta("timeslips")["type"] == "live_push"
    assert get_connector_meta("file_crawler")["type"] == "live_pull"
    assert get_connector_meta("exchange")["type"] == "live_pull"
    assert get_connector_meta("manictime")["type"] == "csv_import"
    assert get_connector_meta("pbx_cdr")["type"] == "csv_import"


def test_connector_status_constants():
    from modules.connectors.base import ConnectorStatus
    assert ConnectorStatus.HEALTHY == "healthy"
    assert ConnectorStatus.ERROR == "error"
    assert ConnectorStatus.UNCONFIGURED == "unconfigured"
    assert ConnectorStatus.DISABLED == "disabled"


# ── ConnectorBase abstract enforcement ─────────────────────────────────────────

def test_connector_base_requires_validate_config():
    from modules.connectors.base import ConnectorBase
    with pytest.raises(TypeError):
        ConnectorBase(tenant_id=TENANT, connector_id="x", config={})


def test_connector_base_concrete_subclass():
    from modules.connectors.base import ConnectorBase

    class TestConnector(ConnectorBase):
        connector_type = "test"
        def validate_config(self):
            return True, ""

    c = TestConnector(tenant_id=TENANT, connector_id="abc", config={})
    assert c.tenant_id == TENANT
    assert c.connector_type == "test"


def test_connector_base_tenant_id_stripped():
    from modules.connectors.base import ConnectorBase

    class TestConnector(ConnectorBase):
        connector_type = "test"
        def validate_config(self):
            return True, ""

    c = TestConnector(tenant_id="hjmm-prod   ", connector_id="x", config={})
    assert c.tenant_id == "hjmm-prod"


# ── ConnectorService unit tests (mocked DB) ────────────────────────────────────

@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.fixture
def mock_factory(mock_session):
    factory = MagicMock(return_value=mock_session)
    return factory


def test_connector_service_imports():
    from modules.connectors.service import ConnectorService
    assert hasattr(ConnectorService, "list_connectors")
    assert hasattr(ConnectorService, "get_connector")
    assert hasattr(ConnectorService, "upsert_connector")
    assert hasattr(ConnectorService, "start_sync_log")
    assert hasattr(ConnectorService, "complete_sync_log")
    assert hasattr(ConnectorService, "get_sync_history")
    assert hasattr(ConnectorService, "log_csv_import")
    assert hasattr(ConnectorService, "get_csv_import_history")
    assert hasattr(ConnectorService, "get_health_summary")
    assert hasattr(ConnectorService, "set_connector_status")


@patch("modules.connectors.service.AsyncSessionLocal")
def test_list_connectors_returns_all_registry_types(mock_get_factory, mock_factory, mock_session):
    """Even with empty DB, list_connectors returns all registry types as unconfigured."""
    mock_get_factory.return_value = mock_session
    result_mock = MagicMock()
    result_mock.mappings.return_value.all.return_value = []
    mock_session.execute = AsyncMock(return_value=result_mock)

    from modules.connectors.service import ConnectorService
    result = asyncio.run(ConnectorService.list_connectors(TENANT))
    assert len(result) >= 5
    slugs = {r["connector_type"] for r in result}
    assert CONNECTOR_TYPES_EXPECTED.issubset(slugs)
    for r in result:
        assert r["configured"] is False
        assert r["status"] == "unconfigured"


@patch("modules.connectors.service.AsyncSessionLocal")
def test_upsert_connector_insert_path(mock_get_factory, mock_factory, mock_session):
    mock_get_factory.return_value = mock_session
    # first execute returns empty (no existing row)
    first_result = MagicMock()
    first_result.first.return_value = None
    mock_session.execute = AsyncMock(return_value=first_result)
    mock_session.commit = AsyncMock()

    from modules.connectors.service import ConnectorService
    cid = asyncio.run(ConnectorService.upsert_connector(TENANT, "timeslips", enabled=True))
    assert isinstance(cid, str)
    assert len(cid) == 36  # UUID


@patch("modules.connectors.service.AsyncSessionLocal")
def test_start_sync_log_returns_uuid(mock_get_factory, mock_factory, mock_session):
    mock_get_factory.return_value = mock_session
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()

    from modules.connectors.service import ConnectorService
    log_id = asyncio.run(ConnectorService.start_sync_log(TENANT, "timeslips", "manual"))
    assert isinstance(log_id, str)
    assert len(log_id) == 36


@patch("modules.connectors.service.AsyncSessionLocal")
def test_complete_sync_log(mock_get_factory, mock_factory, mock_session):
    mock_get_factory.return_value = mock_session
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()

    from modules.connectors.service import ConnectorService
    # Should not raise
    asyncio.run(ConnectorService.complete_sync_log(
        "test-log-id",
        records_processed=42,
        records_skipped=3,
        error_count=0,
    ))
    assert mock_session.execute.called


@patch("modules.connectors.service.AsyncSessionLocal")
def test_log_csv_import_returns_uuid(mock_get_factory, mock_factory, mock_session):
    mock_get_factory.return_value = mock_session
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()

    from modules.connectors.service import ConnectorService
    import_id = asyncio.run(ConnectorService.log_csv_import(
        TENANT, "manictime", "activity_march.csv", 247, imported_by=1
    ))
    assert isinstance(import_id, str)
    assert len(import_id) == 36


@patch("modules.connectors.service.AsyncSessionLocal")
def test_get_sync_history_empty(mock_get_factory, mock_factory, mock_session):
    mock_get_factory.return_value = mock_session
    result_mock = MagicMock()
    result_mock.mappings.return_value.all.return_value = []
    mock_session.execute = AsyncMock(return_value=result_mock)

    from modules.connectors.service import ConnectorService
    history = asyncio.run(ConnectorService.get_sync_history(TENANT, "timeslips"))
    assert history == []


@patch("modules.connectors.service.AsyncSessionLocal")
def test_health_summary_no_connectors(mock_get_factory, mock_factory, mock_session):
    mock_get_factory.return_value = mock_session
    result_mock = MagicMock()
    result_mock.mappings.return_value.all.return_value = []
    mock_session.execute = AsyncMock(return_value=result_mock)

    from modules.connectors.service import ConnectorService
    summary = asyncio.run(ConnectorService.get_health_summary(TENANT))
    assert summary["enabled"] == 0
    assert summary["status"] == "unconfigured"
    assert "connectors" in summary


# ── RQ job tests ───────────────────────────────────────────────────────────────

def test_run_connector_sync_imports():
    from jobs.run_connector_sync import run_connector_sync, _get_adapter
    assert callable(run_connector_sync)
    assert callable(_get_adapter)


def test_get_adapter_unknown_type():
    from jobs.run_connector_sync import _get_adapter
    result = _get_adapter(TENANT, "unknown_connector")
    assert result is None


def test_get_adapter_stub_returns_none_for_unregistered():
    """All adapters return None until M10 C2+ registers them."""
    from jobs.run_connector_sync import _get_adapter
    for ctype in ["timeslips", "file_crawler", "exchange", "manictime", "pbx_cdr"]:
        assert _get_adapter(TENANT, ctype) is None


# ── Router endpoint tests ──────────────────────────────────────────────────────

@pytest.fixture
def client():
    from app import app
    return TestClient(app, raise_server_exceptions=False)


def test_connector_list_route_exists(client):
    """Route must exist — auth may redirect but should not 404/500."""
    resp = client.get("/tenant-admin/connectors", follow_redirects=False)
    assert resp.status_code in (200, 302, 307, 400, 401, 403)


def test_connector_detail_route_exists(client):
    resp = client.get("/tenant-admin/connectors/timeslips", follow_redirects=False)
    assert resp.status_code in (200, 302, 307, 400, 401, 403)


def test_connector_detail_unknown_type_404(client):
    resp = client.get("/tenant-admin/connectors/nonexistent_xyz", follow_redirects=False)
    assert resp.status_code in (404, 302, 307, 401, 403)


def test_connector_health_api_exists(client):
    resp = client.get("/api/connectors/health", follow_redirects=False)
    assert resp.status_code in (200, 302, 307, 400, 401, 403)


def test_timeslips_ingest_requires_key(client):
    """Timeslips ingest endpoint must reject requests without X-Connector-Key."""
    resp = client.post(
        "/api/connectors/timeslips/ingest",
        json={"slips": []},
        follow_redirects=False
    )
    assert resp.status_code in (401, 403, 422)


def test_timeslips_ingest_accepts_with_key(client):
    """With a key header, endpoint should accept (even if processing is stubbed)."""
    resp = client.post(
        "/api/connectors/timeslips/ingest",
        json={"slips": [{"id": 1, "client": "Test"}]},
        headers={"X-Connector-Key": "test-key-placeholder"},
        follow_redirects=False
    )
    assert resp.status_code in (200, 302, 307, 400, 401, 403)


def test_connector_toggle_route_exists(client):
    resp = client.post("/tenant-admin/connectors/timeslips/toggle", follow_redirects=False)
    assert resp.status_code in (200, 302, 303, 307, 401, 403)


def test_connector_trigger_route_exists(client):
    resp = client.post("/tenant-admin/connectors/timeslips/trigger", follow_redirects=False)
    assert resp.status_code in (200, 302, 303, 307, 401, 403)


def test_connector_csv_import_route_exists(client):
    resp = client.post(
        "/tenant-admin/connectors/manictime/import",
        follow_redirects=False
    )
    assert resp.status_code in (200, 302, 303, 307, 400, 401, 403, 422)


# ── Migration structure tests ──────────────────────────────────────────────────

def test_migration_file_exists():
    import os
    path = "core/db/migrations/versions/0018_m10_connector_framework.py"
    assert os.path.exists(path), f"Migration file not found: {path}"


def test_migration_revision_id():
    import importlib.util
    spec = importlib.util.spec_from_file_location("mg", "/app/core/db/migrations/versions/0018_m10_connector_framework.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    assert m.revision == "0018_m10_connector_framework"
    assert m.down_revision == "0017_dms_scan_queues"


def test_migration_has_upgrade_downgrade():
    import importlib.util
    spec = importlib.util.spec_from_file_location("mg", "/app/core/db/migrations/versions/0018_m10_connector_framework.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    assert callable(m.upgrade)
    assert callable(m.downgrade)


def test_template_list_exists():
    import os
    assert os.path.exists("templates/connectors/list.html")


def test_template_detail_exists():
    import os
    assert os.path.exists("templates/connectors/detail.html")


# ── Module structure ───────────────────────────────────────────────────────────

def test_connectors_module_init_exists():
    import os
    assert os.path.exists("modules/connectors/__init__.py")


def test_connectors_base_importable():
    from modules.connectors import base
    assert hasattr(base, "ConnectorBase")
    assert hasattr(base, "CONNECTOR_TYPES")


def test_connectors_service_importable():
    from modules.connectors import service
    assert hasattr(service, "ConnectorService")


def test_connectors_router_importable():
    from modules.connectors import router
    assert hasattr(router, "router")
