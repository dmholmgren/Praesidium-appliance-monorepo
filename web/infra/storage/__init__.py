"""
infra/storage/__init__.py
Praesidium Series 2.0 — Tenant Storage Provisioning

Selects and constructs a StorageProvisioningBackend based on the
STORAGE_BACKEND env var.

Supported values:
  local   → LocalFsBackend       (canonical going forward — appliance, on-prem, cloud)
  cifs    → CifsBridgeBackend    (DEPRECATED — HJMM/enron/test only)

Default: local

⚖  PATENT NOTICE: Patent Pending — 64/020,027
"""

from __future__ import annotations

import os
from .base import StorageProvisioningBackend
from .local_fs import LocalFsBackend
from .cifs_bridge import CifsBridgeBackend


__all__ = [
    "StorageProvisioningBackend",
    "LocalFsBackend",
    "CifsBridgeBackend",
    "get_storage_backend",
]


def get_storage_backend() -> StorageProvisioningBackend:
    """Return the configured backend instance.

    Reads STORAGE_BACKEND from the environment. Per-backend variables
    are read inside each backend's __init__.

    Raises ValueError on unknown value — fail fast over silent fallback.
    """
    name = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
    if name == "local":
        return LocalFsBackend()
    if name == "cifs":
        return CifsBridgeBackend()
    raise ValueError(
        f"Unknown STORAGE_BACKEND={name!r}. "
        "Valid: 'local' (canonical) or 'cifs' (deprecated, HJMM/enron/test)."
    )
