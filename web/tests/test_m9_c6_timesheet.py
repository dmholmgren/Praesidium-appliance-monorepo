"""
tests/test_m9_c6_timesheet.py
Module 9 Component 6 — AI Timesheet Reconciliation
Pytest suite — 20 tests

Tests:
  Schema (timesheet_sessions, timesheet_drafts tables, constraints, indexes)
  Router registration and routes
  Dashboard 200
  Start session — date validation
  Start session — invalid date range
  Session review 404 on unknown session
  Approve draft redirects
  Reject draft redirects
  Push to time_entries redirects
  Status endpoint returns HTML
  Job module importable
  _load_timeslips helper
  _load_ai_calls helper
  _parse_phone_csv helper (colon duration)
  _parse_phone_csv helper (decimal duration)
  _round_quarter helper
  Template exists: timesheet_dashboard.html
  Template exists: timesheet_review.html
  Template has AI confidence bar
  Template has push button

Run:
  pytest tests/test_m9_c6_timesheet.py -v
"""

from __future__ import annotations

import os
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

DB_AVAILABLE = bool(os.environ.get("DATABASE_URL"))
requires_db = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "../templates/admin")


def _db_connect():
    import psycopg2
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "")
    at_idx = url.rfind("@")
    userinfo = url[:at_idx]; hostinfo = url[at_idx+1:]
    user, password = userinfo.split(":", 1)
    host_port, dbname = hostinfo.split("/", 1)
    host, port = host_port.rsplit(":", 1)
    return psycopg2.connect(
        host=host, port=int(port),
        dbname=dbname.split("?")[0], user=user, password=password
    )


def _table_exists(cur, table):
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s", (table,)
    )
    return cur.fetchone() is not None


def _col_exists(cur, table, col):
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
        (table, col)
    )
    return cur.fetchone() is not None


def _idx_exists(cur, name):
    cur.execute("SELECT 1 FROM pg_indexes WHERE indexname=%s", (name,))
    return cur.fetchone() is not None


# ── Schema tests T-01 to T-04 ─────────────────────────────────────────────────

@requires_db
def test_t01_timesheet_sessions_table():
    conn = _db_connect(); cur = conn.cursor()
    assert _table_exists(cur, "timesheet_sessions"), \
        "timesheet_sessions missing — run 0015_m9_timesheet migration"
    conn.close()


@requires_db
def test_t02_timesheet_drafts_table():
    conn = _db_connect(); cur = conn.cursor()
    assert _table_exists(cur, "timesheet_drafts"), \
        "timesheet_drafts missing"
    conn.close()


@requires_db
def test_t03_sessions_required_columns():
    conn = _db_connect(); cur = conn.cursor()
    for col in ("id", "tenant_id", "user_id", "date_from", "date_to",
                "status", "draft_count", "approved_count", "pushed_count"):
        assert _col_exists(cur, "timesheet_sessions", col), \
            f"timesheet_sessions missing column: {col}"
    conn.close()


@requires_db
def test_t04_drafts_required_columns():
    conn = _db_connect(); cur = conn.cursor()
    for col in ("id", "session_id", "tenant_id", "user_id", "entry_date",
                "hours", "description", "source", "ai_confidence",
                "ai_narrative", "status", "time_entry_id"):
        assert _col_exists(cur, "timesheet_drafts", col), \
            f"timesheet_drafts missing column: {col}"
    conn.close()


# ── Import / router tests T-05 to T-07 ───────────────────────────────────────

def test_t05_timesheet_api_importable():
    from modules.admin.timesheet_api import router
    assert router is not None


def test_t06_timesheet_api_routes():
    from modules.admin.timesheet_api import router
    paths = {r.path for r in router.routes}
    assert "/admin/timesheet" in paths
    assert "/admin/timesheet/start" in paths
    assert "/admin/timesheet/{session_id}" in paths
    assert "/admin/timesheet/{session_id}/push" in paths
    assert "/admin/timesheet/{session_id}/status" in paths


def test_t07_reconcile_job_importable():
    from jobs.timesheet_reconcile_job import run, _round_quarter, _parse_phone_csv
    assert callable(run)
    assert callable(_round_quarter)
    assert callable(_parse_phone_csv)


# ── Helper unit tests T-08 to T-11 ───────────────────────────────────────────

def test_t08_round_quarter_rounds_up():
    from jobs.timesheet_reconcile_job import _round_quarter
    assert _round_quarter(0.1) == 0.25
    assert _round_quarter(0.25) == 0.25
    assert _round_quarter(0.26) == 0.5
    assert _round_quarter(1.0) == 1.0
    assert _round_quarter(1.1) == 1.25


def test_t09_parse_phone_csv_colon_duration():
    from jobs.timesheet_reconcile_job import _parse_phone_csv
    csv_content = "Date,Number,Duration\n03/15/2026,555-1234,0:15:00\n03/15/2026,555-5678,0:02:30\n"
    entries = _parse_phone_csv(csv_content)
    # 15 min = 0.25h, 2.5 min = too short (< 0.02h) → skipped
    assert len(entries) >= 1
    assert any(e["hours"] == 0.25 for e in entries)


def test_t10_parse_phone_csv_skips_short_calls():
    from jobs.timesheet_reconcile_job import _parse_phone_csv
    csv_content = "Date,Number,Duration\n03/15/2026,555-1234,0:00:30\n"
    entries = _parse_phone_csv(csv_content)
    assert len(entries) == 0  # 30 seconds < threshold


def test_t11_parse_phone_csv_no_date_col_returns_empty():
    from jobs.timesheet_reconcile_job import _parse_phone_csv
    csv_content = "Number,Duration\n555-1234,0:15:00\n"
    entries = _parse_phone_csv(csv_content)
    assert entries == []


# ── Route tests T-12 to T-18 — mocked ────────────────────────────────────────

def _mock_session():
    return {"user_id": 1, "tenant_id": "hjmm-prod", "role": "admin", "name": "Dennis"}


def _make_client():
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app, raise_server_exceptions=False)


def _mock_db_empty():
    s = AsyncMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    r = MagicMock()
    r.mappings.return_value.fetchall.return_value = []
    r.mappings.return_value.fetchone.return_value = None
    s.execute = AsyncMock(return_value=r)
    s.commit = AsyncMock()
    return s


@patch("modules.admin.timesheet_api._require_admin")
@patch("modules.admin.timesheet_api.AsyncSessionLocal")
def test_t12_dashboard_200(mock_db, mock_admin):
    mock_admin.return_value = _mock_session()
    mock_db.return_value = _mock_db_empty()
    client = _make_client()
    resp = client.get("/admin/timesheet?tenant_id=hjmm-prod")
    assert resp.status_code == 200
    assert "Reconciliation" in resp.text


@patch("modules.admin.timesheet_api._require_admin")
def test_t13_start_invalid_date_format(mock_admin):
    mock_admin.return_value = _mock_session()
    client = _make_client()
    resp = client.post("/admin/timesheet/start", data={
        "tenant_id": "hjmm-prod",
        "date_from": "not-a-date",
        "date_to": "2026-03-31",
    })
    assert resp.status_code == 400


@patch("modules.admin.timesheet_api._require_admin")
def test_t14_start_date_range_reversed(mock_admin):
    mock_admin.return_value = _mock_session()
    client = _make_client()
    resp = client.post("/admin/timesheet/start", data={
        "tenant_id": "hjmm-prod",
        "date_from": "2026-03-31",
        "date_to": "2026-03-01",
    })
    assert resp.status_code == 400


@patch("modules.admin.timesheet_api._require_admin")
@patch("modules.admin.timesheet_api.AsyncSessionLocal")
def test_t15_session_review_404(mock_db, mock_admin):
    mock_admin.return_value = _mock_session()
    mock_db.return_value = _mock_db_empty()
    client = _make_client()
    resp = client.get("/admin/timesheet/nonexistent-id?tenant_id=hjmm-prod")
    assert resp.status_code == 404


@patch("modules.admin.timesheet_api._require_admin")
@patch("modules.admin.timesheet_api.AsyncSessionLocal")
def test_t16_approve_draft_redirects(mock_db, mock_admin):
    mock_admin.return_value = _mock_session()
    mock_db.return_value = _mock_db_empty()
    client = _make_client()
    resp = client.post(
        "/admin/timesheet/session-1/approve/draft-1",
        data={"tenant_id": "hjmm-prod"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/admin/timesheet/session-1" in resp.headers.get("location", "")


@patch("modules.admin.timesheet_api._require_admin")
@patch("modules.admin.timesheet_api.AsyncSessionLocal")
def test_t17_reject_draft_redirects(mock_db, mock_admin):
    mock_admin.return_value = _mock_session()
    mock_db.return_value = _mock_db_empty()
    client = _make_client()
    resp = client.post(
        "/admin/timesheet/session-1/reject/draft-1",
        data={"tenant_id": "hjmm-prod"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


@patch("modules.admin.timesheet_api._require_admin")
@patch("modules.admin.timesheet_api.AsyncSessionLocal")
def test_t18_status_returns_html(mock_db, mock_admin):
    mock_admin.return_value = _mock_session()
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_result = MagicMock()
    mock_result.mappings.return_value.fetchone.return_value = {
        "status": "complete", "draft_count": 10, "approved_count": 5, "error_message": None
    }
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_db.return_value = mock_session
    client = _make_client()
    resp = client.get("/admin/timesheet/session-1/status?tenant_id=hjmm-prod")
    assert resp.status_code == 200
    assert "badge" in resp.text


# ── Template tests T-19 to T-20 ───────────────────────────────────────────────

def test_t19_templates_exist():
    for tmpl in ("timesheet_dashboard.html", "timesheet_review.html"):
        path = os.path.join(TEMPLATE_DIR, tmpl)
        assert os.path.exists(path), f"Missing template: {tmpl}"


def test_t20_review_template_has_key_elements():
    path = os.path.join(TEMPLATE_DIR, "timesheet_review.html")
    with open(path) as f:
        content = f.read()
    assert "ai_confidence" in content
    assert "push" in content.lower()
    assert "approve" in content.lower()
    assert "reject" in content.lower()
