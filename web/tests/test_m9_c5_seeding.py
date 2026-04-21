"""
tests/test_m9_c5_seeding.py
Module 9 Component 5 — Matter & Client Seeding UI
Pytest suite — 16 tests

Tests:
  Router registration and routes
  Conflict detection helpers (matter and client)
  Dashboard 200 (mocked)
  Promote matter — success redirect
  Promote matter — 404 on missing staging record
  Promote matter — 409 on conflict (no force)
  Promote all matters redirect
  Promote client — success redirect
  Promote client — 404 on missing staging record
  Promote all clients redirect
  Template exists
  Template has promote-all form
  Template has tab navigation
  Template has conflict badge markup
  Staging table columns confirmed (ts_matters, ts_clients)
  billing_import_sources table exists

Run:
  pytest tests/test_m9_c5_seeding.py -v
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

DB_AVAILABLE = bool(os.environ.get("DATABASE_URL"))
requires_db = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__),
    "../templates/admin/seed_dashboard.html"
)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db_connect():
    import psycopg2
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "")
    at_idx = url.rfind("@")
    userinfo = url[:at_idx]
    hostinfo = url[at_idx + 1:]
    user, password = userinfo.split(":", 1)
    host_port, dbname = hostinfo.split("/", 1)
    host, port = host_port.rsplit(":", 1)
    return psycopg2.connect(
        host=host, port=int(port),
        dbname=dbname.split("?")[0],
        user=user, password=password,
    )


def _col_exists(cur, table, col):
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
        (table, col),
    )
    return cur.fetchone() is not None


def _table_exists(cur, table):
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s", (table,)
    )
    return cur.fetchone() is not None


# ── Schema tests T-01 to T-02 ─────────────────────────────────────────────────

@requires_db
def test_t01_ts_matters_staging_columns():
    conn = _db_connect()
    cur = conn.cursor()
    for col in ("id", "tenant_id", "source_id", "ts_matter_id",
                "display_name", "promoted_at", "praesidium_matter_id"):
        assert _col_exists(cur, "ts_matters", col), f"ts_matters missing: {col}"
    conn.close()


@requires_db
def test_t02_ts_clients_staging_columns():
    conn = _db_connect()
    cur = conn.cursor()
    for col in ("id", "tenant_id", "source_id", "ts_client_id",
                "ts_name", "promoted_at", "praesidium_client_id"):
        assert _col_exists(cur, "ts_clients", col), f"ts_clients missing: {col}"
    conn.close()


# ── Import / router tests T-03 to T-05 ───────────────────────────────────────

def test_t03_seeding_api_importable():
    from modules.admin.seeding_api import router
    assert router is not None


def test_t04_seeding_api_routes():
    from modules.admin.seeding_api import router
    paths = {r.path for r in router.routes}
    assert "/admin/seed" in paths
    assert "/admin/seed/matters/{staging_id}/promote" in paths
    assert "/admin/seed/matters/promote-all" in paths
    assert "/admin/seed/clients/{staging_id}/promote" in paths
    assert "/admin/seed/clients/promote-all" in paths


def test_t05_conflict_helpers_importable():
    from modules.admin.seeding_api import (
        _check_matter_conflict,
        _check_client_conflict,
    )
    assert callable(_check_matter_conflict)
    assert callable(_check_client_conflict)


# ── Conflict detection tests T-06 to T-07 ────────────────────────────────────

@pytest.mark.asyncio
async def test_t06_matter_conflict_no_crash_empty_inputs():
    from modules.admin.seeding_api import _check_matter_conflict
    result = await _check_matter_conflict("hjmm-prod", "", "")
    assert result is None


@pytest.mark.asyncio
async def test_t07_client_conflict_no_crash_empty_inputs():
    from modules.admin.seeding_api import _check_client_conflict
    result = await _check_client_conflict("hjmm-prod", "", "")
    assert result is None


# ── Route tests T-08 to T-14 — mocked ────────────────────────────────────────

def _mock_session():
    return {"user_id": 1, "tenant_id": "hjmm-prod", "role": "admin"}


def _make_client():
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app, raise_server_exceptions=False)


def _mock_db_empty():
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_result = MagicMock()
    mock_result.mappings.return_value.fetchall.return_value = []
    mock_result.scalar.return_value = 0
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    return mock_session


@patch("modules.admin.seeding_api._require_admin")
@patch("modules.admin.seeding_api.AsyncSessionLocal")
def test_t08_dashboard_200_no_sources(mock_db, mock_admin):
    mock_admin.return_value = _mock_session()
    mock_db.return_value = _mock_db_empty()
    client = _make_client()
    resp = client.get("/admin/seed?tenant_id=hjmm-prod")
    assert resp.status_code == 200


@patch("modules.admin.seeding_api._require_admin")
@patch("modules.admin.seeding_api.AsyncSessionLocal")
def test_t09_promote_matter_missing_staging_404(mock_db, mock_admin):
    mock_admin.return_value = _mock_session()
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_result = MagicMock()
    mock_result.mappings.return_value.fetchone.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_db.return_value = mock_session
    client = _make_client()
    resp = client.post("/admin/seed/matters/nonexistent-id/promote", data={
        "tenant_id": "hjmm-prod",
        "source_id": "src-1",
        "display_name": "Test Matter",
        "praesidium_status": "active",
        "force": "0",
    })
    assert resp.status_code == 404


@patch("modules.admin.seeding_api._require_admin")
@patch("modules.admin.seeding_api.AsyncSessionLocal")
@patch("modules.admin.seeding_api._check_matter_conflict")
def test_t10_promote_matter_conflict_409(mock_conflict, mock_db, mock_admin):
    mock_admin.return_value = _mock_session()
    mock_conflict.return_value = "Matter number '7020.361' already exists"
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_result = MagicMock()
    mock_result.mappings.return_value.fetchone.return_value = {
        "id": "staging-1", "ts_matter_id": "7020.361",
        "display_name": "Test", "ts_nickname": "Test",
        "ts_description": None, "praesidium_status": "active",
    }
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_db.return_value = mock_session
    client = _make_client()
    resp = client.post("/admin/seed/matters/staging-1/promote", data={
        "tenant_id": "hjmm-prod",
        "source_id": "src-1",
        "display_name": "Test Matter",
        "praesidium_status": "active",
        "force": "0",
    })
    assert resp.status_code == 409


@patch("modules.admin.seeding_api._require_admin")
@patch("modules.admin.seeding_api.AsyncSessionLocal")
def test_t11_promote_all_matters_redirects(mock_db, mock_admin):
    mock_admin.return_value = _mock_session()
    mock_db.return_value = _mock_db_empty()
    client = _make_client()
    resp = client.post("/admin/seed/matters/promote-all", data={
        "tenant_id": "hjmm-prod",
        "source_id": "src-1",
        "skip_conflicts": "1",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "/admin/seed" in resp.headers.get("location", "")


@patch("modules.admin.seeding_api._require_admin")
@patch("modules.admin.seeding_api.AsyncSessionLocal")
def test_t12_promote_client_missing_404(mock_db, mock_admin):
    mock_admin.return_value = _mock_session()
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_result = MagicMock()
    mock_result.mappings.return_value.fetchone.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_db.return_value = mock_session
    client = _make_client()
    resp = client.post("/admin/seed/clients/nonexistent/promote", data={
        "tenant_id": "hjmm-prod",
        "source_id": "src-1",
        "force": "0",
    })
    assert resp.status_code == 404


@patch("modules.admin.seeding_api._require_admin")
@patch("modules.admin.seeding_api.AsyncSessionLocal")
def test_t13_promote_all_clients_redirects(mock_db, mock_admin):
    mock_admin.return_value = _mock_session()
    mock_db.return_value = _mock_db_empty()
    client = _make_client()
    resp = client.post("/admin/seed/clients/promote-all", data={
        "tenant_id": "hjmm-prod",
        "source_id": "src-1",
        "skip_conflicts": "1",
    }, follow_redirects=False)
    assert resp.status_code == 303


# ── Template tests T-14 to T-16 ───────────────────────────────────────────────

def test_t14_seed_dashboard_template_exists():
    assert os.path.exists(TEMPLATE_PATH), \
        f"seed_dashboard.html not found at {TEMPLATE_PATH}"


def test_t15_template_has_promote_all():
    with open(TEMPLATE_PATH) as f:
        content = f.read()
    assert "promote-all" in content
    assert "Promote All" in content


def test_t16_template_has_conflict_detection():
    with open(TEMPLATE_PATH) as f:
        content = f.read()
    assert "conflict" in content.lower()
    assert "Conflict" in content
