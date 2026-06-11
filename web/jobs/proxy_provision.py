"""
jobs/proxy_provision.py
Praesidium Series 2.0 — Tenant Proxy Provisioning (Infra Path)

RQ job that materializes a tenant's HTTPS endpoint via the configured
ProxyBackend (LocalNginxBackend or SshRprxBackend). Runs after
jobs/provision_tenant.py has finished its data work.

This is a SEPARATE job from provision_tenant for two reasons:

  1. Different failure semantics. Data work either succeeds atomically
     (single DB transaction logical group) or it doesn't. Infra work can
     fail in mid-flight and benefit from independent retry — e.g. proxy
     daemon momentarily unreachable. Splitting into two jobs lets the
     wizard re-enqueue ONLY the failed step rather than re-running the
     entire provisioning pipeline.

  2. Different operational ownership. On the appliance both run in the
     same worker, but on production the data work runs against a worker
     with DB connectivity and the proxy step runs against a worker (or
     a future agent) with SSH credentials to RPRX-01. Two jobs map
     cleanly to two queues if needed.

Idempotency:
  - write_vhost overwrites an existing config with the new content.
  - reload is naturally idempotent.
  - On success: tenants.is_active flipped to True.
  - On failure: tenants.is_active stays False; the job result includes
    the error message; the wizard offers a retry button that re-enqueues
    THIS job only (no data work re-runs).

⚖  PATENT NOTICE: Patent Pending — 64/020,027
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import psycopg2

log = logging.getLogger("praesidium.jobs.proxy_provision")


def _get_conn():
    """Same connection pattern as jobs/provision_tenant.py."""
    from jobs.provision_tenant import _parse_dsn  # reuse the parser
    raw = os.environ.get("DATABASE_URL_SYNC") or os.environ.get("DATABASE_URL", "")
    if not raw:
        raise RuntimeError("Neither DATABASE_URL_SYNC nor DATABASE_URL is set")
    conn = psycopg2.connect(_parse_dsn(raw))
    conn.autocommit = True
    return conn


def _resolve_cert_paths(
    domain: str, ssl_mode: str,
    ssl_cert_path: str | None, ssl_key_path: str | None,
) -> tuple[str, str]:
    """Pick the cert paths the proxy will read at runtime.

    For ssl_mode='letsencrypt': use the platform's wildcard cert. The
    cert files are managed by certbot (DNS-01 via DME plugin) and
    reside at /etc/letsencrypt/live/<base_domain>/.

    For ssl_mode='custom': use the explicit paths from the wizard upload.

    The base domain is derived by stripping the leftmost label from
    the hostname (acme.praesidium-legal.com → praesidium-legal.com).
    Override via env if you need something else (e.g. the appliance
    serves both *.hjmmlegal.com and *.praesidium-legal.com from
    different cert lineages).
    """
    if ssl_mode == "custom":
        if not ssl_cert_path or not ssl_key_path:
            raise ValueError("ssl_mode=custom requires ssl_cert_path and ssl_key_path")
        return ssl_cert_path, ssl_key_path

    # letsencrypt — find the matching certbot lineage by base domain.
    # Allow env override for unusual setups.
    override = os.environ.get("PROXY_LETSENCRYPT_BASE")
    if override:
        base = override
    else:
        # Default: strip the leftmost subdomain label.
        # acme.praesidium-legal.com → praesidium-legal.com
        # acme.foo.example.com      → foo.example.com (leftmost only)
        parts = domain.split(".")
        base = ".".join(parts[1:]) if len(parts) > 2 else domain

    return (
        f"/etc/letsencrypt/live/{base}/fullchain.pem",
        f"/etc/letsencrypt/live/{base}/privkey.pem",
    )


def provision_proxy(payload: dict) -> dict:
    """RQ job entry point.

    Required payload keys:
        tenant_id, slug, domain, ssl_mode
    Optional:
        ssl_cert_path, ssl_key_path  (required if ssl_mode='custom')
        upstream_host (default from env PROXY_UPSTREAM_HOST or 172.28.1.3)
        upstream_port (default from env PROXY_UPSTREAM_PORT or 8000)
        triggered_by  (default 'wizard')
    """
    tenant_id = payload["tenant_id"]
    slug = payload["slug"]
    domain = payload["domain"]
    ssl_mode = payload.get("ssl_mode", "letsencrypt")
    ssl_cert_path = payload.get("ssl_cert_path")
    ssl_key_path = payload.get("ssl_key_path")
    upstream_host = payload.get("upstream_host") or os.environ.get(
        "PROXY_UPSTREAM_HOST", "172.28.1.3"
    )
    upstream_port = int(payload.get("upstream_port") or os.environ.get(
        "PROXY_UPSTREAM_PORT", "8000"
    ))
    triggered_by = payload.get("triggered_by", "wizard")

    log.info("provision_proxy: starting tenant_id=%s slug=%s domain=%s",
             tenant_id, slug, domain)

    try:
        cert_path, key_path = _resolve_cert_paths(
            domain, ssl_mode, ssl_cert_path, ssl_key_path
        )
    except Exception as exc:
        log.error("provision_proxy: cert path resolution failed: %s", exc)
        return {"status": "failed", "slug": slug, "step": "resolve_certs",
                "error": str(exc)}

    # Backend selection happens here; lazy import so a missing dep on
    # one backend doesn't kill the whole module.
    try:
        from infra.proxy import get_proxy_backend
        backend = get_proxy_backend()
    except Exception as exc:
        log.error("provision_proxy: backend init failed: %s", exc)
        return {"status": "failed", "slug": slug, "step": "backend_init",
                "error": str(exc)}

    # Health check before mutating anything. If the proxy is dead, we
    # don't want to half-write a config and signal a reload that fails.
    ok, msg = backend.health_check()
    if not ok:
        log.error("provision_proxy: backend health check failed: %s", msg)
        return {"status": "failed", "slug": slug, "step": "health_check",
                "error": msg}

    # Write vhost.
    ok, msg = backend.write_vhost(
        slug=slug,
        domain=domain,
        ssl_mode=ssl_mode,
        cert_path=cert_path,
        key_path=key_path,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
    )
    if not ok:
        log.error("provision_proxy: write_vhost failed: %s", msg)
        return {"status": "failed", "slug": slug, "step": "write_vhost",
                "error": msg}

    # Reload.
    ok, msg = backend.reload()
    if not ok:
        log.error("provision_proxy: reload failed: %s", msg)
        # Best effort: leave the vhost file in place; the next retry
        # will overwrite it. We don't remove it because that might
        # disturb a previously-working state.
        return {"status": "failed", "slug": slug, "step": "reload",
                "error": msg}

    # Flip is_active=True now that the tenant is reachable.
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tenants SET is_active = true, updated_at = %s "
                "WHERE id = %s",
                (datetime.now(timezone.utc), tenant_id),
            )
        conn.close()
        log.info("provision_proxy: tenant_id=%s is_active=true", tenant_id)
    except Exception as exc:
        log.error("provision_proxy: failed to flip is_active: %s", exc)
        # The proxy step itself succeeded — the tenant is reachable.
        # Returning a warning is more honest than a failure.
        return {
            "status": "complete_with_warnings",
            "slug": slug,
            "domain": domain,
            "warnings": [f"is_active_update_failed: {exc}"],
        }

    return {
        "status": "complete",
        "slug": slug,
        "tenant_id": tenant_id,
        "domain": domain,
        "vhost_written": True,
        "reloaded": True,
    }
