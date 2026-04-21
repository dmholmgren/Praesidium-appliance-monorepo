"""
jobs/provision_tenant.py
RQ job — Tenant Provisioning
Runs async after wizard confirmation. Creates:
  1. tenants DB record
  2. Feature flag defaults for tier
  3. Branding defaults
  4. Storage directory on FBRG-01 via CIFS bridge
  5. First user (bcrypt hashed, must_change_password=True)
  6. Nginx server block on RPRX-01 via SSH subprocess
  7. Marks provision_status = 'complete'
"""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
import uuid
from datetime import datetime, timezone

import bcrypt
import httpx
import psycopg2

log = logging.getLogger(__name__)

CIFS_BRIDGE_URL = os.environ.get("CIFS_BRIDGE_URL", "http://10.10.60.13:8080")
RPRX_HOST = os.environ.get("RPRX_HOST", "10.10.40.50")
DATABASE_URL_SYNC = os.environ.get("DATABASE_URL_SYNC", "")  # direct psycopg2 URL
NGINX_SITES_DIR = "/etc/nginx/sites-available"

# Tier → feature flag presets  (feature_key: enabled bool)
TIER_PRESETS: dict[str, dict[str, bool]] = {
    "intelligence": {
        "ediscovery": True,
        "predictive_coding": True,
        "email_threading": True,
        "near_duplicate": True,
        "pleading_analysis": True,
        "kg_extraction": True,
        "cite_it": True,
        "trial_desk": True,
        "ai_timesheet": True,
        "billing_import": True,
        "document_review": True,
        "collection_drop_zone": True,
        "production_import": True,
        "byok": True,
        "custom_branding": True,
        "white_label": True,
    },
    "standard": {
        "ediscovery": True,
        "predictive_coding": False,
        "email_threading": True,
        "near_duplicate": True,
        "pleading_analysis": False,
        "kg_extraction": False,
        "cite_it": False,
        "trial_desk": False,
        "ai_timesheet": True,
        "billing_import": True,
        "document_review": True,
        "collection_drop_zone": True,
        "production_import": True,
        "byok": False,
        "custom_branding": False,
        "white_label": False,
    },
    "starter": {
        "ediscovery": False,
        "predictive_coding": False,
        "email_threading": False,
        "near_duplicate": False,
        "pleading_analysis": False,
        "kg_extraction": False,
        "cite_it": False,
        "trial_desk": False,
        "ai_timesheet": False,
        "billing_import": True,
        "document_review": False,
        "collection_drop_zone": False,
        "production_import": False,
        "byok": False,
        "custom_branding": False,
        "white_label": False,
    },
}


def _get_conn(dsn: str):
    """Return a direct psycopg2 connection with autocommit."""
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn


def _parse_dsn(database_url: str) -> str:
    """
    Convert asyncpg URL to psycopg2-compatible DSN.
    Handles passwords containing @ by using rfind.
    """
    url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    at = url.rfind("@")
    credentials = url[len("postgresql://") : at]
    host_db = url[at + 1 :]
    colon = credentials.rfind(":")
    user = credentials[:colon]
    password = credentials[colon + 1 :]
    slash = host_db.rfind("/")
    host_port = host_db[:slash]
    dbname = host_db[slash + 1 :]
    if ":" in host_port:
        host, port = host_port.rsplit(":", 1)
    else:
        host, port = host_port, "5432"
    return f"host={host} port={port} dbname={dbname} user={user} password={password}"


def _create_storage_dir(slug: str) -> bool:
    """Ask CIFS bridge to create /mnt/praesidium/{slug}/."""
    try:
        resp = httpx.post(
            f"{CIFS_BRIDGE_URL}/storage/mkdir",
            json={"path": f"praesidium/{slug}"},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.error("Storage mkdir failed for %s: %s", slug, exc)
        return False


def _write_nginx_block(slug: str, domain: str, ssl_mode: str, ssl_cert_path: str | None, ssl_key_path: str | None) -> bool:
    """
    Write nginx server block for this tenant.
    letsencrypt → wildcard cert at /etc/letsencrypt/live/praesidium-legal.com/
    custom       → paths from ssl_cert_path / ssl_key_path
    """
    if ssl_mode == "letsencrypt":
        cert = "/etc/letsencrypt/live/praesidium-legal.com/fullchain.pem"
        key = "/etc/letsencrypt/live/praesidium-legal.com/privkey.pem"
    else:
        cert = ssl_cert_path or ""
        key = ssl_key_path or ""

    block = f"""# Praesidium tenant: {slug}
server {{
    listen 443 ssl http2;
    server_name {domain};

    ssl_certificate     {cert};
    ssl_certificate_key {key};
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    location / {{
        proxy_pass         http://10.10.60.10:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;
    }}
}}

server {{
    listen 80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}
"""
    conf_path = f"{NGINX_SITES_DIR}/{slug}.conf"
    enabled_path = f"/etc/nginx/sites-enabled/{slug}.conf"
    try:
        # Write via SSH to RPRX-01 using tee (requires passwordless sudo from app host)
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                f"praesidium@{RPRX_HOST}",
                f"sudo tee {conf_path} > /dev/null && "
                f"sudo ln -sf {conf_path} {enabled_path} && "
                f"sudo nginx -t && sudo systemctl reload nginx",
            ],
            input=block.encode(),
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            log.error("nginx block write failed: %s", proc.stderr.decode())
            return False
        return True
    except Exception as exc:
        log.error("nginx SSH error: %s", exc)
        return False


def provision_tenant(payload: dict) -> dict:
    """
    Main RQ entry point.

    payload keys:
      slug, firm_name, tier, deployment_channel,
      domain, ssl_mode, ssl_cert_path, ssl_key_path,
      first_user_email, first_user_password,
      feature_overrides (dict of feature_key -> bool, may be empty)
    """
    slug = payload["slug"]
    firm_name = payload["firm_name"]
    tier = payload.get("tier", "intelligence")
    deployment_channel = payload.get("deployment_channel", "none")
    domain = payload.get("domain") or f"{slug}.praesidium-legal.com"
    ssl_mode = payload.get("ssl_mode", "letsencrypt")
    ssl_cert_path = payload.get("ssl_cert_path")
    ssl_key_path = payload.get("ssl_key_path")
    first_user_email = payload["first_user_email"]
    first_user_password = payload["first_user_password"]
    feature_overrides: dict[str, bool] = payload.get("feature_overrides", {})

    errors: list[str] = []

    dsn = _parse_dsn(DATABASE_URL_SYNC or os.environ.get("DATABASE_URL", ""))
    conn = _get_conn(dsn)
    cur = conn.cursor()

    try:
        # ── 1. Insert tenant record ──────────────────────────────────────
        tenant_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        cur.execute(
            """
            INSERT INTO tenants (
                id, slug, firm_name, tier, deployment_channel,
                is_active, ssl_mode, ssl_cert_path, ssl_key_path, ssl_domain,
                provision_status, created_at, updated_at
            ) VALUES (%s,%s,%s,%s,%s, true,%s,%s,%s,%s, 'provisioning',%s,%s)
            ON CONFLICT (slug) DO NOTHING
            """,
            (
                tenant_id, slug, firm_name, tier, deployment_channel,
                ssl_mode, ssl_cert_path, ssl_key_path, domain,
                now, now,
            ),
        )
        log.info("Tenant record inserted: %s (%s)", slug, tenant_id)

        # ── 2. Feature flags ─────────────────────────────────────────────
        preset = TIER_PRESETS.get(tier, TIER_PRESETS["starter"]).copy()
        preset.update(feature_overrides)  # apply wizard overrides
        for feature_key, enabled in preset.items():
            cur.execute(
                """
                INSERT INTO feature_overrides (tenant_id, feature_key, enabled, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, feature_key) DO UPDATE
                  SET enabled = EXCLUDED.enabled, updated_at = EXCLUDED.updated_at
                """,
                (tenant_id, feature_key, enabled, now, now),
            )
        log.info("Feature flags seeded for %s (%d features)", slug, len(preset))

        # ── 3. Branding defaults ─────────────────────────────────────────
        cur.execute(
            """
            INSERT INTO tenant_branding (
                tenant_id, platform_name, platform_short_name,
                suppress_attribution, base_domain,
                use_praesidium_subdomain, created_at, updated_at
            ) VALUES (%s,%s,%s, false,%s, %s,%s,%s)
            ON CONFLICT (tenant_id) DO NOTHING
            """,
            (
                tenant_id, firm_name, firm_name[:20],
                domain,
                (ssl_mode == "letsencrypt"),
                now, now,
            ),
        )
        log.info("Branding defaults written for %s", slug)

        # ── 4. Storage directory ─────────────────────────────────────────
        if not _create_storage_dir(slug):
            errors.append("storage_dir_failed")
            log.warning("Storage dir not created for %s — continuing", slug)

        # ── 5. First user ────────────────────────────────────────────────
        pw_hash = bcrypt.hashpw(
            first_user_password.encode(), bcrypt.gensalt()
        ).decode()
        cur.execute(
            """
            INSERT INTO users (
                tenant_id, email, hashed_password, role,
                is_active, must_change_password, created_at, updated_at
            ) VALUES (%s,%s,%s,'admin', true, true,%s,%s)
            ON CONFLICT (tenant_id, email) DO NOTHING
            """,
            (tenant_id, first_user_email, pw_hash, now, now),
        )
        log.info("First admin user seeded: %s", first_user_email)

        # ── 6. Nginx block ───────────────────────────────────────────────
        nginx_ok = _write_nginx_block(slug, domain, ssl_mode, ssl_cert_path, ssl_key_path)
        if not nginx_ok:
            errors.append("nginx_block_failed")
            log.warning("Nginx block not written for %s — manual step required", slug)

        # ── 7. Mark complete ─────────────────────────────────────────────
        status = "complete" if not errors else "complete_with_warnings"
        cur.execute(
            "UPDATE tenants SET provision_status=%s, updated_at=%s WHERE id=%s",
            (status, datetime.now(timezone.utc), tenant_id),
        )
        log.info("Tenant %s provisioned — status: %s", slug, status)

        return {
            "tenant_id": tenant_id,
            "slug": slug,
            "status": status,
            "errors": errors,
        }

    except Exception as exc:
        log.exception("Provisioning failed for %s: %s", slug, exc)
        try:
            cur.execute(
                "UPDATE tenants SET provision_status='failed', updated_at=%s WHERE slug=%s",
                (datetime.now(timezone.utc), slug),
            )
        except Exception:
            pass
        return {"slug": slug, "status": "failed", "errors": [str(exc)]}
    finally:
        cur.close()
        conn.close()
