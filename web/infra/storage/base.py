"""
infra/storage/base.py
Praesidium Series 2.0 — Tenant Storage Provisioning Adapter

This is for PROVISIONING — creating the tenant's storage root at provision
time. It is NOT the runtime upload/download path (that is core/services/
storage.py / StorageService). Two separate concerns:

  - Provisioning storage backend (this file): mkdir the tenant root.
    Run once, at tenant creation. Returns the storage_adapter name to
    persist on tenants.storage_adapter.

  - Runtime storage service (core/services/storage.py): upload, download,
    list, etc. Selected per-request from tenants.storage_adapter.

Two backends ship:
  local       LocalFsBackend       /mnt/praesidium/{slug}/   (canonical going forward)
  cifs        CifsBridgeBackend    via FBRG-01 HTTP API      (deprecated; HJMM/enron/test only)

⚖  PATENT NOTICE: Patent Pending — 64/020,027
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StorageProvisioningBackend(Protocol):
    """Contract for tenant-storage-provisioning backends.

    Returns (ok, adapter_name_or_message). On success, the second tuple
    element is the value to persist on tenants.storage_adapter so the
    runtime can later route uploads/downloads to the matching runtime
    adapter. On failure, it's the diagnostic message.

    Idempotent: if the tenant root already exists, success is still
    returned and re-running the provisioning step is safe.
    """

    def create_tenant_root(self, slug: str) -> tuple[bool, str]:
        """Create the tenant's storage root.

        On success: return (True, adapter_name).
        On failure: return (False, error_message).
        """
        ...

    def health_check(self) -> tuple[bool, str]:
        """Confirm this backend can write to its storage location."""
        ...
