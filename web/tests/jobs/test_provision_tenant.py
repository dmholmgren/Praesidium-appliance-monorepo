"""
tests/jobs/test_provision_tenant.py
Praesidium Series 2.0 — provision_tenant data path tests

The data path is mostly DB INSERTs; we validate query shape with mocked
psycopg2. The proxy step is mocked too — its tests live separately.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ── DSN parsing (pure function; easy to test) ───────────────────────────────


def test_parse_dsn_simple():
    from jobs.provision_tenant import _parse_dsn
    out = _parse_dsn("postgresql+asyncpg://user:pw@host:5432/db")
    assert "host=host" in out
    assert "port=5432" in out
    assert "user=user" in out
    assert "password=pw" in out
    assert "dbname=db" in out


def test_parse_dsn_password_with_at():
    """Project-known footgun: passwords containing @."""
    from jobs.provision_tenant import _parse_dsn
    out = _parse_dsn("postgresql+asyncpg://user:p@ss@host:5432/db")
    # The rfind('@') correctly splits at the LAST @.
    assert "password=p@ss" in out
    assert "host=host" in out


def test_parse_dsn_default_port():
    from jobs.provision_tenant import _parse_dsn
    out = _parse_dsn("postgresql://user:pw@host/db")
    assert "port=5432" in out


def test_parse_dsn_psycopg2_dialect():
    from jobs.provision_tenant import _parse_dsn
    out = _parse_dsn("postgresql+psycopg2://user:pw@host:5432/db")
    assert "host=host" in out


def test_parse_dsn_bad_url_raises():
    from jobs.provision_tenant import _parse_dsn
    with pytest.raises(ValueError):
        _parse_dsn("not-a-postgres-url")


# ── Username derivation ─────────────────────────────────────────────────────


def test_username_from_email_strips_domain():
    from jobs.provision_tenant import _username_from_email
    assert _username_from_email("alice@example.com") == "alice"


def test_username_from_email_safe_chars_only():
    from jobs.provision_tenant import _username_from_email
    out = _username_from_email("a+b!c@example.com")
    assert "+" not in out
    assert "!" not in out


def test_full_name_from_email_pretty():
    from jobs.provision_tenant import _full_name_from_email
    assert _full_name_from_email("alice.smith@example.com") == "Alice Smith"


# ── Tier presets ────────────────────────────────────────────────────────────


def test_tier_presets_have_canonical_keys():
    """Wizard flag names must match what tenant_licenses uses today.
    Verified against production data 2026-04-29.
    """
    from jobs.provision_tenant import TIER_PRESETS
    expected = {
        "ediscovery", "predictive_coding", "email_threading", "near_duplicate",
        "pleading_analysis", "kg_extraction", "cite_it", "trial_desk",
        "ai_timesheet", "billing_import", "document_review",
        "collection_drop_zone", "production_import", "byok",
        "custom_branding", "white_label",
    }
    for tier_name, flags in TIER_PRESETS.items():
        # Every preset has the same set of keys.
        assert set(flags.keys()) == expected, (
            f"Tier {tier_name} flag set mismatch — flag names must match "
            f"production tenant_licenses table"
        )


def test_intelligence_tier_enables_all():
    from jobs.provision_tenant import TIER_PRESETS
    flags = TIER_PRESETS["intelligence"]
    assert all(flags.values())


def test_starter_tier_minimal():
    from jobs.provision_tenant import TIER_PRESETS
    flags = TIER_PRESETS["starter"]
    # Only billing_import is on for starter.
    assert flags["billing_import"]
    assert sum(1 for v in flags.values() if v) == 1


# ── End-to-end orchestration with mocked DB and storage ──────────────────────


@patch("jobs.provision_tenant.bcrypt")
@patch("jobs.provision_tenant._get_conn")
@patch("jobs.provision_tenant.os.environ", {"DATABASE_URL": "postgresql://u:p@h/d"})
def test_provision_tenant_inserts_with_correct_columns(
    mock_get_conn, mock_bcrypt, monkeypatch
):
    """Verify the INSERTs match the live schema (no firm_name on tenants,
    no provision_status, password_hash not hashed_password, etc.).
    """
    # Mock storage backend.
    storage_mock = MagicMock()
    storage_mock.create_tenant_root.return_value = (True, "local")

    # Mock RQ queue (proxy enqueue should be called but not actually run).
    queue_mock = MagicMock()
    job_mock = MagicMock()
    job_mock.id = "fake-proxy-job-id"
    queue_mock.enqueue.return_value = job_mock

    redis_mock = MagicMock()

    with patch("infra.storage.get_storage_backend", return_value=storage_mock), \
         patch("redis.Redis.from_url", return_value=redis_mock), \
         patch("rq.Queue", return_value=queue_mock):
        # Mock psycopg2 connection.
        conn_mock = MagicMock()
        cur_mock = MagicMock()
        conn_mock.cursor.return_value = cur_mock
        cur_mock.fetchone.return_value = ("test-tenant-id-1234",)
        conn_mock.cursor.return_value.__enter__ = MagicMock(return_value=cur_mock)
        conn_mock.cursor.return_value.__exit__ = MagicMock(return_value=None)
        mock_get_conn.return_value = conn_mock

        mock_bcrypt.hashpw.return_value = b"$2b$fake_hash"
        mock_bcrypt.gensalt.return_value = b"salt"

        from jobs.provision_tenant import provision_tenant
        result = provision_tenant({
            "slug": "acme",
            "firm_name": "Acme Corp",
            "domain": "acme.example.com",
            "ssl_mode": "letsencrypt",
            "first_user_email": "alice@acme.com",
            "feature_pack": "intelligence",
            "deployment_tier": "dedicated",
        })

    assert result["status"] == "data_complete"
    assert result["slug"] == "acme"

    # Reconstruct all SQL statements that were executed.
    executed_sql = [c.args[0] for c in cur_mock.execute.call_args_list]
    all_sql = " ".join(executed_sql).lower()

    # Schema-correct INSERT into tenants.
    assert "insert into tenants" in all_sql
    assert "name" in all_sql, "should use 'name' column (not firm_name)"
    assert "is_active" in all_sql

    # Schema-correct INSERT into tenant_branding.
    assert "insert into tenant_branding" in all_sql
    assert "firm_name" in all_sql  # branding DOES have firm_name.

    # Schema-correct INSERT into tenant_licenses.
    assert "insert into tenant_licenses" in all_sql
    assert "feature_flags" in all_sql

    # Schema-correct INSERT into users.
    assert "insert into users" in all_sql
    assert "password_hash" in all_sql, "should use password_hash column"
    assert "must_change_password" not in all_sql, (
        "must_change_password column does not exist — use invitation_token"
    )

    # No bogus provision_status references.
    assert "provision_status" not in all_sql, (
        "provision_status column does not exist — use is_active or status"
    )

    # No writes to feature_overrides (that's a global override table,
    # not per-tenant).
    overrides_inserts = [s for s in executed_sql
                         if "insert into feature_overrides" in s.lower()]
    assert not overrides_inserts


@patch("jobs.provision_tenant.bcrypt")
@patch("jobs.provision_tenant._get_conn")
def test_provision_tenant_handles_slug_conflict(mock_get_conn, mock_bcrypt):
    conn_mock = MagicMock()
    cur_mock = MagicMock()
    conn_mock.cursor.return_value = cur_mock
    # ON CONFLICT DO NOTHING returns no row.
    cur_mock.fetchone.return_value = None
    mock_get_conn.return_value = conn_mock

    from jobs.provision_tenant import provision_tenant
    result = provision_tenant({
        "slug": "duplicate",
        "firm_name": "Dup",
        "domain": "duplicate.example.com",
        "ssl_mode": "letsencrypt",
        "first_user_email": "x@y.com",
    })
    assert result["status"] == "failed"
    assert "race" in result["error"].lower() or "in use" in result["error"].lower()


@patch("jobs.provision_tenant._get_conn")
def test_provision_tenant_db_failure_returns_failed(mock_get_conn):
    mock_get_conn.side_effect = RuntimeError("DB unreachable")
    from jobs.provision_tenant import provision_tenant
    result = provision_tenant({
        "slug": "acme",
        "firm_name": "Acme",
        "domain": "acme.example.com",
        "ssl_mode": "letsencrypt",
        "first_user_email": "a@b.com",
    })
    assert result["status"] == "failed"
    assert "DB unreachable" in result["error"]
