"""
modules/admin/provisioning_wizard.py
Praesidium Series 2.0 — Tenant Provisioning Wizard

Routes the platform admin through a 5-step provisioning flow and
enqueues the data-path job (jobs/provision_tenant.py) on confirmation.
The data path enqueues the proxy step (jobs/proxy_provision.py) when
its DB work succeeds; the wizard's progress page polls until the
proxy job also reports complete.

Routes:
  GET  /admin/provision/new                    → step 1 form
  POST /admin/provision/step1                  → validate + advance
  POST /admin/provision/step2                  → validate domain/SSL
  POST /admin/provision/step3                  → validate cert upload (custom only)
  POST /admin/provision/confirm                → render summary
  POST /admin/provision/execute                → enqueue data job, redirect
  GET  /admin/provision/progress/{job_id}      → HTMX poll progress
  POST /admin/provision/retry-proxy/{tenant_id} → re-enqueue proxy step only

Schema-correct since 2026-04-29 — see jobs/provision_tenant.py docstring.

⚖  PATENT NOTICE: Patent Pending — 64/020,027
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from redis import Redis
from rq import Queue
from rq.job import Job
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.admin.admin_panel import require_admin_session

log = logging.getLogger("praesidium.modules.admin.provisioning_wizard")

router = APIRouter(prefix="/admin/provision", tags=["admin-provision"])

REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")

# UI-facing choices. These are presentation labels mapped to wizard inputs.
FEATURE_PACK_CHOICES = ["intelligence", "standard", "starter"]
DEPLOYMENT_TIER_CHOICES = ["dedicated", "isolated", "shared"]
CHANNEL_CHOICES = ["founder", "alpha", "beta", "ga", "none"]
SSL_MODES = ["letsencrypt", "custom"]

# Default tenant subdomain suffix. Pulled from env so cloud installs
# can override (e.g. PLATFORM_DOMAIN=praesidium.legal in cloud, vs.
# praesidium-legal.com on the appliance).
PLATFORM_DOMAIN = os.environ.get("PLATFORM_DOMAIN", "praesidium-legal.com")


def _templates(request: Request):
    return request.app.state.templates


def _get_redis() -> Redis:
    return Redis.from_url(REDIS_URL)


# ── Step 1 — Basic Info ─────────────────────────────────────────────────────


@router.get("/new", response_class=HTMLResponse)
async def provision_new(request: Request, _=Depends(require_admin_session)):
    t = _templates(request)
    return t.TemplateResponse(
        request,
        "admin/provision_wizard.html",
        {
            "step": 1,
            "feature_pack_choices": FEATURE_PACK_CHOICES,
            "deployment_tier_choices": DEPLOYMENT_TIER_CHOICES,
            "channel_choices": CHANNEL_CHOICES,
            "platform_domain": PLATFORM_DOMAIN,
            "errors": {},
            "form": {},
        },
    )


@router.post("/step1", response_class=HTMLResponse)
async def provision_step1(
    request: Request,
    slug: Annotated[str, Form()],
    firm_name: Annotated[str, Form()],
    feature_pack: Annotated[str, Form()],
    deployment_tier: Annotated[str, Form()],
    deployment_channel: Annotated[str, Form()],
    _=Depends(require_admin_session),
):

    errors: dict[str, str] = {}
    slug = slug.strip().lower().replace(" ", "-")

    if not slug or len(slug) < 2:
        errors["slug"] = "Slug must be at least 2 characters."
    if not all(c.isalnum() or c in "-" for c in slug):
        errors["slug"] = "Slug may contain only letters, digits, and hyphens."
    if not firm_name.strip():
        errors["firm_name"] = "Firm name is required."
    if feature_pack not in FEATURE_PACK_CHOICES:
        errors["feature_pack"] = "Invalid feature pack."
    if deployment_tier not in DEPLOYMENT_TIER_CHOICES:
        errors["deployment_tier"] = "Invalid deployment tier."
    if deployment_channel not in CHANNEL_CHOICES:
        errors["deployment_channel"] = "Invalid channel."

    # Slug uniqueness check.
    if not errors:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT 1 FROM tenants WHERE slug = :slug"),
                {"slug": slug},
            )
            if result.first():
                errors["slug"] = f"Slug '{slug}' is already in use."

    t = _templates(request)
    form_data = {
        "slug": slug,
        "firm_name": firm_name,
        "feature_pack": feature_pack,
        "deployment_tier": deployment_tier,
        "deployment_channel": deployment_channel,
    }

    if errors:
        return t.TemplateResponse(
            request,
            "admin/provision_wizard.html",
            {
                "step": 1,
                "feature_pack_choices": FEATURE_PACK_CHOICES,
                "deployment_tier_choices": DEPLOYMENT_TIER_CHOICES,
                "channel_choices": CHANNEL_CHOICES,
                "platform_domain": PLATFORM_DOMAIN,
                "errors": errors,
                "form": form_data,
            },
        )

    form_data.update({
        "domain": f"{slug}.{PLATFORM_DOMAIN}",
        "ssl_mode": "letsencrypt",
    })
    return t.TemplateResponse(
        request,
        "admin/provision_wizard.html",
        {
            "step": 2,
            "feature_pack_choices": FEATURE_PACK_CHOICES,
            "deployment_tier_choices": DEPLOYMENT_TIER_CHOICES,
            "channel_choices": CHANNEL_CHOICES,
            "platform_domain": PLATFORM_DOMAIN,
            "errors": {},
            "form": form_data,
        },
    )


# ── Step 2 — Domain & SSL Mode ──────────────────────────────────────────────


@router.post("/step2", response_class=HTMLResponse)
async def provision_step2(
    request: Request,
    slug: Annotated[str, Form()],
    firm_name: Annotated[str, Form()],
    feature_pack: Annotated[str, Form()],
    deployment_tier: Annotated[str, Form()],
    deployment_channel: Annotated[str, Form()],
    domain: Annotated[str, Form()],
    ssl_mode: Annotated[str, Form()],
    _=Depends(require_admin_session),
):

    errors: dict[str, str] = {}
    domain = domain.strip().lower()
    if not domain or "." not in domain:
        errors["domain"] = "Domain is required and must be a valid hostname."
    if ssl_mode not in SSL_MODES:
        errors["ssl_mode"] = "Invalid SSL mode."

    t = _templates(request)
    form_data = {
        "slug": slug,
        "firm_name": firm_name,
        "feature_pack": feature_pack,
        "deployment_tier": deployment_tier,
        "deployment_channel": deployment_channel,
        "domain": domain,
        "ssl_mode": ssl_mode,
    }

    if errors:
        return t.TemplateResponse(
            request,
            "admin/provision_wizard.html",
            {
                "step": 2,
                "feature_pack_choices": FEATURE_PACK_CHOICES,
                "deployment_tier_choices": DEPLOYMENT_TIER_CHOICES,
                "channel_choices": CHANNEL_CHOICES,
                "platform_domain": PLATFORM_DOMAIN,
                "errors": errors,
                "form": form_data,
            },
        )

    next_step = 3 if ssl_mode == "custom" else 4

    from jobs.provision_tenant import TIER_PRESETS
    feature_flags = TIER_PRESETS.get(feature_pack, TIER_PRESETS["starter"]).copy()

    return t.TemplateResponse(
        request,
        "admin/provision_wizard.html",
        {
            "step": next_step,
            "feature_pack_choices": FEATURE_PACK_CHOICES,
            "deployment_tier_choices": DEPLOYMENT_TIER_CHOICES,
            "channel_choices": CHANNEL_CHOICES,
            "platform_domain": PLATFORM_DOMAIN,
            "errors": {},
            "form": form_data,
            "feature_flags": feature_flags,
        },
    )


# ── Step 3 — Custom Cert Upload ─────────────────────────────────────────────


@router.post("/step3", response_class=HTMLResponse)
async def provision_step3(
    request: Request,
    slug: Annotated[str, Form()],
    firm_name: Annotated[str, Form()],
    feature_pack: Annotated[str, Form()],
    deployment_tier: Annotated[str, Form()],
    deployment_channel: Annotated[str, Form()],
    domain: Annotated[str, Form()],
    ssl_mode: Annotated[str, Form()],
    cert_file: UploadFile = File(None),
    key_file: UploadFile = File(None),
    _=Depends(require_admin_session),
):

    errors: dict[str, str] = {}
    cert_bytes: bytes | None = None
    key_bytes: bytes | None = None

    if ssl_mode == "custom":
        if not cert_file or not cert_file.filename:
            errors["cert_file"] = "Certificate file is required for custom SSL."
        if not key_file or not key_file.filename:
            errors["key_file"] = "Private key file is required for custom SSL."
        if not errors:
            cert_bytes = await cert_file.read()
            key_bytes = await key_file.read()
            if not cert_bytes.startswith(b"-----BEGIN"):
                errors["cert_file"] = "File does not appear to be a valid PEM certificate."
            if not key_bytes.startswith(b"-----BEGIN"):
                errors["key_file"] = "File does not appear to be a valid PEM private key."

    t = _templates(request)
    form_data = {
        "slug": slug,
        "firm_name": firm_name,
        "feature_pack": feature_pack,
        "deployment_tier": deployment_tier,
        "deployment_channel": deployment_channel,
        "domain": domain,
        "ssl_mode": ssl_mode,
    }

    if errors:
        return t.TemplateResponse(
            request,
            "admin/provision_wizard.html",
            {
                "step": 3,
                "feature_pack_choices": FEATURE_PACK_CHOICES,
                "deployment_tier_choices": DEPLOYMENT_TIER_CHOICES,
                "channel_choices": CHANNEL_CHOICES,
                "platform_domain": PLATFORM_DOMAIN,
                "errors": errors,
                "form": form_data,
            },
        )

    if cert_bytes and key_bytes:
        r = _get_redis()
        r.setex(f"provision:cert:{slug}", 600, cert_bytes)
        r.setex(f"provision:key:{slug}", 600, key_bytes)

    from jobs.provision_tenant import TIER_PRESETS
    feature_flags = TIER_PRESETS.get(feature_pack, TIER_PRESETS["starter"]).copy()

    return t.TemplateResponse(
        request,
        "admin/provision_wizard.html",
        {
            "step": 4,
            "feature_pack_choices": FEATURE_PACK_CHOICES,
            "deployment_tier_choices": DEPLOYMENT_TIER_CHOICES,
            "channel_choices": CHANNEL_CHOICES,
            "platform_domain": PLATFORM_DOMAIN,
            "errors": {},
            "form": form_data,
            "feature_flags": feature_flags,
        },
    )


# ── Confirm ─────────────────────────────────────────────────────────────────


@router.post("/confirm", response_class=HTMLResponse)
async def provision_confirm(request: Request, _=Depends(require_admin_session)):

    form = await request.form()

    feature_pack = str(form.get("feature_pack", "intelligence"))
    from jobs.provision_tenant import TIER_PRESETS
    base_flags = TIER_PRESETS.get(feature_pack, TIER_PRESETS["starter"]).copy()

    feature_overrides: dict[str, bool] = {}
    for key in base_flags:
        feature_overrides[key] = f"feature_{key}" in form

    first_user_password = str(form.get("first_user_password", "")).strip()
    if not first_user_password:
        first_user_password = secrets.token_urlsafe(12)

    t = _templates(request)
    return t.TemplateResponse(
        request,
        "admin/provision_confirm.html",
        {
            "form": {
                "slug": str(form.get("slug", "")).strip(),
                "firm_name": str(form.get("firm_name", "")).strip(),
                "feature_pack": feature_pack,
                "deployment_tier": str(form.get("deployment_tier", "dedicated")),
                "deployment_channel": str(form.get("deployment_channel", "none")),
                "domain": str(form.get("domain", "")),
                "ssl_mode": str(form.get("ssl_mode", "letsencrypt")),
                "first_user_email": str(form.get("first_user_email", "")).strip(),
                "first_user_password": first_user_password,
                "feature_overrides": feature_overrides,
            },
            "feature_overrides": feature_overrides,
        },
    )


# ── Execute — Enqueue RQ Job ────────────────────────────────────────────────


@router.post("/execute", response_class=HTMLResponse)
async def provision_execute(request: Request, _=Depends(require_admin_session)):

    form = await request.form()
    slug = str(form.get("slug", "")).strip()

    feature_overrides_raw = str(form.get("feature_overrides_json", "{}"))
    try:
        feature_overrides = json.loads(feature_overrides_raw)
    except Exception:
        feature_overrides = {}

    ssl_mode = str(form.get("ssl_mode", "letsencrypt"))
    ssl_cert_path: str | None = None
    ssl_key_path: str | None = None

    # If custom SSL: stash the uploaded bytes to disk via a separate
    # fast job (jobs/ssl_provision.py) before the data path runs. The
    # data path writes ssl_cert_path/ssl_key_path to the tenants row;
    # the proxy step reads them.
    if ssl_mode == "custom":
        r = _get_redis()
        cert_bytes = r.get(f"provision:cert:{slug}")
        key_bytes = r.get(f"provision:key:{slug}")
        if cert_bytes and key_bytes:
            from jobs.ssl_provision import provision_custom_cert
            ssl_q = Queue("default", connection=r)
            ssl_q.enqueue(
                provision_custom_cert,
                slug=slug,
                cert_pem=cert_bytes,
                key_pem=key_bytes,
                job_timeout=120,
            )
            # Sentinel paths the proxy step will read after the cert
            # provision job lands them. Both jobs share the same hostfs
            # path convention.
            ssl_cert_path = f"/etc/praesidium/certs/{slug}/fullchain.pem"
            ssl_key_path = f"/etc/praesidium/certs/{slug}/privkey.pem"

    payload = {
        "slug": slug,
        "firm_name": str(form.get("firm_name", "")).strip(),
        "feature_pack": str(form.get("feature_pack", "intelligence")),
        "deployment_tier": str(form.get("deployment_tier", "dedicated")),
        "deployment_channel": str(form.get("deployment_channel", "none")),
        "domain": str(form.get("domain", "")),
        "ssl_mode": ssl_mode,
        "ssl_cert_path": ssl_cert_path,
        "ssl_key_path": ssl_key_path,
        "first_user_email": str(form.get("first_user_email", "")).strip(),
        "first_user_password": str(form.get("first_user_password", "")).strip(),
        "feature_overrides": feature_overrides,
        "triggered_by": "platform-admin",
    }

    r = _get_redis()
    q = Queue("default", connection=r)
    from jobs.provision_tenant import provision_tenant
    job = q.enqueue(provision_tenant, payload, job_timeout=300)

    return RedirectResponse(f"/admin/provision/progress/{job.id}", status_code=303)


# ── Progress Polling (HTMX) ─────────────────────────────────────────────────


@router.get("/progress/{job_id}", response_class=HTMLResponse)
async def provision_progress(
    request: Request, job_id: str, _=Depends(require_admin_session)
):

    r = _get_redis()
    try:
        data_job = Job.fetch(job_id, connection=r)
        data_status = data_job.get_status()
        data_result = data_job.result if data_status == "finished" else None
        data_error = str(data_job.exc_info) if data_status == "failed" else None
    except Exception as exc:
        data_status = "unknown"
        data_result = None
        data_error = str(exc)

    # If the data job finished and produced a proxy_job_id, fetch that too.
    proxy_status: str | None = None
    proxy_result = None
    proxy_error = None
    proxy_job_id = (data_result or {}).get("proxy_job_id") if data_result else None
    if proxy_job_id:
        try:
            proxy_job = Job.fetch(proxy_job_id, connection=r)
            proxy_status = proxy_job.get_status()
            proxy_result = proxy_job.result if proxy_status == "finished" else None
            proxy_error = str(proxy_job.exc_info) if proxy_status == "failed" else None
        except Exception as exc:
            proxy_status = "unknown"
            proxy_error = str(exc)

    t = _templates(request)
    return t.TemplateResponse(
        request,
        "admin/provision_progress.html",
        {
            "job_id": job_id,
            "data_status": data_status,
            "data_result": data_result,
            "data_error": data_error,
            "proxy_job_id": proxy_job_id,
            "proxy_status": proxy_status,
            "proxy_result": proxy_result,
            "proxy_error": proxy_error,
        },
    )


# ── Retry only the proxy step (idempotent) ──────────────────────────────────


@router.post("/retry-proxy/{tenant_id}", response_class=HTMLResponse)
async def provision_retry_proxy(
    request: Request,
    tenant_id: str,
    _=Depends(require_admin_session),
):
    """Re-enqueue ONLY the proxy step for a tenant whose data step
    already succeeded. Used when the proxy step failed (daemon down,
    cert path wrong, etc.) and the operator wants to retry without
    rerunning the data work."""

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT slug, domain, ssl_mode, ssl_cert_path, ssl_key_path "
                "FROM tenants WHERE id = :tid"
            ),
            {"tid": tenant_id},
        )
        row = result.first()

    if row is None:
        return HTMLResponse(
            f"<p>Tenant {tenant_id} not found.</p>",
            status_code=404,
        )

    payload = {
        "tenant_id": tenant_id,
        "slug": row.slug,
        "domain": row.domain,
        "ssl_mode": row.ssl_mode or "letsencrypt",
        "ssl_cert_path": row.ssl_cert_path,
        "ssl_key_path": row.ssl_key_path,
        "triggered_by": f"retry:{getattr(user, 'email', 'platform-admin')}",
    }

    r = _get_redis()
    q = Queue("default", connection=r)
    from jobs.proxy_provision import provision_proxy
    job = q.enqueue(provision_proxy, payload, job_timeout=300)

    return RedirectResponse(f"/admin/provision/progress/{job.id}", status_code=303)
