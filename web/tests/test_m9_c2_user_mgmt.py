"""
tests/test_m9_c2_user_mgmt.py
Module 9 Component 2 — User Management UI
Pytest suite — 20 tests

Tests:
  Schema (columns, constraints, indexes on users table)
  Router registration and route inventory
  Validation errors (password length, mismatch, bad email)
  HTMX theme toggle response
  Deactivate / reactivate redirects
  404 on unknown user

Run:
  pytest tests/test_m9_c2_user_mgmt.py -v
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

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


def _col_exists(cur, table: str, col: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
        (table, col),
    )
    return cur.fetchone() is not None


def _index_exists(cur, index_name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname=%s",
        (index_name,),
    )
    return cur.fetchone() is not None


def _constraint_exists(cur, constraint_name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM pg_constraint WHERE conname=%s",
        (constraint_name,),
    )
    return cur.fetchone() is not None


# ── Schema tests T-01 to T-08 ─────────────────────────────────────────────────

@requires_db
def test_t01_theme_preference_column():
    conn = _db_connect()
    cur = conn.cursor()
    assert _col_exists(cur, "users", "theme_preference"), \
        "users.theme_preference missing — run 0013_m9_user_mgmt migration"
    conn.close()


@requires_db
def test_t02_last_active_at_column():
    conn = _db_connect()
    cur = conn.cursor()
    assert _col_exists(cur, "users", "last_active_at"), \
        "users.last_active_at missing"
    conn.close()


@requires_db
def test_t03_user_preferences_column():
    conn = _db_connect()
    cur = conn.cursor()
    assert _col_exists(cur, "users", "user_preferences"), \
        "users.user_preferences missing"
    conn.close()


@requires_db
def test_t04_theme_check_constraint():
    conn = _db_connect()
    cur = conn.cursor()
    assert _constraint_exists(cur, "ck_users_theme_preference"), \
        "ck_users_theme_preference CHECK constraint missing"
    conn.close()


@requires_db
def test_t05_tenant_is_active_index():
    conn = _db_connect()
    cur = conn.cursor()
    assert _index_exists(cur, "ix_users_tenant_is_active"), \
        "ix_users_tenant_is_active index missing"
    conn.close()


@requires_db
def test_t06_last_active_at_index():
    conn = _db_connect()
    cur = conn.cursor()
    assert _index_exists(cur, "ix_users_last_active_at"), \
        "ix_users_last_active_at index missing"
    conn.close()


@requires_db
def test_t07_theme_default_is_dark():
    conn = _db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='users' "
        "AND column_name='theme_preference'"
    )
    row = cur.fetchone()
    conn.close()
    assert row is not None
    assert "dark" in (row[0] or ""), f"Expected default 'dark', got: {row[0]}"


@requires_db
def test_t08_theme_constraint_rejects_invalid():
    """INSERT with invalid theme_preference value must raise constraint violation."""
    conn = _db_connect()
    conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users "
            "(tenant_id, username, email, full_name, role, is_active, theme_preference, password_hash) "
            "VALUES (%s, %s, %s, %s, 'staff', true, 'rainbow', 'x')",
            (
                "test-constraint-tenant",
                f"theme_test_{uuid.uuid4().hex[:6]}",
                f"theme_{uuid.uuid4().hex[:6]}@test.invalid",
                "Test User",
            ),
        )
        conn.rollback()
        pytest.fail("Expected constraint violation — INSERT should have failed")
    except Exception as e:
        conn.rollback()
        assert "ck_users_theme_preference" in str(e) or "check" in str(e).lower(), \
            f"Wrong exception type: {e}"
    finally:
        conn.close()


# ── Import / router tests T-09 to T-11 ───────────────────────────────────────

def test_t09_router_importable():
    from modules.admin.user_mgmt_api import router
    assert router is not None


def test_t10_router_has_all_routes():
    from modules.admin.user_mgmt_api import router
    paths = {r.path for r in router.routes}
    assert "/admin/users" in paths
    assert "/admin/users/new" in paths
    assert "/admin/users/{user_id}" in paths
    assert "/admin/users/{user_id}/deactivate" in paths
    assert "/admin/users/{user_id}/reactivate" in paths
    assert "/admin/users/{user_id}/theme" in paths


def test_t11_assignable_roles_complete():
    from modules.admin.user_mgmt_api import ASSIGNABLE_ROLES
    for role in ("attorney", "paralegal", "staff", "admin", "read_only"):
        assert role in ASSIGNABLE_ROLES, f"Missing role: {role}"


# ── Route tests T-12 to T-20 — mocked session ────────────────────────────────

def _make_client():
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app, raise_server_exceptions=False)


def _mock_session(tenant_id: str = "hjmm-prod") -> dict:
    return {"user_id": 1, "tenant_id": tenant_id, "role": "admin"}


@patch("modules.admin.user_mgmt_api._require_admin")
@patch("modules.admin.user_mgmt_api._get_users")
def test_t12_user_list_200(mock_get_users, mock_require_admin):
    mock_require_admin.return_value = _mock_session()
    mock_get_users.return_value = []
    client = _make_client()
    resp = client.get("/admin/users?tenant_id=hjmm-prod")
    assert resp.status_code == 200
    assert "User Management" in resp.text


@patch("modules.admin.user_mgmt_api._require_admin")
def test_t13_new_user_form_200(mock_require_admin):
    mock_require_admin.return_value = _mock_session()
    client = _make_client()
    resp = client.get("/admin/users/new?tenant_id=hjmm-prod")
    assert resp.status_code == 200
    assert "New User" in resp.text


@patch("modules.admin.user_mgmt_api._require_admin")
def test_t14_create_short_password_shows_error(mock_require_admin):
    mock_require_admin.return_value = _mock_session()
    client = _make_client()
    resp = client.post("/admin/users/new", data={
        "tenant_id": "hjmm-prod",
        "username": "testuser",
        "email": "test@example.com",
        "role": "staff",
        "theme_preference": "dark",
        "password": "short",
        "password_confirm": "short",
    })
    assert resp.status_code == 200
    assert "8 characters" in resp.text


@patch("modules.admin.user_mgmt_api._require_admin")
def test_t15_create_password_mismatch_shows_error(mock_require_admin):
    mock_require_admin.return_value = _mock_session()
    client = _make_client()
    resp = client.post("/admin/users/new", data={
        "tenant_id": "hjmm-prod",
        "username": "testuser",
        "email": "test@example.com",
        "role": "staff",
        "theme_preference": "dark",
        "password": "password123",
        "password_confirm": "different123",
    })
    assert resp.status_code == 200
    assert "do not match" in resp.text.lower()


@patch("modules.admin.user_mgmt_api._require_admin")
def test_t16_create_invalid_email_shows_error(mock_require_admin):
    mock_require_admin.return_value = _mock_session()
    client = _make_client()
    resp = client.post("/admin/users/new", data={
        "tenant_id": "hjmm-prod",
        "username": "testuser",
        "email": "not-an-email",
        "role": "staff",
        "theme_preference": "dark",
        "password": "validpass123",
        "password_confirm": "validpass123",
    })
    assert resp.status_code == 200
    assert "email" in resp.text.lower()


@patch("modules.admin.user_mgmt_api._require_admin")
def test_t17_create_missing_username_shows_error(mock_require_admin):
    mock_require_admin.return_value = _mock_session()
    client = _make_client()
    resp = client.post("/admin/users/new", data={
        "tenant_id": "hjmm-prod",
        "username": "   ",
        "email": "test@example.com",
        "role": "staff",
        "theme_preference": "dark",
        "password": "validpass123",
        "password_confirm": "validpass123",
    })
    assert resp.status_code == 200
    assert "username" in resp.text.lower()


@patch("modules.admin.user_mgmt_api._require_admin")
@patch("modules.admin.user_mgmt_api._get_user")
def test_t18_edit_unknown_user_returns_404(mock_get_user, mock_require_admin):
    mock_require_admin.return_value = _mock_session()
    mock_get_user.return_value = None
    client = _make_client()
    resp = client.get("/admin/users/99999?tenant_id=hjmm-prod")
    assert resp.status_code == 404


@patch("modules.admin.user_mgmt_api._require_admin")
@patch("modules.admin.user_mgmt_api.AsyncSessionLocal")
def test_t19_deactivate_redirects_to_list(mock_db, mock_require_admin):
    mock_require_admin.return_value = _mock_session()
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_db.return_value = mock_session
    client = _make_client()
    resp = client.post("/admin/users/1/deactivate",
                       data={"tenant_id": "hjmm-prod"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert "/admin/users" in resp.headers.get("location", "")


@patch("modules.admin.user_mgmt_api._require_admin")
@patch("modules.admin.user_mgmt_api.AsyncSessionLocal")
def test_t20_theme_toggle_returns_badge_html(mock_db, mock_require_admin):
    mock_require_admin.return_value = _mock_session()
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_db.return_value = mock_session
    client = _make_client()
    resp = client.post("/admin/users/1/theme", data={
        "tenant_id": "hjmm-prod",
        "theme_preference": "light",
    })
    assert resp.status_code == 200
    assert "badge" in resp.text
    assert "Light" in resp.text
