"""
tests/infra/test_factories.py
Praesidium Series 2.0 — Backend selection factory tests
"""

from __future__ import annotations

import pytest

from infra.proxy import LocalNginxBackend, SshRprxBackend, get_proxy_backend
from infra.storage import (
    CifsBridgeBackend,
    LocalFsBackend,
    get_storage_backend,
)


# ── Proxy ───────────────────────────────────────────────────────────────────


def test_proxy_factory_default_is_local(monkeypatch):
    monkeypatch.delenv("PROXY_BACKEND", raising=False)
    backend = get_proxy_backend()
    assert isinstance(backend, LocalNginxBackend)


def test_proxy_factory_local(monkeypatch):
    monkeypatch.setenv("PROXY_BACKEND", "local")
    backend = get_proxy_backend()
    assert isinstance(backend, LocalNginxBackend)


def test_proxy_factory_ssh_rprx(monkeypatch):
    monkeypatch.setenv("PROXY_BACKEND", "ssh-rprx")
    backend = get_proxy_backend()
    assert isinstance(backend, SshRprxBackend)


def test_proxy_factory_ssh_rprx_underscore(monkeypatch):
    monkeypatch.setenv("PROXY_BACKEND", "ssh_rprx")
    backend = get_proxy_backend()
    assert isinstance(backend, SshRprxBackend)


def test_proxy_factory_unknown_value_raises(monkeypatch):
    monkeypatch.setenv("PROXY_BACKEND", "haproxy")
    with pytest.raises(ValueError) as exc_info:
        get_proxy_backend()
    assert "haproxy" in str(exc_info.value)


# ── Storage ─────────────────────────────────────────────────────────────────


def test_storage_factory_default_is_local(monkeypatch):
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    backend = get_storage_backend()
    assert isinstance(backend, LocalFsBackend)


def test_storage_factory_local(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    backend = get_storage_backend()
    assert isinstance(backend, LocalFsBackend)


def test_storage_factory_cifs(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "cifs")
    backend = get_storage_backend()
    assert isinstance(backend, CifsBridgeBackend)


def test_storage_factory_unknown_raises(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    with pytest.raises(ValueError) as exc_info:
        get_storage_backend()
    assert "s3" in str(exc_info.value)


def test_proxy_factory_handles_whitespace_and_case(monkeypatch):
    monkeypatch.setenv("PROXY_BACKEND", "  LOCAL  ")
    backend = get_proxy_backend()
    assert isinstance(backend, LocalNginxBackend)
