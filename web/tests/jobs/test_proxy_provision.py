"""
tests/jobs/test_proxy_provision.py
Praesidium Series 2.0 — proxy_provision tests
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── Cert path resolution (pure function) ─────────────────────────────────────


def test_resolve_cert_paths_letsencrypt_strips_subdomain(monkeypatch):
    monkeypatch.delenv("PROXY_LETSENCRYPT_BASE", raising=False)
    from jobs.proxy_provision import _resolve_cert_paths
    cert, key = _resolve_cert_paths(
        "acme.praesidium-legal.com", "letsencrypt", None, None,
    )
    assert cert == "/etc/letsencrypt/live/praesidium-legal.com/fullchain.pem"
    assert key == "/etc/letsencrypt/live/praesidium-legal.com/privkey.pem"


def test_resolve_cert_paths_letsencrypt_two_label_keeps_full(monkeypatch):
    monkeypatch.delenv("PROXY_LETSENCRYPT_BASE", raising=False)
    from jobs.proxy_provision import _resolve_cert_paths
    cert, _ = _resolve_cert_paths(
        "example.com", "letsencrypt", None, None,
    )
    assert "example.com" in cert


def test_resolve_cert_paths_env_override(monkeypatch):
    monkeypatch.setenv("PROXY_LETSENCRYPT_BASE", "hjmmlegal.com")
    from jobs.proxy_provision import _resolve_cert_paths
    cert, key = _resolve_cert_paths(
        "anything.example.com", "letsencrypt", None, None,
    )
    assert cert == "/etc/letsencrypt/live/hjmmlegal.com/fullchain.pem"


def test_resolve_cert_paths_custom_uses_supplied():
    from jobs.proxy_provision import _resolve_cert_paths
    cert, key = _resolve_cert_paths(
        "x.example.com", "custom",
        "/etc/praesidium/certs/x/fullchain.pem",
        "/etc/praesidium/certs/x/privkey.pem",
    )
    assert cert == "/etc/praesidium/certs/x/fullchain.pem"
    assert key == "/etc/praesidium/certs/x/privkey.pem"


def test_resolve_cert_paths_custom_missing_paths_raises():
    from jobs.proxy_provision import _resolve_cert_paths
    with pytest.raises(ValueError):
        _resolve_cert_paths("x.example.com", "custom", None, None)


# ── End-to-end orchestration ─────────────────────────────────────────────────


def _backend_mock(
    *,
    health=(True, ""), write=(True, ""), reload_=(True, ""),
):
    backend = MagicMock()
    backend.health_check.return_value = health
    backend.write_vhost.return_value = write
    backend.reload.return_value = reload_
    return backend


def _conn_mock():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=None)
    return conn, cur


@patch("jobs.proxy_provision._get_conn")
def test_provision_proxy_happy_path(mock_get_conn, monkeypatch):
    monkeypatch.delenv("PROXY_LETSENCRYPT_BASE", raising=False)
    conn, cur = _conn_mock()
    mock_get_conn.return_value = conn
    backend = _backend_mock()
    with patch("infra.proxy.get_proxy_backend", return_value=backend):
        from jobs.proxy_provision import provision_proxy
        result = provision_proxy({
            "tenant_id": "tid-1",
            "slug": "acme",
            "domain": "acme.example.com",
            "ssl_mode": "letsencrypt",
        })
    assert result["status"] == "complete"
    backend.write_vhost.assert_called_once()
    backend.reload.assert_called_once()
    # is_active flipped to true.
    update_calls = [c for c in cur.execute.call_args_list
                    if "is_active = true" in c.args[0].lower().replace("  ", " ")
                    or "is_active=true" in c.args[0].lower()]
    assert update_calls, "expected an UPDATE setting is_active=true"


def test_provision_proxy_health_failure_short_circuits():
    backend = _backend_mock(health=(False, "container dead"))
    with patch("infra.proxy.get_proxy_backend", return_value=backend):
        from jobs.proxy_provision import provision_proxy
        result = provision_proxy({
            "tenant_id": "tid", "slug": "acme",
            "domain": "acme.example.com", "ssl_mode": "letsencrypt",
        })
    assert result["status"] == "failed"
    assert result["step"] == "health_check"
    backend.write_vhost.assert_not_called()


def test_provision_proxy_write_failure_skips_reload():
    backend = _backend_mock(write=(False, "permission denied"))
    with patch("infra.proxy.get_proxy_backend", return_value=backend):
        from jobs.proxy_provision import provision_proxy
        result = provision_proxy({
            "tenant_id": "tid", "slug": "acme",
            "domain": "acme.example.com", "ssl_mode": "letsencrypt",
        })
    assert result["status"] == "failed"
    assert result["step"] == "write_vhost"
    backend.reload.assert_not_called()


def test_provision_proxy_reload_failure_does_not_remove_vhost():
    backend = _backend_mock(reload_=(False, "nginx -t failed"))
    with patch("infra.proxy.get_proxy_backend", return_value=backend):
        from jobs.proxy_provision import provision_proxy
        result = provision_proxy({
            "tenant_id": "tid", "slug": "acme",
            "domain": "acme.example.com", "ssl_mode": "letsencrypt",
        })
    assert result["status"] == "failed"
    assert result["step"] == "reload"
    # Importantly, the backend's remove_vhost should NOT be called —
    # we leave the vhost in place so the next retry overwrites it
    # rather than nuking a previously-working state.
    backend.remove_vhost.assert_not_called()
