"""
tests/infra/test_local_fs_backend.py
Praesidium Series 2.0 — LocalFsBackend unit tests
"""

from __future__ import annotations

import pytest

from infra.storage.local_fs import LocalFsBackend


@pytest.fixture
def backend(tmp_path):
    return LocalFsBackend(root=str(tmp_path / "praesidium"))


def test_create_tenant_root_creates_dir(backend, tmp_path):
    ok, adapter = backend.create_tenant_root("acme")
    assert ok
    assert adapter == "local"
    assert (tmp_path / "praesidium" / "acme").is_dir()


def test_create_tenant_root_idempotent(backend, tmp_path):
    backend.create_tenant_root("acme")
    ok, adapter = backend.create_tenant_root("acme")
    assert ok
    assert adapter == "local"


def test_create_tenant_root_rejects_path_traversal(backend):
    ok, msg = backend.create_tenant_root("../escape")
    assert not ok
    assert "invalid slug" in msg


def test_create_tenant_root_rejects_slash(backend):
    ok, msg = backend.create_tenant_root("a/b")
    assert not ok
    assert "invalid slug" in msg


def test_create_tenant_root_rejects_empty(backend):
    ok, msg = backend.create_tenant_root("")
    assert not ok


def test_health_check_passes_on_writable_dir(backend, tmp_path):
    (tmp_path / "praesidium").mkdir()
    ok, msg = backend.health_check()
    assert ok, msg


def test_health_check_fails_when_root_missing(tmp_path):
    backend = LocalFsBackend(root=str(tmp_path / "does-not-exist"))
    ok, msg = backend.health_check()
    assert not ok
    assert "does not exist" in msg
