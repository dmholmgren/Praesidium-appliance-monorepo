"""
infra/storage/cifs_bridge.py
Praesidium Series 2.0 — Storage Provisioning Backend: CIFS Bridge (DEPRECATED)

⚠ DEPRECATED — kept only for back-compat with HJMM/enron/test on
production. New tenants on every deployment should use LocalFsBackend.
This adapter targets the FBRG-01 HTTP bridge which proxies CIFS mounts
into HTTP calls from the worker tier.

Operating model:
  - Worker calls FBRG-01 over HTTP to mkdir on the CIFS-mounted share.
  - Backend persists storage_adapter='cifs' on tenants.

Will be removed when HJMM/enron/test are migrated off CIFS storage.

Configuration via env vars:
  CIFS_BRIDGE_URL    http://10.10.60.13:8080

⚖  PATENT NOTICE: Patent Pending — 64/020,027
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("praesidium.infra.storage.cifs_bridge")


class CifsBridgeBackend:
    """DEPRECATED. Calls FBRG-01 to mkdir on the legacy CIFS share."""

    ADAPTER_NAME = "cifs"

    def __init__(self, bridge_url: str | None = None) -> None:
        self.bridge_url = (
            bridge_url
            or os.environ.get("CIFS_BRIDGE_URL", "http://10.10.60.13:8080")
        ).rstrip("/")

    def create_tenant_root(self, slug: str) -> tuple[bool, str]:
        try:
            import httpx  # local import; avoids dep at module load time
            resp = httpx.post(
                f"{self.bridge_url}/storage/mkdir",
                json={"path": f"praesidium/{slug}"},
                timeout=15,
            )
            resp.raise_for_status()
            log.info("CifsBridgeBackend.create_tenant_root: %s ok", slug)
            return True, self.ADAPTER_NAME
        except Exception as exc:
            return False, f"cifs bridge mkdir failed: {exc}"

    def health_check(self) -> tuple[bool, str]:
        try:
            import httpx
            resp = httpx.get(f"{self.bridge_url}/health", timeout=5)
            if resp.status_code != 200:
                return False, f"bridge returned {resp.status_code}"
            return True, f"cifs bridge ok at {self.bridge_url}"
        except Exception as exc:
            return False, f"cifs bridge unreachable: {exc}"
