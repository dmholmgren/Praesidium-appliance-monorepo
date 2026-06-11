"""
infra/proxy/base.py
Praesidium Series 2.0 — Reverse Proxy Provisioning Adapter Interface

This module defines the contract every reverse-proxy backend must implement.
Backends are selected at runtime via the PROXY_BACKEND environment variable;
see infra/proxy/__init__.py for the factory.

A "proxy backend" is responsible for materializing a tenant's HTTPS endpoint:
writing the vhost config, reloading the proxy, and confirming the new
hostname is reachable. Backends are NOT responsible for DNS, certificates,
or DB state — those are handled by other layers.

Every method returns (ok: bool, message: str). Backends do not raise on
operational errors; they return False with a human-readable diagnostic.
This lets the orchestrator treat backends as black-box infrastructure
that can fail and be retried without exception handling everywhere.

⚖  PATENT NOTICE: Patent Pending — 64/020,027
   Filed March 29, 2026 by Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProxyBackend(Protocol):
    """Contract for reverse-proxy provisioning backends.

    Implementations: LocalNginxBackend, SshRprxBackend.

    Method semantics:
      - All methods are synchronous. The orchestrator runs in an RQ worker
        and uses subprocess; no event loop to share.
      - All methods are idempotent. write_vhost on an existing slug
        overwrites; remove_vhost on a missing slug is a no-op success;
        reload is naturally idempotent.
      - Returns (ok, message). On failure, message is a single-line human
        diagnostic suitable for logging and surfacing to the wizard
        progress page. On success, message may be an empty string or a
        short confirmation.
    """

    def write_vhost(
        self,
        slug: str,
        domain: str,
        ssl_mode: str,
        cert_path: str,
        key_path: str,
        upstream_host: str,
        upstream_port: int,
    ) -> tuple[bool, str]:
        """Write or replace the vhost config for this tenant.

        Args:
            slug: Tenant slug (URL-safe, lowercase, used as filename).
            domain: Fully-qualified hostname (e.g. acme.praesidium-legal.com).
            ssl_mode: 'letsencrypt' or 'custom'.
            cert_path: Absolute path to the fullchain PEM that the proxy
                will read at runtime. The orchestrator computes this; the
                backend just embeds it in the vhost config.
            key_path: Absolute path to the private key PEM.
            upstream_host: IP or hostname the proxy should forward to.
            upstream_port: TCP port on the upstream.
        """
        ...

    def remove_vhost(self, slug: str) -> tuple[bool, str]:
        """Remove the vhost config for this tenant.

        Idempotent: missing files are not an error.
        Caller is responsible for calling reload() afterward.
        """
        ...

    def reload(self) -> tuple[bool, str]:
        """Validate and reload the proxy configuration.

        On failure (config invalid, daemon dead, etc.), the proxy stays
        running with the prior config — reload is non-disruptive. The
        backend MUST validate before reloading and return False with the
        validation error if invalid.
        """
        ...

    def health_check(self) -> tuple[bool, str]:
        """Confirm the proxy backend is reachable and responsive.

        Used by the orchestrator before attempting writes, and by ops
        tooling to confirm install correctness. Should be cheap.
        """
        ...
