"""
infra/storage/local_fs.py
Praesidium Series 2.0 — Storage Provisioning Backend: Local Filesystem

Canonical going forward. Used by:
  - The appliance (data lives at /mnt/praesidium/{slug}/).
  - On-prem deployments using local storage (the typical case).
  - Cloud deployments that mount a managed volume at /mnt/praesidium/.

NOT used by:
  - Legacy HJMM/enron/test tenants on production — those use
    storage_adapter='cifs' and the FBRG-01 HTTP bridge. The cifs adapter
    is preserved for backward compatibility and is NOT proposed for new
    tenants.

Operating model:
  - The worker container has /mnt/praesidium bind-mounted from the host.
  - create_tenant_root mkdirs /mnt/praesidium/{slug}/ with mode 0750.
  - Existing directory is treated as success (idempotent).

Configuration via env vars:
  STORAGE_LOCAL_ROOT     /mnt/praesidium    (host-side mount root)

⚖  PATENT NOTICE: Patent Pending — 64/020,027
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("praesidium.infra.storage.local_fs")


class LocalFsBackend:
    """Provisions tenant storage roots on the local filesystem.

    Persists `storage_adapter='local'` on tenants. The runtime
    StorageService implementation that reads from this same path is
    the LocalStorageAdapter (separate file in core/services/, not
    yet ported from CIFSStorageAdapter).
    """

    ADAPTER_NAME = "local"

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or os.environ.get("STORAGE_LOCAL_ROOT", "/mnt/praesidium"))

    def create_tenant_root(self, slug: str) -> tuple[bool, str]:
        # Defensive: prevent slug from escaping the root via path traversal.
        if "/" in slug or ".." in slug or not slug:
            return False, f"invalid slug for storage path: {slug!r}"

        target = self.root / slug
        try:
            # Ensure root itself exists (in case mount is bare).
            self.root.mkdir(parents=True, exist_ok=True)
            target.mkdir(mode=0o750, exist_ok=True)
            log.info("LocalFsBackend.create_tenant_root: %s ready", target)
            return True, self.ADAPTER_NAME
        except OSError as exc:
            return False, f"mkdir {target} failed: {exc}"

    def health_check(self) -> tuple[bool, str]:
        try:
            if not self.root.exists():
                return False, f"storage root {self.root} does not exist"
            if not self.root.is_dir():
                return False, f"storage root {self.root} is not a directory"
            # Write probe to confirm RW.
            probe = self.root / ".healthcheck"
            probe.write_text("ok")
            probe.unlink()
            return True, f"local fs ok at {self.root}"
        except OSError as exc:
            return False, f"local fs not writable at {self.root}: {exc}"
