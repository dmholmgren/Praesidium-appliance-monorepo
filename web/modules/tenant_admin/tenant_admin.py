"""
modules/tenant_admin/tenant_admin.py
Tenant Admin Panel — accessible to firm administrators at /tenant-admin/
Protected by existing tenant session middleware.
Tenant admins can NOT access GlassBreak (/admin).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated

import bcrypt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

log = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")

router = APIRouter(prefix="/tenant-admin", tags=["tenant-admin"])


def _templates(request: Request):
    from app import templates
    return templates


def _tenant_id(request: Request) -> str:
    return getattr(request.state, "tenant_id", "").strip()


def _require_admin(user, request: Request):
    """Return True if user is a tenant admin (role=admin or platform_admin)."""
    role = getattr(user, "role", "")
    return role in ("admin", "platform_admin")


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def tenant_admin_dashboard(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT slug, name, tier, domain, status "
                    "FROM tenants WHERE id = :tid"
                ),
                {"tid": tid},
            )
        ).fetchone()
        user_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM users WHERE tenant_id = :tid AND is_active = true"),
                {"tid": tid},
            )
        ).scalar()

    t = _templates(request)
    return t.TemplateResponse(
        request,
        "tenant_admin/dashboard.html",
        {"tenant": row, "user_count": user_count, "user": user},
    )


# ── Firm Profile ──────────────────────────────────────────────────────────────

@router.get("/profile", response_class=HTMLResponse)
async def tenant_profile_get(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)
    async with AsyncSessionLocal() as session:
        branding = (
            await session.execute(
                text(
                    "SELECT platform_name, platform_short_name, suppress_attribution "
                    "FROM tenant_branding WHERE tenant_id = :tid"
                ),
                {"tid": tid},
            )
        ).fetchone()
    t = _templates(request)
    return t.TemplateResponse(
        request,
        "tenant_admin/dashboard.html",
        {"page": "profile", "branding": branding, "user": user},
    )


@router.post("/profile", response_class=HTMLResponse)
async def tenant_profile_post(
    request: Request,
    platform_name: Annotated[str, Form()],
    platform_short_name: Annotated[str, Form()],
    suppress_attribution: Annotated[str, Form()] = "off",
    user=Depends(get_current_user),
):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)
    suppress = suppress_attribution == "on"
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE tenant_branding SET platform_name=:name, "
                "platform_short_name=:short, suppress_attribution=:suppress "
                "WHERE tenant_id=:tid"
            ),
            {
                "name":     platform_name.strip(),
                "short":    platform_short_name.strip()[:20],
                "suppress": suppress,
                "tid":      tid,
            },
        )
        await session.commit()
    return RedirectResponse("/tenant-admin/profile?saved=1", status_code=303)


# ── Users ─────────────────────────────────────────────────────────────────────

@router.get("/users", response_class=HTMLResponse)
async def tenant_users_get(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, email, role, is_active, created_at "
                    "FROM users WHERE tenant_id = :tid ORDER BY created_at"
                ),
                {"tid": tid},
            )
        ).fetchall()

        # Auth source for this tenant — drives "Sync Directory" button visibility
        tenant_row = (
            await session.execute(
                text("SELECT auth_adapter FROM tenants WHERE TRIM(id) = :tid LIMIT 1"),
                {"tid": tid},
            )
        ).fetchone()
        auth_adapter = tenant_row[0] if tenant_row else "local"

        # Last directory sync
        last_sync = (
            await session.execute(
                text("""
                    SELECT started_at, completed_at, discovered_count, new_count, error
                    FROM connector_entity_sync_log
                    WHERE TRIM(tenant_id) = :tid
                      AND connector_type LIKE 'auth_%'
                    ORDER BY started_at DESC
                    LIMIT 1
                """),
                {"tid": tid},
            )
        ).fetchone()

    t = _templates(request)
    return t.TemplateResponse(
        request,
        "tenant_admin/users.html",
        {
            "users":        rows,
            "user":         user,
            "auth_adapter": auth_adapter,
            "last_sync":    last_sync,
            "saved":        request.query_params.get("saved"),
            "syncing":      request.query_params.get("syncing"),
            "sync_error":   request.query_params.get("sync_error"),
        },
    )


@router.post("/users/sync-directory")
async def tenant_users_sync_directory(
    request: Request,
    user=Depends(get_current_user),
):
    """
    Trigger a directory sync from the tenant's configured auth source.
    Pulls users from LDAP / Azure AD and pre-populates the users table.
    Enqueues a background job on PROC-01.

    STUB — job implementations per auth source to be added:
      jobs/sync_directory_ldap.py   → walk LDAP OU tree
      jobs/sync_directory_azure.py  → MS Graph /users endpoint
    """
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)

    tid = _tenant_id(request)
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", 0)

    async with AsyncSessionLocal() as session:
        tenant_row = (
            await session.execute(
                text("SELECT auth_adapter FROM tenants WHERE TRIM(id) = :tid LIMIT 1"),
                {"tid": tid},
            )
        ).fetchone()
        auth_adapter = tenant_row[0] if tenant_row else "local"

    if auth_adapter == "local":
        return RedirectResponse(
            "/tenant-admin/users?sync_error=Local+auth+has+no+external+directory",
            status_code=303,
        )

    # Log sync attempt
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO connector_entity_sync_log
                    (tenant_id, connector_type, triggered_by)
                VALUES (:tid, :ctype, :uid)
            """),
            {"tid": tid, "ctype": f"auth_{auth_adapter}", "uid": user_id},
        )
        await session.commit()

    # Enqueue adapter-specific directory sync job
    # Normalize ldaps → ldaps (both ldap and ldaps map to the same job)
    job_adapter = auth_adapter  # ldap → sync_directory_ldap, ldaps → sync_directory_ldaps
    try:
        from redis import Redis
        from rq import Queue
        q = Queue(connection=Redis.from_url(REDIS_URL))
        job = q.enqueue(
            f"jobs.sync_directory_{job_adapter}.run",
            tid,
            user_id,
            job_timeout=300,
        )
        log.info(
            f"[users/sync-directory] tenant={tid} adapter={auth_adapter} "
            f"job_id={job.id}"
        )
    except Exception as e:
        log.exception(f"[users/sync-directory] failed to enqueue job: {e}")

    return RedirectResponse("/tenant-admin/users?syncing=1", status_code=303)


@router.post("/users/add", response_class=HTMLResponse)
async def tenant_user_add(
    request: Request,
    email: Annotated[str, Form()],
    role: Annotated[str, Form()],
    temp_password: Annotated[str, Form()],
    user=Depends(get_current_user),
):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)
    email = email.strip().lower()
    if role not in ("admin", "attorney", "staff", "viewer"):
        role = "attorney"
    pw_hash = bcrypt.hashpw(temp_password.encode(), bcrypt.gensalt()).decode()
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO users (tenant_id, email, password_hash, role, is_active) "
                "VALUES (:tid, :email, :pw, :role, true) "
                "ON CONFLICT (tenant_id, email) DO NOTHING"
            ),
            {"tid": tid, "email": email, "pw": pw_hash, "role": role},
        )
        await session.commit()
    return RedirectResponse("/tenant-admin/users?saved=1", status_code=303)


@router.post("/users/{user_id}/deactivate")
async def tenant_user_deactivate(
    request: Request, user_id: int, user=Depends(get_current_user)
):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE users SET is_active=false "
                "WHERE id=:uid AND tenant_id=:tid AND id != :me"
            ),
            {"uid": user_id, "tid": tid, "me": user.id},
        )
        await session.commit()
    return RedirectResponse("/tenant-admin/users?saved=1", status_code=303)


@router.post("/users/{user_id}/reset-password")
async def tenant_user_reset_password(
    request: Request,
    user_id: int,
    new_password: Annotated[str, Form()],
    user=Depends(get_current_user),
):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)
    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE users SET password_hash=:pw "
                "WHERE id=:uid AND tenant_id=:tid"
            ),
            {"pw": pw_hash, "uid": user_id, "tid": tid},
        )
        await session.commit()
    return RedirectResponse("/tenant-admin/users?saved=1", status_code=303)


# ── Feature Settings ──────────────────────────────────────────────────────────

@router.get("/features", response_class=HTMLResponse)
async def tenant_features_get(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT feature_flags FROM tenants WHERE id = :tid"),
                {"tid": tid},
            )
        ).fetchone()
        feature_flags = row[0] if row and row[0] else {}
    features = [
        {"feature_key": k, "enabled": v, "locked_by_platform": False}
        for k, v in sorted(feature_flags.items())
    ]
    t = _templates(request)
    return t.TemplateResponse(
        request,
        "tenant_admin/features.html",
        {"features": features, "user": user, "saved": request.query_params.get("saved")},
    )


@router.post("/features", response_class=HTMLResponse)
async def tenant_features_post(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)
    form = await request.form()
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT feature_flags FROM tenants WHERE id = :tid"),
                {"tid": tid},
            )
        ).fetchone()
        feature_flags = dict(row[0]) if row and row[0] else {}
        updated = {k: (f"feature_{k}" in form) for k in feature_flags}
        await session.execute(
            text("UPDATE tenants SET feature_flags = :flags WHERE id = :tid"),
            {"flags": json.dumps(updated), "tid": tid},
        )
        await session.commit()
    return RedirectResponse("/tenant-admin/features?saved=1", status_code=303)


# ── SSL & Domain ──────────────────────────────────────────────────────────────

@router.get("/ssl", response_class=HTMLResponse)
async def tenant_ssl_get(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT ssl_mode, ssl_cert_path, ssl_key_path, ssl_domain "
                    "FROM tenants WHERE id=:tid"
                ),
                {"tid": tid},
            )
        ).fetchone()

    t = _templates(request)
    return t.TemplateResponse(
        request,
        "tenant_admin/ssl.html",
        {"ssl": row, "cert_expiry": None, "user": user},
    )


# ── BYOK API Keys ─────────────────────────────────────────────────────────────

@router.get("/byok", response_class=HTMLResponse)
async def tenant_byok_get(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT provider, key_hint FROM credentials_vault "
                    "WHERE tenant_id=:tid AND key_type='api_key' ORDER BY provider"
                ),
                {"tid": tid},
            )
        ).fetchall()
    t = _templates(request)
    return t.TemplateResponse(
        request,
        "tenant_admin/byok.html",
        {"keys": rows, "user": user, "saved": request.query_params.get("saved")},
    )


@router.post("/byok", response_class=HTMLResponse)
async def tenant_byok_post(
    request: Request,
    provider: Annotated[str, Form()],
    api_key: Annotated[str, Form()],
    user=Depends(get_current_user),
):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)

    if provider not in ("anthropic", "openai"):
        return RedirectResponse("/tenant-admin/byok?error=invalid_provider", status_code=303)

    api_key = api_key.strip()
    if len(api_key) < 20:
        return RedirectResponse("/tenant-admin/byok?error=key_too_short", status_code=303)

    from cryptography.fernet import Fernet
    import base64
    secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    key_bytes = (secret[:32]).encode().ljust(32, b"0")
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    f = Fernet(fernet_key)
    encrypted = f.encrypt(api_key.encode()).decode()
    hint = f"{api_key[:4]}...{api_key[-4:]}"

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO credentials_vault "
                "(tenant_id, provider, key_type, encrypted_key, key_hint) "
                "VALUES (:tid, :prov, 'api_key', :enc, :hint) "
                "ON CONFLICT (tenant_id, provider, key_type) DO UPDATE "
                "SET encrypted_key=EXCLUDED.encrypted_key, key_hint=EXCLUDED.key_hint"
            ),
            {"tid": tid, "prov": provider, "enc": encrypted, "hint": hint},
        )
        await session.commit()
    return RedirectResponse("/tenant-admin/byok?saved=1", status_code=303)


# ── Break Glass ───────────────────────────────────────────────────────────────

@router.get("/break-glass", response_class=HTMLResponse)
async def tenant_break_glass(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    t = _templates(request)
    return t.TemplateResponse(
        request,
        "tenant_admin/break_glass.html",
        {"user": user},
    )
