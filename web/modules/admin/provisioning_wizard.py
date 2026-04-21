"""
modules/admin/provisioning_wizard.py
GlassBreak — Tenant Provisioning Wizard (M8 C0e)

Routes:
  GET  /admin/provision/new          → Step 1 form
  POST /admin/provision/step1        → validate + advance to step 2
  POST /admin/provision/step2        → validate domain/SSL + advance to step 3
  POST /admin/provision/step3        → validate SSL upload + advance to step 4
  GET  /admin/provision/step4        → feature flags editor (HTMX partial)
  POST /admin/provision/confirm      → render summary confirmation page
  POST /admin/provision/execute      → enqueue RQ job, redirect to progress
  GET  /admin/provision/progress/{job_id}  → HTMX poll progress
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from typing import Annotated

import bcrypt
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from redis import Redis
from rq import Queue
from rq.job import Job

from core.db.base import get_session_factory
from modules.dashboard.services.auth_helper import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/provision", tags=["admin-provision"])

REDIS_URL = __import__("os").environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
TIER_CHOICES = ["intelligence", "standard", "starter"]
CHANNEL_CHOICES = ["founder", "alpha", "beta", "ga", "none"]


def _templates(request: Request):
    return request.app.state.templates


def _get_redis() -> Redis:
    return Redis.from_url(REDIS_URL)


# ── Step 1 — Basic Info ──────────────────────────────────────────────────────


@router.get("/new", response_class=HTMLResponse)
async def provision_new(request: Request, user=Depends(get_current_user)):
    if not getattr(user, "is_platform_admin", False):
        return RedirectResponse("/admin", status_code=303)
    t = _templates(request)
    return t.TemplateResponse(
        request,
        "admin/provision_wizard.html",
        {
            "step": 1,
            "tier_choices": TIER_CHOICES,
            "channel_choices": CHANNEL_CHOICES,
            "errors": {},
            "form": {},
        },
    )


@router.post("/step1", response_class=HTMLResponse)
async def provision_step1(
    request: Request,
    slug: Annotated[str, Form()],
    firm_name: Annotated[str, Form()],
    tier: Annotated[str, Form()],
    deployment_channel: Annotated[str, Form()],
    user=Depends(get_current_user),
):
    if not getattr(user, "is_platform_admin", False):
        return RedirectResponse("/admin", status_code=303)

    errors: dict[str, str] = {}
    slug = slug.strip().lower().replace(" ", "-")

    if not slug or len(slug) < 2:
        errors["slug"] = "Slug must be at least 2 characters."
    if not firm_name.strip():
        errors["firm_name"] = "Firm name is required."
    if tier not in TIER_CHOICES:
        errors["tier"] = "Invalid tier."
    if deployment_channel not in CHANNEL_CHOICES:
        errors["deployment_channel"] = "Invalid channel."

    # Slug uniqueness check
    if not errors:
        session_factory = get_session_factory()
        async with session_factory() as session:
            from sqlalchemy import text
            result = await session.execute(
                text("SELECT id FROM tenants WHERE slug = :slug"), {"slug": slug}
            )
            if result.fetchone():
                errors["slug"] = f"Slug '{slug}' is already in use."

    t = _templates(request)
    if errors:
        return t.TemplateResponse(
            request,
            "admin/provision_wizard.html",
            {
                "step": 1,
                "tier_choices": TIER_CHOICES,
                "channel_choices": CHANNEL_CHOICES,
                "errors": errors,
                "form": {
                    "slug": slug,
                    "firm_name": firm_name,
                    "tier": tier,
                    "deployment_channel": deployment_channel,
                },
            },
        )

    return t.TemplateResponse(
        request,
        "admin/provision_wizard.html",
        {
            "step": 2,
            "tier_choices": TIER_CHOICES,
            "channel_choices": CHANNEL_CHOICES,
            "errors": {},
            "form": {
                "slug": slug,
                "firm_name": firm_name,
                "tier": tier,
                "deployment_channel": deployment_channel,
                "domain": f"{slug}.praesidium-legal.com",
                "ssl_mode": "letsencrypt",
            },
        },
    )


# ── Step 2 — Domain & SSL Mode ───────────────────────────────────────────────


@router.post("/step2", response_class=HTMLResponse)
async def provision_step2(
    request: Request,
    slug: Annotated[str, Form()],
    firm_name: Annotated[str, Form()],
    tier: Annotated[str, Form()],
    deployment_channel: Annotated[str, Form()],
    domain: Annotated[str, Form()],
    ssl_mode: Annotated[str, Form()],
    user=Depends(get_current_user),
):
    if not getattr(user, "is_platform_admin", False):
        return RedirectResponse("/admin", status_code=303)

    errors: dict[str, str] = {}
    domain = domain.strip().lower()

    if not domain:
        errors["domain"] = "Domain is required."
    if ssl_mode not in ("letsencrypt", "custom"):
        errors["ssl_mode"] = "Invalid SSL mode."

    t = _templates(request)
    form_data = {
        "slug": slug,
        "firm_name": firm_name,
        "tier": tier,
        "deployment_channel": deployment_channel,
        "domain": domain,
        "ssl_mode": ssl_mode,
    }

    if errors:
        return t.TemplateResponse(
            request,
            "admin/provision_wizard.html",
            {"step": 2, "errors": errors, "form": form_data,
             "tier_choices": TIER_CHOICES, "channel_choices": CHANNEL_CHOICES},
        )

    # If custom SSL → show upload step; if letsencrypt → skip to step 4 (feature flags)
    next_step = 3 if ssl_mode == "custom" else 4

    # Build tier-default feature flags for step 4
    from jobs.provision_tenant import TIER_PRESETS
    feature_flags = TIER_PRESETS.get(tier, TIER_PRESETS["starter"]).copy()

    return t.TemplateResponse(
        request,
        "admin/provision_wizard.html",
        {
            "step": next_step,
            "errors": {},
            "form": form_data,
            "feature_flags": feature_flags,
            "tier_choices": TIER_CHOICES,
            "channel_choices": CHANNEL_CHOICES,
        },
    )


# ── Step 3 — Custom Cert Upload ──────────────────────────────────────────────


@router.post("/step3", response_class=HTMLResponse)
async def provision_step3(
    request: Request,
    slug: Annotated[str, Form()],
    firm_name: Annotated[str, Form()],
    tier: Annotated[str, Form()],
    deployment_channel: Annotated[str, Form()],
    domain: Annotated[str, Form()],
    ssl_mode: Annotated[str, Form()],
    cert_file: UploadFile = File(None),
    key_file: UploadFile = File(None),
    user=Depends(get_current_user),
):
    if not getattr(user, "is_platform_admin", False):
        return RedirectResponse("/admin", status_code=303)

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

    t = _templates(request)
    form_data = {
        "slug": slug, "firm_name": firm_name, "tier": tier,
        "deployment_channel": deployment_channel, "domain": domain,
        "ssl_mode": ssl_mode,
    }

    if errors:
        return t.TemplateResponse(
            request,
            "admin/provision_wizard.html",
            {"step": 3, "errors": errors, "form": form_data,
             "tier_choices": TIER_CHOICES, "channel_choices": CHANNEL_CHOICES},
        )

    # Stash cert bytes in Redis temporarily (keyed by slug, 10-minute TTL)
    if cert_bytes and key_bytes:
        r = _get_redis()
        r.setex(f"provision:cert:{slug}", 600, cert_bytes)
        r.setex(f"provision:key:{slug}", 600, key_bytes)

    from jobs.provision_tenant import TIER_PRESETS
    feature_flags = TIER_PRESETS.get(tier, TIER_PRESETS["starter"]).copy()

    return t.TemplateResponse(
        request,
        "admin/provision_wizard.html",
        {
            "step": 4,
            "errors": {},
            "form": form_data,
            "feature_flags": feature_flags,
            "tier_choices": TIER_CHOICES,
            "channel_choices": CHANNEL_CHOICES,
        },
    )


# ── Step 4 → Confirm ────────────────────────────────────────────────────────


@router.post("/confirm", response_class=HTMLResponse)
async def provision_confirm(request: Request, user=Depends(get_current_user)):
    if not getattr(user, "is_platform_admin", False):
        return RedirectResponse("/admin", status_code=303)

    form = await request.form()
    slug = str(form.get("slug", "")).strip()
    firm_name = str(form.get("firm_name", "")).strip()
    tier = str(form.get("tier", "intelligence"))
    deployment_channel = str(form.get("deployment_channel", "none"))
    domain = str(form.get("domain", f"{slug}.praesidium-legal.com"))
    ssl_mode = str(form.get("ssl_mode", "letsencrypt"))
    first_user_email = str(form.get("first_user_email", "")).strip()

    # Collect feature overrides from form checkboxes
    from jobs.provision_tenant import TIER_PRESETS
    base_flags = TIER_PRESETS.get(tier, TIER_PRESETS["starter"]).copy()
    feature_overrides: dict[str, bool] = {}
    for key in base_flags:
        feature_overrides[key] = f"feature_{key}" in form

    # Auto-generate temp password if not supplied
    first_user_password = str(form.get("first_user_password", "")).strip()
    if not first_user_password:
        first_user_password = secrets.token_urlsafe(12)

    t = _templates(request)
    return t.TemplateResponse(
        request,
        "admin/provision_confirm.html",
        {
            "form": {
                "slug": slug,
                "firm_name": firm_name,
                "tier": tier,
                "deployment_channel": deployment_channel,
                "domain": domain,
                "ssl_mode": ssl_mode,
                "first_user_email": first_user_email,
                "first_user_password": first_user_password,
                "feature_overrides": feature_overrides,
            },
            "feature_overrides": feature_overrides,
        },
    )


# ── Execute — Enqueue RQ Job ─────────────────────────────────────────────────


@router.post("/execute", response_class=HTMLResponse)
async def provision_execute(request: Request, user=Depends(get_current_user)):
    if not getattr(user, "is_platform_admin", False):
        return RedirectResponse("/admin", status_code=303)

    form = await request.form()
    slug = str(form.get("slug", "")).strip()
    firm_name = str(form.get("firm_name", "")).strip()
    tier = str(form.get("tier", "intelligence"))
    deployment_channel = str(form.get("deployment_channel", "none"))
    domain = str(form.get("domain", ""))
    ssl_mode = str(form.get("ssl_mode", "letsencrypt"))
    first_user_email = str(form.get("first_user_email", ""))
    first_user_password = str(form.get("first_user_password", ""))

    # Parse feature_overrides JSON embedded in hidden field
    feature_overrides_raw = str(form.get("feature_overrides_json", "{}"))
    try:
        feature_overrides = json.loads(feature_overrides_raw)
    except Exception:
        feature_overrides = {}

    # Retrieve cert bytes from Redis if custom SSL
    ssl_cert_path: str | None = None
    ssl_key_path: str | None = None
    if ssl_mode == "custom":
        r = _get_redis()
        cert_bytes = r.get(f"provision:cert:{slug}")
        key_bytes = r.get(f"provision:key:{slug}")
        if cert_bytes and key_bytes:
            from rq import Queue as RQueue
            ssl_q = RQueue("default", connection=r)
            from jobs.ssl_provision import provision_custom_cert
            ssl_job = ssl_q.enqueue(
                provision_custom_cert,
                slug=slug,
                cert_pem=cert_bytes,
                key_pem=key_bytes,
                job_timeout=120,
            )
            # For now pass sentinel paths; provision_tenant job will read actual paths
            ssl_cert_path = f"/etc/praesidium/certs/{slug}/fullchain.pem"
            ssl_key_path = f"/etc/praesidium/certs/{slug}/privkey.pem"

    payload = {
        "slug": slug,
        "firm_name": firm_name,
        "tier": tier,
        "deployment_channel": deployment_channel,
        "domain": domain,
        "ssl_mode": ssl_mode,
        "ssl_cert_path": ssl_cert_path,
        "ssl_key_path": ssl_key_path,
        "first_user_email": first_user_email,
        "first_user_password": first_user_password,
        "feature_overrides": feature_overrides,
    }

    r = _get_redis()
    q = Queue("default", connection=r)
    from jobs.provision_tenant import provision_tenant
    job = q.enqueue(provision_tenant, payload, job_timeout=300)

    return RedirectResponse(f"/admin/provision/progress/{job.id}", status_code=303)


# ── Progress Polling (HTMX) ──────────────────────────────────────────────────


@router.get("/progress/{job_id}", response_class=HTMLResponse)
async def provision_progress(
    request: Request, job_id: str, user=Depends(get_current_user)
):
    if not getattr(user, "is_platform_admin", False):
        return RedirectResponse("/admin", status_code=303)

    r = _get_redis()
    try:
        job = Job.fetch(job_id, connection=r)
        status = job.get_status()
        result = job.result if status == "finished" else None
        error = str(job.exc_info) if status == "failed" else None
    except Exception as exc:
        status = "unknown"
        result = None
        error = str(exc)

    t = _templates(request)
    return t.TemplateResponse(
        request,
        "admin/provision_progress.html",
        {
            "job_id": job_id,
            "status": status,
            "result": result,
            "error": error,
        },
    )
