"""
modules/admin/admin_panel.py
Module 8 Component 1 — Global Admin Panel

Full platform admin UI served at /admin/* routes.

Auth: bcrypt session-based (replaces bootstrap X-Platform-Admin token for browser UI).
  POST /admin/login     — authenticate with PLATFORM_ADMIN_PASSWORD_HASH
  POST /admin/logout    — invalidate session
  GET  /admin/          — redirect to /admin/health

Pages (all GET, all session-protected, all HTMX-compatible):
  GET  /admin/health            — VM health grid + sidecar status
  GET  /admin/firmware          — firmware banks per VM + bank swap controls
  GET  /admin/backups           — config backup list + on-demand backup trigger
  GET  /admin/config            — config generator: generate + download scripts
  GET  /admin/tenants           — tenant list + middleware server assignments
  GET  /admin/connect           — Praesidium Connect site registry
  GET  /admin/events            — recent firmware_events log

HTMX partial endpoints (return HTML fragments, not full pages):
  GET  /admin/htmx/health-grid         — VM health rows (auto-refresh)
  GET  /admin/htmx/bank-status/{vm}    — bank status row for one VM
  POST /admin/htmx/bank/set            — set pending bank (returns status fragment)
  POST /admin/htmx/bank/confirm        — confirm bank swap (returns status fragment)
  POST /admin/htmx/backup/trigger      — trigger backup (returns job status fragment)
  GET  /admin/htmx/events-recent       — recent events rows (auto-refresh)
  GET  /admin/htmx/job-status/{job_id} — poll RQ job status (returns badge fragment)

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.admin_panel")

router = APIRouter(prefix="/admin")

# ── Template setup ────────────────────────────────────────────────────────────
# Templates live at /app/templates/admin/
_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "../../templates/admin")
templates = Jinja2Templates(directory=_TEMPLATE_DIR)

# ── Config ────────────────────────────────────────────────────────────────────
PLATFORM_ADMIN_PASSWORD_HASH = os.environ.get("PLATFORM_ADMIN_PASSWORD_HASH", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
SESSION_COOKIE = "praesidium_admin_session"
SESSION_TTL = 8 * 3600  # 8 hours
SESSION_SECRET = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# ── In-memory session store ───────────────────────────────────────────────────
# { token: { "created_at": float, "username": str } }
_sessions: dict[str, dict] = {}


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"created_at": time.time(), "username": "admin"}
    return token


def _validate_session(token: Optional[str]) -> bool:
    if not token or token not in _sessions:
        return False
    sess = _sessions[token]
    if time.time() - sess["created_at"] > SESSION_TTL:
        del _sessions[token]
        return False
    return True


def _invalidate_session(token: str) -> None:
    _sessions.pop(token, None)


async def require_admin_session(request: Request):
    """Dependency: validate admin session cookie. Redirect to login if invalid."""
    token = request.cookies.get(SESSION_COOKIE)
    if not _validate_session(token):
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/admin/login"},
        )
    return token


# ── RQ helper ─────────────────────────────────────────────────────────────────
def _enqueue(func_path: str, job_timeout: int = 300, **kwargs) -> Optional[str]:
    try:
        import redis as redis_lib
        from rq import Queue
        r = redis_lib.Redis.from_url(REDIS_URL, socket_connect_timeout=3)
        r.ping()
        q = Queue("default", connection=r)
        job = q.enqueue(func_path, job_timeout=job_timeout, **kwargs)
        return job.id
    except Exception as exc:
        log.warning(f"RQ enqueue failed: {exc}")
        return None


def _get_job(job_id: str) -> dict:
    try:
        import redis as redis_lib
        from rq.job import Job
        r = redis_lib.Redis.from_url(REDIS_URL, socket_connect_timeout=3)
        job = Job.fetch(job_id, connection=r)
        js = job.get_status()
        return {
            "job_id": job_id,
            "status": js.value if js else "unknown",
            "result": job.result if job.is_finished else None,
            "exc_info": job.exc_info[:200] if job.exc_info else None,
        }
    except Exception as exc:
        return {"job_id": job_id, "status": "unknown", "error": str(exc)}


# ══════════════════════════════════════════════════════════════════════════════
# Auth endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if _validate_session(token):
        return RedirectResponse("/admin/health", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    password: str = Form(...),
):
    valid = False
    if PLATFORM_ADMIN_PASSWORD_HASH:
        try:
            valid = bcrypt.checkpw(
                password.encode(),
                PLATFORM_ADMIN_PASSWORD_HASH.encode()
            )
        except Exception:
            valid = False
    else:
        # No hash configured — allow any non-empty password from internal network
        client_ip = request.client.host if request.client else ""
        valid = bool(password) and (client_ip.startswith("10.10.") or client_ip == "127.0.0.1")

    if not valid:
        log.warning(f"Admin login failed from {request.client.host if request.client else 'unknown'}")
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid password"},
            status_code=401,
        )

    token = _create_session()
    response = RedirectResponse("/admin/health", status_code=302)
    response.set_cookie(
        SESSION_COOKIE, token,
        httponly=True, samesite="lax",
        max_age=SESSION_TTL,
        secure=False,  # Set True when SSL is in use
    )
    log.info(f"Admin login successful from {request.client.host if request.client else 'unknown'}")
    return response


@router.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        _invalidate_session(token)
    response = RedirectResponse("/admin/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ══════════════════════════════════════════════════════════════════════════════
# Admin pages
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/", response_class=HTMLResponse)
async def admin_root(_: str = Depends(require_admin_session)):
    return RedirectResponse("/admin/health", status_code=302)


@router.get("/health", response_class=HTMLResponse)
async def admin_health(
    request: Request,
    _: str = Depends(require_admin_session),
):
    """VM health grid — polls sidecar and middleware_servers table."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT id, name, ip, port, role, status, is_default, last_seen
            FROM middleware_servers ORDER BY role, name
            """)
        )
        servers = [dict(r) for r in result.mappings().fetchall()]

    return templates.TemplateResponse("health.html", {
        "request": request,
        "servers": servers,
        "page": "health",
        "now": datetime.now(timezone.utc).isoformat(),
    })


@router.get("/firmware", response_class=HTMLResponse)
async def admin_firmware(
    request: Request,
    _: str = Depends(require_admin_session),
):
    """Firmware banks per VM with bank swap controls."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT fb.vm_name, fb.bank, fb.is_active, fb.pending_active,
                   fb.loaded_at, fb.confirmed_at, fb.confirmed_by,
                   fv.version_tag, ms.role
            FROM firmware_banks fb
            LEFT JOIN firmware_versions fv ON fv.id = fb.firmware_version_id
            LEFT JOIN middleware_servers ms ON ms.name = fb.vm_name
            ORDER BY fb.vm_name, fb.bank
            """)
        )
        rows = result.mappings().fetchall()

    # Group by VM
    vms: dict = {}
    for r in rows:
        vm = r["vm_name"]
        if vm not in vms:
            vms[vm] = {"vm_name": vm, "role": r["role"], "banks": {}}
        vms[vm]["banks"][r["bank"]] = dict(r)

    return templates.TemplateResponse("firmware.html", {
        "request": request,
        "vms": vms,
        "page": "firmware",
    })


@router.get("/backups", response_class=HTMLResponse)
async def admin_backups(
    request: Request,
    _: str = Depends(require_admin_session),
):
    """Config backup list."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT id, label, backup_at, triggered_by, is_auto,
                   pre_event_type, config_archive_path, config_hash,
                   alembic_head, tenant_count
            FROM config_backups
            ORDER BY backup_at DESC
            LIMIT 50
            """)
        )
        backups = [dict(r) for r in result.mappings().fetchall()]

    return templates.TemplateResponse("backups.html", {
        "request": request,
        "backups": backups,
        "page": "backups",
    })


@router.get("/config", response_class=HTMLResponse)
async def admin_config(
    request: Request,
    _: str = Depends(require_admin_session),
):
    """Config generator — generate and download provision scripts."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT id, target_role, target_hostname, target_ip,
                   generated_at, firmware_version
            FROM config_generated_scripts
            ORDER BY generated_at DESC
            LIMIT 20
            """)
        )
        generated = [dict(r) for r in result.mappings().fetchall()]

    roles = ["web", "web-dev", "rprx", "proc", "fbrg", "wss", "db"]
    return templates.TemplateResponse("config.html", {
        "request": request,
        "generated": generated,
        "roles": roles,
        "page": "config",
    })


@router.get("/tenants", response_class=HTMLResponse)
async def admin_tenants(
    request: Request,
    _: str = Depends(require_admin_session),
):
    """Tenant list with licensing summary."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT t.id, t.slug, t.domain, t.status,
                   t.storage_adapter, t.ai_provider,
                   tl.tier, tl.expires_at,
                   ms.name as middleware_server_name
            FROM tenants t
            LEFT JOIN tenant_licenses tl ON tl.tenant_id = t.id
            LEFT JOIN middleware_servers ms ON ms.id = t.middleware_server_id
            ORDER BY t.slug
            """)
        )
        tenants = [dict(r) for r in result.mappings().fetchall()]

    return templates.TemplateResponse("tenants.html", {
        "request": request,
        "tenants": tenants,
        "page": "tenants",
    })


@router.get("/connect", response_class=HTMLResponse)
async def admin_connect(
    request: Request,
    _: str = Depends(require_admin_session),
):
    """Praesidium Connect site registry."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT id, site_code, site_name, firmware_version,
                   alembic_head, last_checkin, checkin_ip,
                   license_status, connect_enabled, registered_at
            FROM praesidium_connect_sites
            ORDER BY registered_at DESC
            """)
        )
        sites = [dict(r) for r in result.mappings().fetchall()]

    return templates.TemplateResponse("connect.html", {
        "request": request,
        "sites": sites,
        "page": "connect",
    })


@router.get("/events", response_class=HTMLResponse)
async def admin_events(
    request: Request,
    _: str = Depends(require_admin_session),
):
    """Recent firmware_events log."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT id, event_type, vm_name, triggered_by,
                   detail, created_at
            FROM firmware_events
            ORDER BY created_at DESC
            LIMIT 100
            """)
        )
        events = [dict(r) for r in result.mappings().fetchall()]

    return templates.TemplateResponse("events.html", {
        "request": request,
        "events": events,
        "page": "events",
    })


# ══════════════════════════════════════════════════════════════════════════════
# HTMX partial endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/htmx/health-grid", response_class=HTMLResponse)
async def htmx_health_grid(
    request: Request,
    _: str = Depends(require_admin_session),
):
    """Auto-refreshing VM health rows fragment."""
    import httpx as _httpx

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT id, name, ip, port, role, status, last_seen FROM middleware_servers ORDER BY role, name")
        )
        servers = [dict(r) for r in result.mappings().fetchall()]

    # Quick TCP health checks
    import asyncio
    import socket

    async def check(s):
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(s["ip"], s["port"]), timeout=2.0
            )
            writer.close()
            await writer.wait_closed()
            return {**s, "reachable": True}
        except Exception:
            return {**s, "reachable": False}

    checked = await asyncio.gather(*[check(s) for s in servers])

    return templates.TemplateResponse("partials/health_grid.html", {
        "request": request,
        "servers": checked,
        "checked_at": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
    })


@router.get("/htmx/bank-status/{vm_name}", response_class=HTMLResponse)
async def htmx_bank_status(
    vm_name: str,
    request: Request,
    _: str = Depends(require_admin_session),
):
    """Bank status row fragment for one VM."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT fb.bank, fb.is_active, fb.pending_active,
                   fb.confirmed_at, fv.version_tag
            FROM firmware_banks fb
            LEFT JOIN firmware_versions fv ON fv.id = fb.firmware_version_id
            WHERE fb.vm_name = :vm_name ORDER BY fb.bank
            """),
            {"vm_name": vm_name}
        )
        banks = {r["bank"]: dict(r) for r in result.mappings().fetchall()}

    return templates.TemplateResponse("partials/bank_row.html", {
        "request": request,
        "vm_name": vm_name,
        "banks": banks,
    })


@router.post("/htmx/bank/set", response_class=HTMLResponse)
async def htmx_bank_set(
    request: Request,
    vm_name: str = Form(...),
    target_bank: str = Form(...),
    _: str = Depends(require_admin_session),
):
    """Set pending bank — returns status fragment."""
    job_id = _enqueue(
        "jobs.bank_swap.set_pending_bank",
        job_timeout=120,
        vm_name=vm_name,
        target_bank=target_bank,
        triggered_by="admin-panel",
    )
    return templates.TemplateResponse("partials/job_queued.html", {
        "request": request,
        "job_id": job_id,
        "action": f"Bank set: {vm_name} → {target_bank}",
        "confirm_vm": vm_name,
        "confirm_bank": target_bank,
    })


@router.post("/htmx/bank/confirm", response_class=HTMLResponse)
async def htmx_bank_confirm(
    request: Request,
    vm_name: str = Form(...),
    target_bank: str = Form(...),
    _: str = Depends(require_admin_session),
):
    """Confirm bank swap — returns status fragment."""
    job_id = _enqueue(
        "jobs.bank_swap.confirm_bank_swap",
        job_timeout=900,
        vm_name=vm_name,
        target_bank=target_bank,
        confirmed_by="admin-panel",
    )
    return templates.TemplateResponse("partials/job_queued.html", {
        "request": request,
        "job_id": job_id,
        "action": f"Bank swap confirmed: {vm_name} → {target_bank}",
        "confirm_vm": None,
        "confirm_bank": None,
    })


@router.post("/htmx/backup/trigger", response_class=HTMLResponse)
async def htmx_backup_trigger(
    request: Request,
    label: str = Form(""),
    _: str = Depends(require_admin_session),
):
    """Trigger config backup — returns job status fragment."""
    job_id = _enqueue(
        "jobs.config_backup.create_config_backup",
        job_timeout=300,
        label=label or None,
        triggered_by="admin-panel",
        is_auto=False,
    )
    return templates.TemplateResponse("partials/job_queued.html", {
        "request": request,
        "job_id": job_id,
        "action": "Config backup triggered",
        "confirm_vm": None,
        "confirm_bank": None,
    })


@router.get("/htmx/events-recent", response_class=HTMLResponse)
async def htmx_events_recent(
    request: Request,
    _: str = Depends(require_admin_session),
):
    """Recent events rows fragment — for auto-refresh."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT event_type, vm_name, triggered_by, created_at
            FROM firmware_events
            ORDER BY created_at DESC LIMIT 20
            """)
        )
        events = [dict(r) for r in result.mappings().fetchall()]

    return templates.TemplateResponse("partials/events_rows.html", {
        "request": request,
        "events": events,
    })


@router.get("/htmx/job-status/{job_id}", response_class=HTMLResponse)
async def htmx_job_status(
    job_id: str,
    request: Request,
    _: str = Depends(require_admin_session),
):
    """Poll RQ job status — returns a status badge fragment."""
    info = _get_job(job_id)
    return templates.TemplateResponse("partials/job_status_badge.html", {
        "request": request,
        "job": info,
    })
