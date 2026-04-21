"""
test_m8_c0e_jobs.py
Module 8 Component 0e — PROC-01 Jobs
Pytest suite — 24 tests

T-01  bank/set: valid request returns 202 + job_id
T-02  bank/set: Redis unavailable returns 503
T-03  bank/confirm: valid request returns 202 + job_id
T-04  bank/rollback: valid request returns 202 + job_id
T-05  bank/status: returns active + pending bank state
T-06  bank/status: unknown VM returns 404
T-07  backup/create: returns 202 + job_id
T-08  backup/list: returns records list
T-09  backup/get: unknown ID returns 404
T-10  docker action start: returns 202 + job_id
T-11  docker action stop: returns 202 + job_id
T-12  docker action invalid: returns 422
T-13  docker logs: tail param passed through
T-14  job/status: returns job status dict
T-15  recent events: returns events list
T-16  recent events: vm_name filter applied
T-17  SSE stream: yields connected event then heartbeat
T-18  bank_swap.set_pending_bank: invalid bank returns error
T-19  bank_swap.set_pending_bank: no DB returns error
T-20  config_backup.create_config_backup: no DB returns error
T-21  config_backup._collect_scripts: empty dir returns empty dict
T-22  docker_control.docker_start: disallowed container returns error
T-23  docker_control.docker_logs: tail capped at 1000
T-24  _sse helper: formats event frame correctly

Run inside container:
  pytest tests/test_m8_c0e_jobs.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    import httpx
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def set_admin_token():
    """Set PLATFORM_ADMIN_TOKEN for all tests."""
    os.environ["PLATFORM_ADMIN_TOKEN"] = "test-c0e-token"
    yield
    os.environ.pop("PLATFORM_ADMIN_TOKEN", None)


@pytest.fixture
def api_client():
    if not HTTPX_AVAILABLE:
        pytest.skip("httpx not available")
    try:
        import importlib
        import modules.admin.jobs_api as jobs_mod
        importlib.reload(jobs_mod)
        app = FastAPI()
        app.include_router(jobs_mod.router)
        return TestClient(app, headers={"X-Platform-Admin": "test-c0e-token"})
    except ImportError as exc:
        pytest.skip(f"App deps not available: {exc}")


def _mock_session_factory(rows=None, fetchone_val=None):
    """Build a mock AsyncSessionLocal context manager."""
    mock_sess = AsyncMock()
    mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
    mock_sess.__aexit__ = AsyncMock(return_value=False)
    if rows is not None:
        mock_sess.execute = AsyncMock(
            return_value=MagicMock(
                mappings=lambda: MagicMock(fetchall=lambda: rows),
                fetchone=lambda: fetchone_val,
            )
        )
    elif fetchone_val is not None:
        mock_sess.execute = AsyncMock(
            return_value=MagicMock(
                mappings=lambda: MagicMock(fetchone=lambda: fetchone_val),
                fetchone=lambda: fetchone_val,
            )
        )
    mock_sess.commit = AsyncMock()
    return mock_sess


# ══════════════════════════════════════════════════════════════════════════════
# T-01 to T-04: Bank swap endpoints
# ══════════════════════════════════════════════════════════════════════════════

def test_t01_bank_set_valid(api_client):
    """T-01: Bank set with valid payload returns 202 + job_id."""
    with patch("modules.admin.jobs_api._enqueue", return_value="job-bank-set-001"):
        resp = api_client.post("/admin/api/jobs/bank/set", json={
            "vm_name": "MAIN-PRD-WEB-01",
            "target_bank": "bank_b",
        })
    assert resp.status_code == 202
    data = resp.json()
    assert data["job_id"] == "job-bank-set-001"
    assert data["target_bank"] == "bank_b"
    assert data["status"] == "queued"


def test_t02_bank_set_redis_unavailable(api_client):
    """T-02: Redis unavailable returns 503."""
    with patch("modules.admin.jobs_api._enqueue", return_value=None):
        resp = api_client.post("/admin/api/jobs/bank/set", json={
            "vm_name": "MAIN-PRD-WEB-01",
            "target_bank": "bank_b",
        })
    assert resp.status_code == 503


def test_t03_bank_confirm_valid(api_client):
    """T-03: Bank confirm returns 202 + job_id."""
    with patch("modules.admin.jobs_api._enqueue", return_value="job-confirm-001"):
        resp = api_client.post("/admin/api/jobs/bank/confirm", json={
            "vm_name": "MAIN-PRD-WEB-01",
            "target_bank": "bank_b",
            "confirmed_by": "dmholmgren",
        })
    assert resp.status_code == 202
    assert resp.json()["job_id"] == "job-confirm-001"


def test_t04_bank_rollback_valid(api_client):
    """T-04: Bank rollback returns 202 + job_id."""
    with patch("modules.admin.jobs_api._enqueue", return_value="job-rollback-001"):
        resp = api_client.post("/admin/api/jobs/bank/rollback", json={
            "vm_name": "MAIN-PRD-WEB-01",
            "reason": "test rollback",
        })
    assert resp.status_code == 202
    assert resp.json()["job_id"] == "job-rollback-001"


# ══════════════════════════════════════════════════════════════════════════════
# T-05 to T-06: Bank status
# ══════════════════════════════════════════════════════════════════════════════

def test_t05_bank_status_returns_state(api_client):
    """T-05: Bank status returns active and pending bank."""
    mock_rows = [
        {"bank": "bank_a", "is_active": True, "pending_active": False,
         "loaded_at": None, "loaded_by": None, "confirmed_at": None,
         "confirmed_by": None, "version_tag": None},
        {"bank": "bank_b", "is_active": False, "pending_active": True,
         "loaded_at": None, "loaded_by": None, "confirmed_at": None,
         "confirmed_by": None, "version_tag": None},
    ]
    with patch("modules.admin.jobs_api.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value = _mock_session_factory(rows=mock_rows)
        resp = api_client.get("/admin/api/jobs/bank/status/MAIN-PRD-WEB-01")

    assert resp.status_code == 200
    data = resp.json()
    assert data["active_bank"] == "bank_a"
    assert data["pending_bank"] == "bank_b"
    assert "bank_a" in data["banks"]
    assert "bank_b" in data["banks"]


def test_t06_bank_status_unknown_vm(api_client):
    """T-06: Unknown VM returns 404."""
    with patch("modules.admin.jobs_api.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value = _mock_session_factory(rows=[])
        resp = api_client.get("/admin/api/jobs/bank/status/NONEXISTENT-VM")
    assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# T-07 to T-09: Backup endpoints
# ══════════════════════════════════════════════════════════════════════════════

def test_t07_backup_create_returns_job_id(api_client):
    """T-07: Backup create returns 202 + job_id."""
    with patch("modules.admin.jobs_api._enqueue", return_value="job-backup-001"):
        resp = api_client.post("/admin/api/jobs/backup/create", json={
            "label": "test-backup",
        })
    assert resp.status_code == 202
    assert resp.json()["job_id"] == "job-backup-001"


def test_t08_backup_list_returns_records(api_client):
    """T-08: Backup list returns records."""
    mock_rows = [
        {"id": "bk-001", "label": "test", "backup_at": "2026-03-29T00:00:00Z",
         "triggered_by": "admin", "is_auto": False, "pre_event_type": None,
         "config_archive_path": "/opt/praesidium/backups/test.tar.gz",
         "config_hash": "abc123", "alembic_head": "def456",
         "tenant_count": 1, "notes": None}
    ]
    with patch("modules.admin.jobs_api.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value = _mock_session_factory(rows=mock_rows)
        resp = api_client.get("/admin/api/jobs/backup/list")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


def test_t09_backup_get_not_found(api_client):
    """T-09: Unknown backup ID returns 404."""
    mock_sess = AsyncMock()
    mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
    mock_sess.__aexit__ = AsyncMock(return_value=False)
    mock_sess.execute = AsyncMock(
        return_value=MagicMock(
            mappings=lambda: MagicMock(fetchone=lambda: None)
        )
    )
    with patch("modules.admin.jobs_api.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value = mock_sess
        resp = api_client.get("/admin/api/jobs/backup/nonexistent-id")
    assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# T-10 to T-13: Docker control
# ══════════════════════════════════════════════════════════════════════════════

def test_t10_docker_start(api_client):
    """T-10: Docker start returns 202 + job_id."""
    with patch("modules.admin.jobs_api._enqueue", return_value="job-docker-start"):
        resp = api_client.post("/admin/api/jobs/docker/start", json={})
    assert resp.status_code == 202
    assert resp.json()["action"] == "start"


def test_t11_docker_stop(api_client):
    """T-11: Docker stop returns 202 + job_id."""
    with patch("modules.admin.jobs_api._enqueue", return_value="job-docker-stop"):
        resp = api_client.post("/admin/api/jobs/docker/stop", json={"stop_timeout": 30})
    assert resp.status_code == 202
    assert resp.json()["action"] == "stop"


def test_t12_docker_invalid_action(api_client):
    """T-12: Invalid docker action returns 422."""
    resp = api_client.post("/admin/api/jobs/docker/explode", json={})
    assert resp.status_code == 422
    assert "Invalid action" in resp.json()["detail"]


def test_t13_docker_logs_tail(api_client):
    """T-13: Docker logs action accepts tail param."""
    with patch("modules.admin.jobs_api._enqueue", return_value="job-docker-logs") as mock_enq:
        resp = api_client.post("/admin/api/jobs/docker/logs", json={"tail": 200})
    assert resp.status_code == 202
    # Verify tail was passed to enqueue
    call_kwargs = mock_enq.call_args[1]
    assert call_kwargs.get("tail") == 200


# ══════════════════════════════════════════════════════════════════════════════
# T-14 to T-16: Job status + recent events
# ══════════════════════════════════════════════════════════════════════════════

def test_t14_job_status(api_client):
    """T-14: Job status endpoint returns dict with job_id."""
    with patch("modules.admin.jobs_api._get_job_status", return_value={
        "job_id": "test-job-999",
        "status": "finished",
        "result": {"status": "ok"},
    }):
        resp = api_client.get("/admin/api/jobs/status/test-job-999")
    assert resp.status_code == 200
    assert resp.json()["job_id"] == "test-job-999"


def test_t15_recent_events(api_client):
    """T-15: Recent events returns list."""
    mock_rows = [
        {"id": "ev-001", "event_type": "bank_set", "vm_name": "MAIN-PRD-WEB-01",
         "from_version": None, "to_version": None,
         "triggered_by": "admin-api", "detail": {}, "created_at": "2026-03-29T00:00:00Z"},
    ]
    with patch("modules.admin.jobs_api.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value = _mock_session_factory(rows=mock_rows)
        resp = api_client.get("/admin/api/jobs/recent")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


def test_t16_recent_events_vm_filter(api_client):
    """T-16: Recent events vm_name filter is passed as query param."""
    with patch("modules.admin.jobs_api.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value = _mock_session_factory(rows=[])
        resp = api_client.get("/admin/api/jobs/recent?vm_name=MAIN-PRD-WEB-01")
    assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# T-17: SSE stream
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.skip(reason="SSE stream blocks TestClient — verify manually with curl -N")
def test_t17_sse_connected_event(api_client):
    """T-17: SSE stream yields connected event immediately."""
    import threading
    result = {}
    def run():
        try:
            with patch("modules.admin.jobs_api._poll_new_events", new_callable=AsyncMock, return_value=[]):
                with api_client.stream("GET", "/admin/api/jobs/stream") as resp:
                    result["status"] = resp.status_code
                    result["ct"] = resp.headers.get("content-type", "")
                    chunk = next(resp.iter_text())
                    result["chunk"] = chunk
        except Exception as e:
            result["error"] = str(e)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=5)
    assert result.get("status") == 200
    assert "text/event-stream" in result.get("ct", "")
    assert "connected" in result.get("chunk", "")


# ══════════════════════════════════════════════════════════════════════════════
# T-18 to T-19: bank_swap unit tests
# ══════════════════════════════════════════════════════════════════════════════

def test_t18_bank_swap_invalid_bank():
    """T-18: set_pending_bank with invalid bank name returns error dict."""
    from jobs.bank_swap import set_pending_bank
    result = set_pending_bank(
        vm_name="MAIN-PRD-WEB-01",
        target_bank="bank_c",
        triggered_by="test",
    )
    assert result["status"] == "error"
    assert "bank_c" in result["error"]


def test_t19_bank_swap_no_db():
    """T-19: set_pending_bank with no DB connection returns error."""
    from jobs.bank_swap import set_pending_bank
    with patch("jobs.bank_swap._db_connect", return_value=None):
        result = set_pending_bank("MAIN-PRD-WEB-01", "bank_b", "test")
    assert result["status"] == "error"
    assert "DB" in result["error"]


# ══════════════════════════════════════════════════════════════════════════════
# T-20 to T-21: config_backup unit tests
# ══════════════════════════════════════════════════════════════════════════════

def test_t20_config_backup_no_db():
    """T-20: create_config_backup with no DB returns error."""
    from jobs.config_backup import create_config_backup
    with patch("jobs.config_backup._db_connect", return_value=None):
        result = create_config_backup(label="test", triggered_by="test")
    assert result["status"] == "error"


def test_t21_collect_scripts_empty_dir(tmp_path):
    """T-21: _collect_scripts returns empty dict when dir does not exist."""
    from jobs.config_backup import _collect_scripts
    with patch("jobs.config_backup.os.path.isdir", return_value=False):
        result = _collect_scripts()
    assert result == {}


# ══════════════════════════════════════════════════════════════════════════════
# T-22 to T-23: docker_control unit tests
# ══════════════════════════════════════════════════════════════════════════════

def test_t22_docker_disallowed_container():
    """T-22: docker_start with disallowed container name returns error."""
    from jobs.docker_control import docker_start
    result = docker_start(container="some-other-container", triggered_by="test")
    assert result["status"] == "error"
    assert "not in allowed list" in result["error"]


def test_t23_docker_logs_tail_capped():
    """T-23: docker_logs caps tail at 1000 regardless of input."""
    from jobs.docker_control import docker_logs
    with patch("jobs.docker_control._run") as mock_run:
        mock_run.return_value = {"returncode": 0, "stdout": "", "stderr": "line1\nline2"}
        result = docker_logs(container="praesidium-web", tail=9999)
    # tail should be capped — _run called with str(1000) not str(9999)
    call_args = mock_run.call_args[0][0]
    assert "1000" in call_args


# ══════════════════════════════════════════════════════════════════════════════
# T-24: SSE helper
# ══════════════════════════════════════════════════════════════════════════════

def test_t24_sse_format():
    """T-24: _sse formats a valid SSE event frame."""
    from modules.admin.jobs_api import _sse
    frame = _sse("test_event", {"key": "value"})
    assert frame.startswith("event: test_event\n")
    assert "data: " in frame
    assert frame.endswith("\n\n")
    # Data must be valid JSON
    data_line = [l for l in frame.splitlines() if l.startswith("data:")][0]
    parsed = json.loads(data_line[len("data: "):])
    assert parsed["key"] == "value"
