"""
jobs/provision_tenant.py
Praesidium Series 2.0 — Tenant Provisioning (Data Path)

RQ job invoked by the admin provisioning wizard. Performs the data-layer
work of standing up a new tenant:

  1. Insert tenants row (is_active=False until proxy step succeeds)
  2. Insert tenant_branding row
  3. Insert tenant_licenses row (feature flags from wizard)
  4. Provision storage root via STORAGE_BACKEND
  5. Insert first admin user
  6. Enqueue proxy_provision job (separate RQ job, separate failure semantics)
  7. Return job result dict; orchestrator polls until proxy step also completes

The proxy/infra work is delegated entirely to jobs/proxy_provision.py +
infra/proxy/. That separation lets the same wizard work on:
  - The appliance              (PROXY_BACKEND=local, STORAGE_BACKEND=local)
  - HJMM production            (PROXY_BACKEND=ssh-rprx, STORAGE_BACKEND=cifs)
  - On-prem multi-tenant test  (PROXY_BACKEND=local, STORAGE_BACKEND=local)
  - Future cloud installs      (whatever combination)

Idempotent retry semantics:
  - tenants row is inserted with is_active=False ("provisioning" state).
  - On the proxy step succeeding, is_active is flipped to True.
  - If the proxy step fails, the tenant row stays at is_active=False.
    The wizard's retry control re-enqueues only proxy_provision (the data
    work is already done). When proxy succeeds, is_active flips to True.

Schema notes (validated against live DB 2026-04-29):
  - tenants.tier is enum tenant_tier ('shared'|'isolated'|'dedicated')
  - tenants.status is enum tenant_status ('active'|'suspended'|'cancelled')
    — there is NO 'provisioning' value; we use is_active=False instead.
  - tenants.feature_flags is jsonb on the tenants row itself, but
    runtime feature checks read tenant_licenses.feature_flags. We
    populate both for back-compat with the licensing checker.
  - users.password_hash (NOT hashed_password)
  - users requires username + full_name; we derive both from email.
  - There is NO 'must_change_password' column. First-login password
    change is handled via the invitation_token flow.

⚖  PATENT NOTICE: Patent Pending — 64/020,027
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import psycopg2
import psycopg2.extras

log = logging.getLogger("praesidium.jobs.provision_tenant")


# ── Tier → feature flag presets ──────────────────────────────────────────────
# Names match the canonical flags persisted in tenant_licenses.feature_flags
# in production today (verified 2026-04-29). Do NOT prefix with 'feature_'.

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


# ── DB connection helpers ────────────────────────────────────────────────────


def _parse_dsn(database_url: str) -> str:
    """Convert a SQLAlchemy asyncpg URL to a psycopg2 keyword DSN.

    Tolerates passwords containing '@' by using rfind for the credential
    separator. This is the same pattern used elsewhere in the codebase
    (see core/db/base.py).
    """
    url = (
        database_url
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg2://", "postgresql://")
    )
    if not url.startswith("postgresql://"):
        raise ValueError(f"DATABASE_URL must be postgresql://: {url[:40]}...")

    rest = url[len("postgresql://"):]
    at = rest.rfind("@")
    if at < 0:
        raise ValueError("DATABASE_URL missing credentials@host separator")
    creds = rest[:at]
    host_db = rest[at + 1:]

    colon = creds.rfind(":")
    if colon < 0:
        raise ValueError("DATABASE_URL credentials missing user:password")
    user, password = creds[:colon], creds[colon + 1:]

    slash = host_db.rfind("/")
    if slash < 0:
        raise ValueError("DATABASE_URL missing /dbname")
    host_port, dbname = host_db[:slash], host_db[slash + 1:]
    if ":" in host_port:
        host, port = host_port.rsplit(":", 1)
    else:
        host, port = host_port, "5432"

    return f"host={host} port={port} dbname={dbname} user={user} password={password}"


def _get_conn():
    """Return an autocommit psycopg2 connection."""
    raw = os.environ.get("DATABASE_URL_SYNC") or os.environ.get("DATABASE_URL", "")
    if not raw:
        raise RuntimeError("Neither DATABASE_URL_SYNC nor DATABASE_URL is set")
    conn = psycopg2.connect(_parse_dsn(raw))
    conn.autocommit = True
    return conn


# ── Helpers ──────────────────────────────────────────────────────────────────


def _username_from_email(email: str) -> str:
    """Derive a unique-enough username from an email local-part."""
    local = email.split("@", 1)[0].lower()
    # Strip anything that isn't safe in a username column.
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in local)
    return safe[:90]  # leave headroom under the 100-char limit


def _full_name_from_email(email: str) -> str:
    """Provisional full name. The user can edit this on first login."""
    local = email.split("@", 1)[0]
    parts = [p.capitalize() for p in local.replace(".", " ").replace("_", " ").split()]
    return " ".join(parts) or local


# ── Main job entry point ─────────────────────────────────────────────────────


def provision_tenant(payload: dict) -> dict:
    """RQ job entry point. See module docstring for payload contract.

    Required payload keys:
        slug, firm_name, domain, ssl_mode, first_user_email
    Optional:
        feature_pack ('intelligence' | 'standard' | 'starter') — default 'intelligence'
        deployment_tier ('shared' | 'isolated' | 'dedicated') — default 'dedicated'
        deployment_channel — default 'none'
        first_user_password — auto-generated if missing
        feature_overrides — dict of flag_name → bool
        ssl_cert_path, ssl_key_path — for ssl_mode='custom'
        triggered_by — string for audit (default 'wizard')
    """
    slug = payload["slug"]
    firm_name = payload["firm_name"]
    domain = payload.get("domain") or f"{slug}.praesidium-legal.com"
    ssl_mode = payload.get("ssl_mode", "letsencrypt")
    ssl_cert_path = payload.get("ssl_cert_path")
    ssl_key_path = payload.get("ssl_key_path")
    feature_pack = payload.get("feature_pack", "intelligence")
    deployment_tier = payload.get("deployment_tier", "dedicated")
    deployment_channel = payload.get("deployment_channel", "none")
    first_user_email = payload["first_user_email"]
    first_user_password = (
        payload.get("first_user_password")
        or secrets.token_urlsafe(12)
    )
    feature_overrides: dict[str, bool] = payload.get("feature_overrides") or {}
    triggered_by = payload.get("triggered_by", "wizard")

    warnings: list[str] = []
    tenant_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Compute the merged feature flag set.
    base_flags = TIER_PRESETS.get(feature_pack, TIER_PRESETS["starter"]).copy()
    base_flags.update(feature_overrides)

    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()

        # ── Step 1. Tenant row (is_active=False until proxy succeeds). ──
        cur.execute(
            """
            INSERT INTO tenants (
                id, name, slug, tier, plan, status, is_active,
                domain, deployment_channel, feature_flags,
                ssl_mode, ssl_cert_path, ssl_key_path, ssl_domain,
                storage_adapter, auth_adapter, email_adapter,
                calendar_adapter, research_providers, ai_provider,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s::tenant_tier, %s, 'active'::tenant_status, false,
                %s, %s, %s::jsonb,
                %s, %s, %s, %s,
                %s, 'local', 'none',
                'none', 'none', 'anthropic',
                %s, %s
            )
            ON CONFLICT (slug) DO NOTHING
            RETURNING id
            """,
            (
                tenant_id, firm_name, slug, deployment_tier, deployment_tier,
                domain, deployment_channel, json.dumps(base_flags),
                ssl_mode, ssl_cert_path, ssl_key_path, domain,
                # storage_adapter set via separate UPDATE after backend runs
                "local",
                now, now,
            ),
        )
        row = cur.fetchone()
        if row is None:
            # Slug already exists — fail loudly. The wizard validated this
            # at step 1 but a race is possible.
            return {
                "status": "failed",
                "slug": slug,
                "error": f"slug '{slug}' already in use (race condition)",
            }
        tenant_id = row[0]
        log.info("provision_tenant: tenants row created id=%s slug=%s", tenant_id, slug)

        # ── Step 2. Tenant branding ──────────────────────────────────────
        cur.execute(
            """
            INSERT INTO tenant_branding (
                tenant_id, firm_name, base_domain,
                platform_name, platform_short_name,
                suppress_attribution, use_praesidium_subdomain,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s,
                %s, %s,
                false, %s,
                %s, %s
            )
            ON CONFLICT (tenant_id) DO NOTHING
            """,
            (
                tenant_id, firm_name, domain,
                firm_name[:255], firm_name[:50],
                ssl_mode == "letsencrypt",
                now, now,
            ),
        )
        log.info("provision_tenant: tenant_branding row written")

        # ── Step 3. Tenant license (feature flags) ──────────────────────
        cur.execute(
            """
            INSERT INTO tenant_licenses (
                tenant_id, tier, feature_flags, billing_plan,
                licensed_at
            ) VALUES (%s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (tenant_id) DO UPDATE
              SET tier = EXCLUDED.tier,
                  feature_flags = EXCLUDED.feature_flags,
                  billing_plan = EXCLUDED.billing_plan
            """,
            (tenant_id, feature_pack, json.dumps(base_flags),
             deployment_tier, now),
        )
        log.info("provision_tenant: tenant_licenses row written (%d flags)",
                 len(base_flags))

        # ── Step 4. Storage provisioning ─────────────────────────────────
        # Lazy import so failed-load of an optional dep doesn't break the
        # entire job module.
        from infra.storage import get_storage_backend

        storage = get_storage_backend()
        ok, msg_or_adapter = storage.create_tenant_root(slug)
        if ok:
            adapter_name = msg_or_adapter
            cur.execute(
                "UPDATE tenants SET storage_adapter = %s, updated_at = %s "
                "WHERE id = %s",
                (adapter_name, datetime.now(timezone.utc), tenant_id),
            )
            log.info("provision_tenant: storage provisioned (%s)", adapter_name)
        else:
            warnings.append(f"storage_provisioning_failed: {msg_or_adapter}")
            log.warning("provision_tenant: storage provisioning failed: %s",
                        msg_or_adapter)

        # ── Step 5. First admin user ─────────────────────────────────────
        username = _username_from_email(first_user_email)
        full_name = _full_name_from_email(first_user_email)
        password_hash = bcrypt.hashpw(
            first_user_password.encode(), bcrypt.gensalt()
        ).decode()
        invitation_token = secrets.token_urlsafe(32)
        invitation_expires = now + timedelta(days=7)

        cur.execute(
            """
            INSERT INTO users (
                tenant_id, username, email, full_name, password_hash,
                role, is_active, is_timekeeper, auth_provider,
                invitation_token, invitation_expires_at,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                'admin'::user_role_enum, true, false, 'local',
                %s, %s,
                %s, %s
            )
            ON CONFLICT (tenant_id, email) DO NOTHING
            """,
            (
                tenant_id, username, first_user_email, full_name, password_hash,
                invitation_token, invitation_expires,
                now, now,
            ),
        )
        log.info("provision_tenant: first admin user created (%s)",
                 first_user_email)

        cur.close()
        conn.close()
        conn = None

    except Exception as exc:
        log.exception("provision_tenant: data path failed: %s", exc)
        # Best-effort cleanup: mark tenant as suspended so it doesn't
        # appear active in admin lists.
        try:
            if conn is not None:
                with conn.cursor() as ccur:
                    ccur.execute(
                        "UPDATE tenants SET status = 'suspended'::tenant_status, "
                        "is_active = false, updated_at = %s WHERE id = %s",
                        (datetime.now(timezone.utc), tenant_id),
                    )
        except Exception:
            pass
        return {
            "status": "failed",
            "slug": slug,
            "tenant_id": tenant_id,
            "error": str(exc),
        }
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # ── Step 6. Enqueue the proxy job (separate RQ job, separate retry) ──
    # The wizard's progress page tracks the proxy job's progress and
    # flips is_active=True when proxy succeeds.
    try:
        import redis as _redis
        from rq import Queue
        from jobs.proxy_provision import provision_proxy

        r = _redis.Redis.from_url(
            os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
        )
        q = Queue("default", connection=r)
        proxy_job = q.enqueue(
            provision_proxy,
            {
                "tenant_id": tenant_id,
                "slug": slug,
                "domain": domain,
                "ssl_mode": ssl_mode,
                "ssl_cert_path": ssl_cert_path,
                "ssl_key_path": ssl_key_path,
                "triggered_by": triggered_by,
            },
            job_timeout=300,
        )
        proxy_job_id = proxy_job.id
        log.info("provision_tenant: enqueued proxy job %s", proxy_job_id)
    except Exception as exc:
        warnings.append(f"proxy_enqueue_failed: {exc}")
        proxy_job_id = None
        log.error("provision_tenant: failed to enqueue proxy job: %s", exc)

    return {
        "status": "data_complete",
        "slug": slug,
        "tenant_id": tenant_id,
        "proxy_job_id": proxy_job_id,
        "first_user_email": first_user_email,
        "first_user_password": first_user_password,
        "invitation_token": invitation_token if 'invitation_token' in locals() else None,
        "warnings": warnings,
    }
