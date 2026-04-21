"""
tests/test_m9_c4_crawl.py
Module 9 Component 4 — File Crawl Trigger
Pytest suite — 18 tests

Tests:
  Schema (cifs_crawl_jobs and cifs_crawl_entries tables, constraints, indexes)
  Router registration and route inventory
  crawl_api imports and helpers
  Dashboard endpoint (mocked)
  Trigger endpoint validation
  Cancel endpoint
  Browse endpoint (mocked CIFS)
  crawl_job module importable
  _fmt_size helper

Run:
  pytest tests/test_m9_c4_crawl.py -v
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

DB_AVAILABLE = bool(os.environ.get("DATABASE_URL"))
requires_db = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")


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


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s", (table,)
    )
    return cur.fetchone() is not None


def _col_exists(cur, table: str, col: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
        (table, col),
    )
    return cur.fetchone() is not None


def _index_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname=%s", (name,)
    )
    return cur.fetchone() is not None


def _constraint_exists(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_constraint WHERE conname=%s", (name,))
    return cur.fetchone() is not None


# ── Schema tests T-01 to T-08 ─────────────────────────────────────────────────

@requires_db
def test_t01_cifs_crawl_jobs_table_exists():
    conn = _db_connect()
    cur = conn.cursor()
    assert _table_exists(cur, "cifs_crawl_jobs"), \
        "cifs_crawl_jobs table missing — run 0014_m9_crawl_jobs migration"
    conn.close()


@requires_db
def test_t02_cifs_crawl_entries_table_exists():
    conn = _db_connect()
    cur = conn.cursor()
    assert _table_exists(cur, "cifs_crawl_entries"), \
        "cifs_crawl_entries table missing"
    conn.close()


@requires_db
def test_t03_crawl_jobs_required_columns():
    conn = _db_connect()
    cur = conn.cursor()
    for col in ("id", "tenant_id", "mount", "status", "queued_at",
                "files_discovered", "files_indexed", "rq_job_id"):
        assert _col_exists(cur, "cifs_crawl_jobs", col), \
            f"cifs_crawl_jobs missing column: {col}"
    conn.close()


@requires_db
def test_t04_crawl_entries_required_columns():
    conn = _db_connect()
    cur = conn.cursor()
    for col in ("id", "tenant_id", "job_id", "mount", "file_path",
                "file_name", "file_size_bytes", "mime_type", "is_directory"):
        assert _col_exists(cur, "cifs_crawl_entries", col), \
            f"cifs_crawl_entries missing column: {col}"
    conn.close()


@requires_db
def test_t05_crawl_jobs_status_constraint():
    conn = _db_connect()
    cur = conn.cursor()
    assert _constraint_exists(cur, "ck_cifs_crawl_jobs_status"), \
        "ck_cifs_crawl_jobs_status CHECK constraint missing"
    conn.close()


@requires_db
def test_t06_crawl_jobs_mount_constraint():
    conn = _db_connect()
    cur = conn.cursor()
    assert _constraint_exists(cur, "ck_cifs_crawl_jobs_mount"), \
        "ck_cifs_crawl_jobs_mount CHECK constraint missing"
    conn.close()


@requires_db
def test_t07_crawl_entries_job_path_unique():
    conn = _db_connect()
    cur = conn.cursor()
    assert _constraint_exists(cur, "uq_cifs_crawl_entries_job_path"), \
        "uq_cifs_crawl_entries_job_path UNIQUE constraint missing"
    conn.close()


@requires_db
def test_t08_crawl_jobs_indexes_exist():
    conn = _db_connect()
    cur = conn.cursor()
    for idx in ("ix_cifs_crawl_jobs_tenant_id", "ix_cifs_crawl_jobs_status",
                "ix_cifs_crawl_entries_job_id"):
        assert _index_exists(cur, idx), f"Missing index: {idx}"
    conn.close()


# ── Import / router tests T-09 to T-12 ───────────────────────────────────────

def test_t09_crawl_api_importable():
    from modules.admin.crawl_api import router
    assert router is not None


def test_t10_crawl_api_has_required_routes():
    from modules.admin.crawl_api import router
    paths = {r.path for r in router.routes}
    assert "/admin/crawl" in paths
    assert "/admin/crawl/trigger" in paths
    assert "/admin/crawl/{job_id}" in paths
    assert "/admin/crawl/{job_id}/cancel" in paths
    assert "/admin/crawl/browse" in paths


def test_t11_crawl_job_importable():
    from jobs.crawl_job import run
    assert callable(run)


def test_t12_fmt_size_helper():
    from modules.admin.crawl_api import _fmt_size
    assert _fmt_size(0) == "0 B"
    assert "KB" in _fmt_size(2048)
    assert "MB" in _fmt_size(2 * 1024 * 1024)
    assert "GB" in _fmt_size(2 * 1024 * 1024 * 1024)


# ── Route tests T-13 to T-18 — mocked ────────────────────────────────────────

def _mock_session():
    return {"user_id": 1, "tenant_id": "hjmm-prod", "role": "admin"}


def _make_client():
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app, raise_server_exceptions=False)


@patch("modules.admin.crawl_api._require_admin")
@patch("modules.admin.crawl_api._cifs_health")
@patch("modules.admin.crawl_api.AsyncSessionLocal")
def test_t13_crawl_dashboard_200(mock_db, mock_health, mock_admin):
    mock_admin.return_value = _mock_session()
    mock_health.return_value = {"status": "healthy"}
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_result = MagicMock()
    mock_result.mappings.return_value.fetchall.return_value = []
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_db.return_value = mock_session
    client = _make_client()
    resp = client.get("/admin/crawl?tenant_id=hjmm-prod")
    assert resp.status_code == 200
    assert "Crawl" in resp.text


@patch("modules.admin.crawl_api._require_admin")
def test_t14_trigger_invalid_mount_rejected(mock_admin):
    mock_admin.return_value = _mock_session()
    client = _make_client()
    resp = client.post("/admin/crawl/trigger", data={
        "tenant_id": "hjmm-prod",
        "mount": "invalid_mount",
        "root_path": "",
        "depth_limit": 10,
    })
    assert resp.status_code == 400


@patch("modules.admin.crawl_api._require_admin")
@patch("modules.admin.crawl_api.AsyncSessionLocal")
def test_t15_trigger_valid_mount_redirects(mock_db, mock_admin):
    mock_admin.return_value = _mock_session()
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_db.return_value = mock_session
    client = _make_client()
    with patch("rq.Queue") as mock_queue:
        mock_queue.return_value.enqueue = MagicMock()
        resp = client.post("/admin/crawl/trigger", data={
            "tenant_id": "hjmm-prod",
            "mount": "clients",
            "root_path": "",
            "depth_limit": 5,
        }, follow_redirects=False)
    assert resp.status_code == 303
    assert "/admin/crawl" in resp.headers.get("location", "")


@patch("modules.admin.crawl_api._require_admin")
@patch("modules.admin.crawl_api.AsyncSessionLocal")
def test_t16_cancel_not_found_returns_404(mock_db, mock_admin):
    mock_admin.return_value = _mock_session()
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_result = MagicMock()
    mock_result.fetchone.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_db.return_value = mock_session
    client = _make_client()
    resp = client.post(
        "/admin/crawl/nonexistent-job-id/cancel",
        data={"tenant_id": "hjmm-prod"},
    )
    assert resp.status_code == 404


@patch("modules.admin.crawl_api._require_admin")
@patch("modules.admin.crawl_api._cifs_list")
def test_t17_browse_invalid_mount_returns_error(mock_list, mock_admin):
    mock_admin.return_value = _mock_session()
    client = _make_client()
    resp = client.get("/admin/crawl/browse?mount=evil&tenant_id=hjmm-prod")
    assert resp.status_code == 200
    assert "Invalid" in resp.text


@patch("modules.admin.crawl_api._require_admin")
@patch("modules.admin.crawl_api._cifs_list")
def test_t18_browse_valid_mount_returns_html(mock_list, mock_admin):
    mock_admin.return_value = _mock_session()
    mock_list.return_value = [
        {"name": "hjmm", "is_directory": True, "size": 0},
        {"name": "report.pdf", "is_directory": False, "size": 204800},
    ]
    client = _make_client()
    resp = client.get("/admin/crawl/browse?mount=clients&tenant_id=hjmm-prod")
    assert resp.status_code == 200
    assert "hjmm" in resp.text
    assert "report.pdf" in resp.text
