"""
tests/infra/test_ssh_rprx_backend.py
Praesidium Series 2.0 — SshRprxBackend unit tests
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from infra.proxy.ssh_rprx import SshRprxBackend


@pytest.fixture
def backend():
    return SshRprxBackend(
        rprx_host="rprx.test",
        ssh_user="praesidium",
        ssh_key="/dev/null",
        sites_available="/etc/nginx/sites-available",
        sites_enabled="/etc/nginx/sites-enabled",
    )


@patch("infra.proxy.ssh_rprx.subprocess.run")
def test_write_vhost_pipes_config_via_ssh(mock_run, backend):
    mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
    ok, msg = backend.write_vhost(
        slug="acme", domain="acme.example.com", ssl_mode="letsencrypt",
        cert_path="/etc/letsencrypt/live/example.com/fullchain.pem",
        key_path="/etc/letsencrypt/live/example.com/privkey.pem",
        upstream_host="1.2.3.4", upstream_port=8000,
    )
    assert ok, msg
    assert mock_run.call_count == 1

    # Verify the ssh argv contains the right host and the heredoc tee
    # command.
    argv = mock_run.call_args.args[0]
    assert "ssh" == argv[0]
    assert "praesidium@rprx.test" in argv
    full_cmd = argv[-1]
    assert "tee" in full_cmd
    assert "ln -sf" in full_cmd
    # shlex.quote leaves safe paths unquoted; verify the path is present
    # in either form.
    assert "/etc/nginx/sites-available/acme.conf" in full_cmd

    # Stdin must be the rendered config bytes.
    stdin_bytes = mock_run.call_args.kwargs["input"]
    assert isinstance(stdin_bytes, bytes)
    assert b"server_name acme.example.com" in stdin_bytes
    assert b"server 1.2.3.4:8000" in stdin_bytes


@patch("infra.proxy.ssh_rprx.subprocess.run")
def test_write_vhost_returns_error_on_nonzero(mock_run, backend):
    mock_run.return_value = MagicMock(
        returncode=1, stdout=b"", stderr=b"sudo: a password is required",
    )
    ok, msg = backend.write_vhost(
        slug="acme", domain="acme.example.com", ssl_mode="letsencrypt",
        cert_path="/a", key_path="/b",
        upstream_host="1.1.1.1", upstream_port=80,
    )
    assert not ok
    assert "password is required" in msg


@patch("infra.proxy.ssh_rprx.subprocess.run")
def test_remove_vhost_uses_rm(mock_run, backend):
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    ok, _ = backend.remove_vhost("acme")
    assert ok
    full_cmd = mock_run.call_args.args[0][-1]
    assert "rm -f" in full_cmd
    assert "/etc/nginx/sites-available/acme.conf" in full_cmd
    assert "/etc/nginx/sites-enabled/acme.conf" in full_cmd


@patch("infra.proxy.ssh_rprx.subprocess.run")
def test_reload_validates_then_reloads(mock_run, backend):
    mock_run.side_effect = [
        MagicMock(returncode=0, stdout="ok", stderr=""),    # nginx -t
        MagicMock(returncode=0, stdout="", stderr=""),       # systemctl reload
    ]
    ok, _ = backend.reload()
    assert ok
    assert mock_run.call_count == 2
    assert "nginx -t" in mock_run.call_args_list[0].args[0][-1]
    assert "systemctl reload nginx" in mock_run.call_args_list[1].args[0][-1]


@patch("infra.proxy.ssh_rprx.subprocess.run")
def test_reload_aborts_on_validate_failure(mock_run, backend):
    mock_run.return_value = MagicMock(
        returncode=1, stdout="", stderr="nginx: [emerg] invalid",
    )
    ok, msg = backend.reload()
    assert not ok
    assert "[emerg] invalid" in msg
    assert mock_run.call_count == 1


@patch("infra.proxy.ssh_rprx.subprocess.run")
def test_health_check_runs_nginx_t(mock_run, backend):
    mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
    ok, _ = backend.health_check()
    assert ok
    assert "nginx -t" in mock_run.call_args.args[0][-1]
