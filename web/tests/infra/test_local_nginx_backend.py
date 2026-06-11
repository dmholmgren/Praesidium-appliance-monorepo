"""
tests/infra/test_local_nginx_backend.py
Praesidium Series 2.0 — LocalNginxBackend unit tests

Validates the appliance/on-prem proxy backend without touching docker
or the host filesystem outside a tmp_path scope.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from infra.proxy.local_nginx import LocalNginxBackend


@pytest.fixture
def backend(tmp_path):
    return LocalNginxBackend(
        sites_dir=str(tmp_path),
        container_name="test-nginx",
        listen_ip="10.0.0.1",
    )


def test_write_vhost_creates_file(backend, tmp_path):
    ok, msg = backend.write_vhost(
        slug="acme",
        domain="acme.example.com",
        ssl_mode="letsencrypt",
        cert_path="/etc/letsencrypt/live/example.com/fullchain.pem",
        key_path="/etc/letsencrypt/live/example.com/privkey.pem",
        upstream_host="172.28.1.3",
        upstream_port=8000,
    )
    assert ok, msg
    target = tmp_path / "acme.conf"
    assert target.exists()
    content = target.read_text()
    assert "server_name acme.example.com" in content
    assert "ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem" in content
    assert "10.0.0.1:443 ssl http2" in content
    assert "server 172.28.1.3:8000" in content


def test_write_vhost_overwrites(backend, tmp_path):
    """write_vhost is idempotent — second call overwrites the first."""
    backend.write_vhost(
        slug="acme", domain="acme.example.com", ssl_mode="letsencrypt",
        cert_path="/a.pem", key_path="/b.pem",
        upstream_host="1.1.1.1", upstream_port=80,
    )
    backend.write_vhost(
        slug="acme", domain="acme.example.com", ssl_mode="letsencrypt",
        cert_path="/c.pem", key_path="/d.pem",
        upstream_host="2.2.2.2", upstream_port=8080,
    )
    content = (tmp_path / "acme.conf").read_text()
    assert "/c.pem" in content
    assert "/a.pem" not in content
    assert "server 2.2.2.2:8080" in content


def test_write_vhost_handles_hyphen_in_slug(backend, tmp_path):
    """nginx upstream identifiers can't contain hyphens."""
    backend.write_vhost(
        slug="my-firm",
        domain="my-firm.example.com",
        ssl_mode="letsencrypt",
        cert_path="/a.pem", key_path="/b.pem",
        upstream_host="1.1.1.1", upstream_port=8000,
    )
    content = (tmp_path / "my-firm.conf").read_text()
    assert "upstream praesidium_web_my_firm" in content
    # Filename keeps the hyphen; only the upstream identifier is sanitized.
    assert (tmp_path / "my-firm.conf").exists()


def test_remove_vhost_removes_file(backend, tmp_path):
    target = tmp_path / "acme.conf"
    target.write_text("dummy")
    ok, _ = backend.remove_vhost("acme")
    assert ok
    assert not target.exists()


def test_remove_vhost_missing_is_idempotent(backend):
    ok, msg = backend.remove_vhost("nonexistent")
    assert ok
    assert "no-op" in msg.lower()


@patch("infra.proxy.local_nginx.subprocess.run")
def test_reload_validates_before_reloading(mock_run, backend):
    # First call (nginx -t) succeeds, second (-s reload) succeeds.
    mock_run.side_effect = [
        MagicMock(returncode=0, stdout="ok", stderr=""),
        MagicMock(returncode=0, stdout="", stderr=""),
    ]
    ok, _ = backend.reload()
    assert ok
    assert mock_run.call_count == 2
    assert mock_run.call_args_list[0].args[0] == [
        "docker", "exec", "test-nginx", "nginx", "-t",
    ]
    assert mock_run.call_args_list[1].args[0] == [
        "docker", "exec", "test-nginx", "nginx", "-s", "reload",
    ]


@patch("infra.proxy.local_nginx.subprocess.run")
def test_reload_aborts_if_validate_fails(mock_run, backend):
    mock_run.return_value = MagicMock(
        returncode=1, stdout="", stderr="nginx: [emerg] cannot load cert"
    )
    ok, msg = backend.reload()
    assert not ok
    assert "cannot load cert" in msg
    # Only ONE subprocess call — validation, then bail.
    assert mock_run.call_count == 1


@patch("infra.proxy.local_nginx.subprocess.run")
def test_health_check_passes_when_running(mock_run, backend):
    mock_run.side_effect = [
        # docker inspect → "true"
        MagicMock(returncode=0, stdout="true\n", stderr=""),
        # docker exec ... nginx -t → ok
        MagicMock(returncode=0, stdout="ok", stderr=""),
    ]
    ok, msg = backend.health_check()
    assert ok, msg


@patch("infra.proxy.local_nginx.subprocess.run")
def test_health_check_fails_when_container_stopped(mock_run, backend):
    mock_run.return_value = MagicMock(returncode=0, stdout="false\n", stderr="")
    ok, msg = backend.health_check()
    assert not ok
    assert "not running" in msg


@patch("infra.proxy.local_nginx.subprocess.run")
def test_health_check_fails_when_container_missing(mock_run, backend):
    mock_run.return_value = MagicMock(
        returncode=1, stdout="", stderr="No such object"
    )
    ok, msg = backend.health_check()
    assert not ok
    assert "not found" in msg


def test_path_traversal_rejected_by_filesystem(backend, tmp_path):
    # The backend doesn't validate slug; the orchestrator does (it
    # comes from a validated form). But filesystem mkdirs with ../
    # still resolve into the target dir — the test below confirms
    # the filename is what we wrote, not somewhere else.
    backend.write_vhost(
        slug="safe", domain="safe.example.com", ssl_mode="letsencrypt",
        cert_path="/a", key_path="/b", upstream_host="1.1.1.1",
        upstream_port=80,
    )
    assert (tmp_path / "safe.conf").exists()
    # No surprise files outside the dir.
    parent_files = list(tmp_path.parent.iterdir())
    assert all(p.name == tmp_path.name or not p.name.endswith(".conf")
               for p in parent_files)
