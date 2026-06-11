"""
infra/proxy/__init__.py
Praesidium Series 2.0 — Reverse Proxy Provisioning

Selects and constructs a ProxyBackend based on the PROXY_BACKEND env var.

Supported values:
  local      → LocalNginxBackend  (appliance, on-prem multi-tenant test)
  ssh-rprx   → SshRprxBackend     (HJMM production via 10.10.40.50)

Default: local

⚖  PATENT NOTICE: Patent Pending — 64/020,027
"""

from __future__ import annotations

import os
from .base import ProxyBackend
from .local_nginx import LocalNginxBackend
from .ssh_rprx import SshRprxBackend


__all__ = ["ProxyBackend", "LocalNginxBackend", "SshRprxBackend",
           "get_proxy_backend"]


def get_proxy_backend() -> ProxyBackend:
    """Return the configured backend instance.

    Reads PROXY_BACKEND from the environment. Construction reads each
    backend's own env vars internally; see local_nginx.py and ssh_rprx.py
    for the per-backend variables.

    Raises ValueError if PROXY_BACKEND has an unknown value — fail fast
    rather than silently fall back, since misconfiguration here means
    every tenant provisioning silently does the wrong thing.
    """
    name = os.environ.get("PROXY_BACKEND", "local").strip().lower()
    if name == "local":
        return LocalNginxBackend()
    if name in ("ssh-rprx", "ssh_rprx", "rprx"):
        return SshRprxBackend()
    raise ValueError(
        f"Unknown PROXY_BACKEND={name!r}. "
        "Valid: 'local' (appliance/on-prem) or 'ssh-rprx' (HJMM production)."
    )
