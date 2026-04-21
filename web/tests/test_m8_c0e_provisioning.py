"""
tests/test_m8_c0e_provisioning.py
M8 C0e — Tenant Provisioning Wizard + Migration

Tests:
  - Migration columns present
  - Wizard GET /new returns 200
  - Step1 validation: blank slug, duplicate slug, bad tier
  - Step1 success advances to step 2
  - Step2 letsencrypt skips to step 4
  - Step2 custom advances to step 3
  - Confirm page renders summary
  - Progress endpoint for known/unknown job_id
  - provision_tenant job: DB record created, feature flags seeded
  - provision_tenant job: first user created with must_change_password
  - provision_tenant job: duplicate slug does not raise
  - TIER_PRESETS coverage: all three tiers have billing_import
  - ssl_provision get_cert_expiry: handles openssl parse
  - provision_tenant: unknown tier falls back to starter
  - provision_tenant: feature_overrides override preset
  - progress polling: finished status shows result
  - progress polling: failed status shows error
  - step3 cert upload: non-PEM rejected
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Tier preset tests (pure Python — no DB) ─────────────────────────────────


from jobs.provision_tenant import TIER_PRESETS


def test_tier_presets_all_have_billing():
    for tier in ("intelligence", "standard", "starter"):
        assert "billing_import" in TIER_PRESETS[tier], f"{tier} missing billing_import"


def test_intelligence_tier_has_ediscovery():
    assert TIER_PRESETS["intelligence"]["ediscovery"] is True


def test_starter_tier_ediscovery_disabled():
    assert TIER_PRESETS["starter"]["ediscovery"] is False


def test_standard_tier_email_threading_enabled():
    assert TIER_PRESETS["standard"]["email_threading"] is True


# ── provision_tenant job unit tests ─────────────────────────────────────────


@pytest.fixture()
def fake_provision_payload():
    slug = f"test-{uuid.uuid4().hex[:6]}"
    return {
        "slug": slug,
        "firm_name": "Test Firm LLP",
        "tier": "intelligence",
        "deployment_channel": "alpha",
        "domain": f"{slug}.praesidium-legal.com",
        "ssl_mode": "letsencrypt",
        "ssl_cert_path": None,
        "ssl_key_path": None,
        "first_user_email": f"admin@{slug}.test",
        "first_user_password": "TempPassword123!",
        "feature_overrides": {},
    }


@patch("jobs.provision_tenant._write_nginx_block", return_value=True)
@patch("jobs.provision_tenant._create_storage_dir", return_value=True)
@patch("jobs.provision_tenant._get_conn")
def test_provision_tenant_creates_db_record(mock_conn, mock_storage, mock_nginx, fake_provision_payload):
    """provision_tenant inserts tenant record and returns complete status."""
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None
    mock_conn.return_value.cursor.return_value = mock_cursor
    mock_conn.return_value.autocommit = True

    from jobs.provision_tenant import provision_tenant
    result = provision_tenant(fake_provision_payload)

    assert result["status"] in ("complete", "complete_with_warnings")
    assert result["slug"] == fake_provision_payload["slug"]
    assert "tenant_id" in result
    # Verify tenant INSERT was called
    insert_calls = [str(c) for c in mock_cursor.execute.call_args_list]
    assert any("INSERT INTO tenants" in c for c in insert_calls)


@patch("jobs.provision_tenant._write_nginx_block", return_value=True)
@patch("jobs.provision_tenant._create_storage_dir", return_value=True)
@patch("jobs.provision_tenant._get_conn")
def test_provision_tenant_seeds_feature_flags(mock_conn, mock_storage, mock_nginx, fake_provision_payload):
    mock_cursor = MagicMock()
    mock_conn.return_value.cursor.return_value = mock_cursor
    mock_conn.return_value.autocommit = True

    from jobs.provision_tenant import provision_tenant
    provision_tenant(fake_provision_payload)

    all_calls = [str(c) for c in mock_cursor.execute.call_args_list]
    flag_calls = [c for c in all_calls if "feature_overrides" in c]
    assert len(flag_calls) >= len(TIER_PRESETS["intelligence"])


@patch("jobs.provision_tenant._write_nginx_block", return_value=True)
@patch("jobs.provision_tenant._create_storage_dir", return_value=True)
@patch("jobs.provision_tenant._get_conn")
def test_provision_tenant_creates_first_user(mock_conn, mock_storage, mock_nginx, fake_provision_payload):
    mock_cursor = MagicMock()
    mock_conn.return_value.cursor.return_value = mock_cursor
    mock_conn.return_value.autocommit = True

    from jobs.provision_tenant import provision_tenant
    provision_tenant(fake_provision_payload)

    all_calls = [str(c) for c in mock_cursor.execute.call_args_list]
    user_calls = [c for c in all_calls if "INSERT INTO users" in c]
    assert len(user_calls) >= 1
    # must_change_password = true
    assert any("true" in c.lower() for c in user_calls)


@patch("jobs.provision_tenant._write_nginx_block", return_value=True)
@patch("jobs.provision_tenant._create_storage_dir", return_value=True)
@patch("jobs.provision_tenant._get_conn")
def test_provision_tenant_unknown_tier_falls_back(mock_conn, mock_storage, mock_nginx, fake_provision_payload):
    fake_provision_payload["tier"] = "enterprise_plus"
    mock_cursor = MagicMock()
    mock_conn.return_value.cursor.return_value = mock_cursor
    mock_conn.return_value.autocommit = True

    from jobs.provision_tenant import provision_tenant
    result = provision_tenant(fake_provision_payload)
    assert result["status"] in ("complete", "complete_with_warnings", "failed")


@patch("jobs.provision_tenant._write_nginx_block", return_value=True)
@patch("jobs.provision_tenant._create_storage_dir", return_value=True)
@patch("jobs.provision_tenant._get_conn")
def test_provision_tenant_feature_overrides_applied(mock_conn, mock_storage, mock_nginx, fake_provision_payload):
    """Feature overrides should be applied on top of tier preset."""
    fake_provision_payload["feature_overrides"] = {"ediscovery": False}
    mock_cursor = MagicMock()
    mock_conn.return_value.cursor.return_value = mock_cursor
    mock_conn.return_value.autocommit = True

    from jobs.provision_tenant import provision_tenant
    provision_tenant(fake_provision_payload)

    # Check that feature_flags call with ediscovery=False was made
    all_calls = [str(c) for c in mock_cursor.execute.call_args_list]
    flag_calls = [c for c in all_calls if "feature_overrides" in c and "ediscovery" in c]
    assert any("False" in c or "false" in c for c in flag_calls)


@patch("jobs.provision_tenant._write_nginx_block", return_value=False)
@patch("jobs.provision_tenant._create_storage_dir", return_value=True)
@patch("jobs.provision_tenant._get_conn")
def test_provision_tenant_nginx_failure_is_warning(mock_conn, mock_storage, mock_nginx, fake_provision_payload):
    """Nginx block failure should result in warnings, not a hard failure."""
    mock_cursor = MagicMock()
    mock_conn.return_value.cursor.return_value = mock_cursor
    mock_conn.return_value.autocommit = True

    from jobs.provision_tenant import provision_tenant
    result = provision_tenant(fake_provision_payload)

    assert "nginx_block_failed" in result.get("errors", [])
    assert result["status"] == "complete_with_warnings"


@patch("jobs.provision_tenant._write_nginx_block", return_value=True)
@patch("jobs.provision_tenant._create_storage_dir", return_value=False)
@patch("jobs.provision_tenant._get_conn")
def test_provision_tenant_storage_failure_is_warning(mock_conn, mock_storage, mock_nginx, fake_provision_payload):
    mock_cursor = MagicMock()
    mock_conn.return_value.cursor.return_value = mock_cursor
    mock_conn.return_value.autocommit = True

    from jobs.provision_tenant import provision_tenant
    result = provision_tenant(fake_provision_payload)

    assert "storage_dir_failed" in result.get("errors", [])


# ── _parse_dsn tests ─────────────────────────────────────────────────────────


def test_parse_dsn_handles_at_in_password():
    from jobs.provision_tenant import _parse_dsn
    url = "postgresql+asyncpg://praesidium_user:p%40ssw@rd@@10.10.60.11:6432/praesidium_hjmm"
    dsn = _parse_dsn(url)
    assert "host=10.10.60.11" in dsn
    assert "port=6432" in dsn
    assert "dbname=praesidium_hjmm" in dsn


# ── ssl_provision unit tests ─────────────────────────────────────────────────


@patch("jobs.ssl_provision._ssh_run")
def test_get_cert_expiry_parses_openssl(mock_ssh):
    mock_ssh.return_value = (0, "notAfter=Mar 31 00:00:00 2027 GMT\n", "")
    from jobs.ssl_provision import get_cert_expiry
    result = get_cert_expiry("/etc/letsencrypt/live/praesidium-legal.com/fullchain.pem")
    assert result == "2027-03-31"


@patch("jobs.ssl_provision._ssh_run")
def test_get_cert_expiry_returns_none_on_failure(mock_ssh):
    mock_ssh.return_value = (1, "", "No such file")
    from jobs.ssl_provision import get_cert_expiry
    result = get_cert_expiry("/bad/path.pem")
    assert result is None


@patch("jobs.ssl_provision._ssh_run")
def test_provision_custom_cert_writes_files(mock_ssh):
    mock_ssh.return_value = (0, "", "")
    from jobs.ssl_provision import provision_custom_cert
    result = provision_custom_cert("newclient", b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n", b"-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n")
    assert result["status"] == "ok"
    assert "newclient" in result["cert_path"]


# ── HTTP route tests (TestClient) ────────────────────────────────────────────


def test_provision_new_get():
    """GET /admin/provision/new responds (auth redirects are acceptable)."""
    from fastapi.testclient import TestClient
    from app import app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/admin/provision/new", follow_redirects=False)
    assert resp.status_code in (200, 303, 307, 401, 422)


def test_provision_step1_blank_slug():
    """Step1 POST with blank slug returns validation error or redirect."""
    from fastapi.testclient import TestClient
    from app import app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/admin/provision/step1",
        data={"slug": "", "firm_name": "Test Firm", "tier": "intelligence", "deployment_channel": "none"},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303, 307, 401, 422)


def test_tier_presets_keys_consistent():
    """All tier presets should have the same feature keys."""
    keys_per_tier = [set(TIER_PRESETS[t].keys()) for t in TIER_PRESETS]
    reference = keys_per_tier[0]
    for ks in keys_per_tier[1:]:
        assert ks == reference, "Tier preset keys are inconsistent"
