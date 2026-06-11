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

    from core.services.nav_context import get_nav_context
    nav_ctx = await get_nav_context(request)
    brand = getattr(request.state, 'branding', None)
    t = _templates(request)
    return t.TemplateResponse(
        request,
        "tenant_admin_react.html",
        {"tenant": row, "user_count": user_count, "user": user,
         "brand": brand, "page": "tenant-admin",
         "current_user": getattr(request.state, 'current_user', None),
         **nav_ctx},
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
                    "SELECT id, email, role, is_active, created_at, auth_provider "
                    "FROM users WHERE tenant_id = :tid ORDER BY created_at"
                ),
                {"tid": tid},
            )
        ).fetchall()

        # ── Enumerate ALL configured auth connectors for this tenant ──────────
        # Joins connector_registry (catalog of auth connectors) with
        # tenant_connectors (per-tenant config). Inactive registry rows or
        # unconfigured/disabled tenant rows are filtered out.
        # auth_local is excluded — there's no external directory to poll.
        auth_connectors = (
            await session.execute(
                text("""
                    SELECT
                        cr.connector_type,
                        cr.display_name,
                        cr.icon,
                        tc.status        AS tenant_status,
                        tc.last_sync_at  AS tenant_last_sync,
                        tc.last_error    AS tenant_last_error
                    FROM connector_registry cr
                    LEFT JOIN tenant_connectors tc
                      ON tc.connector_type = cr.connector_type
                     AND TRIM(tc.tenant_id) = :tid
                    WHERE cr.connector_group = 'auth'
                      AND cr.is_active = true
                      AND cr.connector_type != 'auth_local'
                    ORDER BY cr.display_order, cr.connector_type
                """),
                {"tid": tid},
            )
        ).mappings().all()

        # ── Per-connector last sync log (most recent run per connector_type) ─
        last_sync_per_connector = (
            await session.execute(
                text("""
                    SELECT DISTINCT ON (connector_type)
                        connector_type,
                        started_at, completed_at,
                        discovered_count, new_count, auto_matched_count, error
                    FROM connector_entity_sync_log
                    WHERE TRIM(tenant_id) = :tid
                      AND connector_type LIKE 'auth_%'
                    ORDER BY connector_type, started_at DESC
                """),
                {"tid": tid},
            )
        ).mappings().all()
        last_sync_map = {r["connector_type"]: dict(r) for r in last_sync_per_connector}

        # Most recent sync overall (for header summary)
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

        # ── Discovered users count — directory-sourced inactive users ─────────
        # Awaiting admin review/assignment. Drives the "N awaiting review" badge.
        discovered_count = (
            await session.execute(
                text("""
                    SELECT COUNT(*) FROM users
                    WHERE TRIM(tenant_id) = :tid
                      AND is_active = false
                      AND auth_provider IS NOT NULL
                      AND auth_provider != 'local'
                """),
                {"tid": tid},
            )
        ).scalar() or 0

    # Build display list — merge registry row with last-sync info
    auth_connectors_display = []
    for c in auth_connectors:
        cdict = dict(c)
        cdict["last_sync"] = last_sync_map.get(c["connector_type"])
        auth_connectors_display.append(cdict)

    t = _templates(request)
    return t.TemplateResponse(
        request,
        "tenant_admin/users.html",
        {
            "users":            rows,
            "user":             user,
            "auth_connectors":  auth_connectors_display,
            "discovered_count": discovered_count,
            "last_sync":        last_sync,
            "saved":            request.query_params.get("saved"),
            "syncing":          request.query_params.get("syncing"),
            "sync_error":       request.query_params.get("sync_error"),
        },
    )


@router.post("/users/sync-directory")
async def tenant_users_sync_directory(
    request: Request,
    connector_type: Annotated[str, Form()] = "",
    user=Depends(get_current_user),
):
    """
    Trigger a directory sync from a specific registered auth connector.
    Accepts connector_type form parameter (e.g. 'auth_ldap', 'auth_azure').

    For each connector_type, dispatches to:
      auth_ldap   → jobs.sync_directory_ldap.run
      auth_azure  → jobs.sync_directory_azure.run
      auth_okta   → jobs.sync_directory_okta.run

    The job pulls users from the directory and stages them in the `users`
    table with is_active=false. Admin reviews and activates them at
    /tenant-admin/users/discovered.
    """
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)

    tid = _tenant_id(request)
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", 0)

    # Validate connector_type — must be a registered active auth connector
    # AND configured for this tenant. Defends against the form being tampered
    # with to enqueue arbitrary jobs.
    if not connector_type or not connector_type.startswith("auth_") or connector_type == "auth_local":
        return RedirectResponse(
            "/tenant-admin/users?sync_error=Invalid+connector",
            status_code=303,
        )

    async with AsyncSessionLocal() as session:
        valid = (
            await session.execute(
                text("""
                    SELECT cr.connector_type
                    FROM connector_registry cr
                    LEFT JOIN tenant_connectors tc
                      ON tc.connector_type = cr.connector_type
                     AND TRIM(tc.tenant_id) = :tid
                    WHERE cr.connector_type = :ctype
                      AND cr.connector_group = 'auth'
                      AND cr.is_active = true
                """),
                {"tid": tid, "ctype": connector_type},
            )
        ).first()

    if not valid:
        return RedirectResponse(
            "/tenant-admin/users?sync_error=Connector+not+registered+or+inactive",
            status_code=303,
        )

    # Insert sync log row — captures the run BEFORE the job is enqueued so
    # the UI can show "queued" state immediately.
    sync_log_id = None
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO connector_entity_sync_log
                    (tenant_id, connector_type, triggered_by)
                VALUES (:tid, :ctype, :uid)
                RETURNING id
            """),
            {"tid": tid, "ctype": connector_type, "uid": user_id},
        )
        row = result.first()
        sync_log_id = str(row[0]) if row else None
        await session.commit()

    # Enqueue the job. Adapter slug maps connector_type → job module:
    # 'auth_ldap' → 'sync_directory_ldap'
    job_module = connector_type.replace("auth_", "sync_directory_")
    try:
        from redis import Redis
        from rq import Queue
        q = Queue(connection=Redis.from_url(REDIS_URL))
        job = q.enqueue(
            f"jobs.{job_module}.run",
            tid,
            user_id,
            sync_log_id,
            job_timeout=300,
        )
        log.info(
            f"[users/sync-directory] tenant={tid} connector={connector_type} "
            f"job_id={job.id}"
        )
    except Exception as e:
        log.exception(f"[users/sync-directory] failed to enqueue job: {e}")
        return RedirectResponse(
            "/tenant-admin/users?sync_error=Failed+to+enqueue+sync+job",
            status_code=303,
        )

    return RedirectResponse(f"/tenant-admin/users?syncing=1", status_code=303)


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


# ── Discovered Users Review (post-directory-sync) ────────────────────────────

@router.get("/users/discovered", response_class=HTMLResponse)
async def tenant_users_discovered(request: Request, user=Depends(get_current_user)):
    """
    Review users discovered via directory sync. These were created with
    is_active=false and auth_provider != 'local'. The admin assigns roles
    and bulk-activates them.
    """
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)

    async with AsyncSessionLocal() as session:
        discovered = (
            await session.execute(
                text("""
                    SELECT
                        u.id, u.email, u.username, u.full_name, u.role,
                        u.auth_provider, u.created_at,
                        uac.display_name AS uac_display_name,
                        uac.meta AS uac_meta
                    FROM users u
                    LEFT JOIN user_auth_credentials uac
                      ON uac.user_id = u.id AND TRIM(uac.tenant_id) = :tid
                    WHERE TRIM(u.tenant_id) = :tid
                      AND u.is_active = false
                      AND u.auth_provider IS NOT NULL
                      AND u.auth_provider != 'local'
                    ORDER BY u.auth_provider, u.email
                """),
                {"tid": tid},
            )
        ).mappings().all()

    t = _templates(request)
    return t.TemplateResponse(
        request,
        "tenant_admin/users_discovered.html",
        {
            "discovered": discovered,
            "user":       user,
            "saved":      request.query_params.get("saved"),
            "skipped":    request.query_params.get("skipped"),
        },
    )


@router.post("/users/discovered/import")
async def tenant_users_discovered_import(
    request: Request,
    user=Depends(get_current_user),
):
    """
    Bulk-activate selected discovered users with assigned roles.
    Form fields:
      activate[]=<user_id>     — checkbox per row, repeated
      role_<user_id>=<role>    — select per row
    """
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)
    admin_id = user.id if not isinstance(user, dict) else user.get("id", 0)

    form = await request.form()
    activate_ids = form.getlist("activate")
    if not activate_ids:
        return RedirectResponse(
            "/tenant-admin/users/discovered?skipped=No+users+selected",
            status_code=303,
        )

    valid_roles = {
        "super_admin", "admin", "partner", "attorney",
        "paralegal", "staff", "read_only",
    }

    activated = 0
    async with AsyncSessionLocal() as session:
        for raw_uid in activate_ids:
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue

            role = str(form.get(f"role_{uid}", "staff")).strip()
            if role not in valid_roles:
                role = "staff"

            # Activate + set role. Verify the user belongs to this tenant
            # AND was originally inactive directory-discovered (defends
            # against form tampering).
            result = await session.execute(
                text("""
                    UPDATE users SET
                        is_active = true,
                        role      = CAST(:role AS user_role_enum),
                        updated_at = now()
                    WHERE id = :uid
                      AND TRIM(tenant_id) = :tid
                      AND is_active = false
                      AND auth_provider IS NOT NULL
                      AND auth_provider != 'local'
                    RETURNING id
                """),
                {"uid": uid, "tid": tid, "role": role},
            )
            if result.first():
                activated += 1
                # Audit log
                await session.execute(
                    text("""
                        INSERT INTO user_activity_log
                            (tenant_id, user_id, action, details, performed_by_id)
                        VALUES (:tid, :uid, 'directory_import_activate',
                                CAST(:details AS jsonb), :admin)
                    """),
                    {
                        "tid": tid, "uid": uid,
                        "details": f'{{"role": "{role}"}}',
                        "admin": admin_id,
                    },
                )

        await session.commit()

    return RedirectResponse(
        f"/tenant-admin/users/discovered?saved=Activated+{activated}+user(s)",
        status_code=303,
    )


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

    if provider not in ("anthropic", "openai", "voyage"):
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




# ── Onboarding (React) ────────────────────────────────────────────────────────

@router.get('/onboarding', response_class=HTMLResponse)
async def tenant_onboarding_react(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse('/dashboard', status_code=303)
    from core.services.nav_context import get_nav_context
    nav_ctx = await get_nav_context(request)
    brand = getattr(request.state, 'branding', None)
    t = _templates(request)
    return t.TemplateResponse(
        request,
        'onboarding_react.html',
        {'user': user, 'brand': brand, 'page': 'tenant-admin',
         'current_user': getattr(request.state, 'current_user', None),
         **nav_ctx},
    )

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


# ── Embed Time Records ────────────────────────────────────────────────────────

@router.post("/client-matter-cleanup/embed-time")
async def embed_time_records(request: Request, user=Depends(get_current_user)):
    """Trigger billing phase chunking + Voyage embedding."""
    if not _require_admin(user, request):
        return {"error": "Unauthorized"}
    tid = _tenant_id(request)

    import subprocess
    result = {"chunks_created": 0, "embedded": 0, "embed_error": None}

    # Phase 1: Run chunker
    try:
        proc = subprocess.run(
            ["python3", "/app/jobs/chunk_billing_phases.py", "--tenant", tid],
            capture_output=True, text=True, timeout=120,
        )
        for line in proc.stdout.split("\n"):
            if "Chunks created:" in line:
                result["chunks_created"] = int(line.split(":")[1].strip())
    except Exception as e:
        return {"error": f"Chunking failed: {e}"}

    # Phase 2: Run embedding (if Voyage key is configured)
    try:
        from cryptography.fernet import Fernet
        import base64
        secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
        key_bytes = (secret[:32]).encode().ljust(32, b"0")
        fernet_key = base64.urlsafe_b64encode(key_bytes)
        f = Fernet(fernet_key)

        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    text("""
                        SELECT encrypted_key FROM credentials_vault
                        WHERE tenant_id = :tid AND provider = 'voyage'
                          AND key_type = 'api_key'
                    """),
                    {"tid": tid},
                )
            ).fetchone()

        if row:
            voyage_key = f.decrypt(row[0].encode()).decode()
            import subprocess as sp
            proc2 = sp.run(
                ["python3", "/app/jobs/embed_billing_chunks.py", "--tenant", tid,
                 "--api-key", voyage_key],
                capture_output=True, text=True, timeout=600,
            )
            for line in proc2.stdout.split("\n"):
                if "Chunks embedded:" in line:
                    result["embedded"] = int(line.split(":")[1].strip())
        else:
            result["embed_error"] = "No Voyage API key configured — add it under BYOK Settings"
    except Exception as e:
        result["embed_error"] = str(e)

    from fastapi.responses import JSONResponse
    return JSONResponse(result)



# ── Scan Legacy Inventory ─────────────────────────────────────────────────────


# ── Folder Reconciliation ─────────────────────────────────────────────────────

@router.get("/folder-reconciliation", response_class=HTMLResponse)
async def folder_reconciliation(request: Request, user=Depends(get_current_user)):
    """Main folder reconciliation page — lists all dms_folder_matches."""
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)

    async with AsyncSessionLocal() as session:
        matches = (
            await session.execute(
                text("""
                    SELECT fm.id::text, fm.folder_path, fm.score, fm.accepted,
                           fm.disk_file_count, fm.best_disk_path,
                           m.matter_name, c.client_name, m.id::text as matter_id
                    FROM dms_folder_matches fm
                    LEFT JOIN matters m ON fm.matter_id = m.id
                    LEFT JOIN clients c ON m.client_id = c.id
                    WHERE TRIM(fm.tenant_id) = :tid
                    ORDER BY fm.accepted ASC NULLS FIRST, fm.score DESC
                """),
                {"tid": tid},
            )
        ).mappings().all()

        stats_row = (
            await session.execute(
                text("""
                    SELECT
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE accepted = true) as accepted,
                        COUNT(*) FILTER (WHERE accepted = false) as pending,
                        COUNT(*) FILTER (WHERE accepted IS NULL) as unreviewed
                    FROM dms_folder_matches WHERE TRIM(tenant_id) = :tid
                """),
                {"tid": tid},
            )
        ).fetchone()

    t = _templates(request)
    return t.TemplateResponse(
        request,
        "tenant_admin/folder_reconciliation.html",
        {
            "user": user,
            "matches": [dict(m) for m in matches],
            "stats": {
                "total": stats_row[0] or 0,
                "accepted": stats_row[1] or 0,
                "pending": stats_row[2] or 0,
                "unreviewed": stats_row[3] or 0,
            },
        },
    )


@router.get("/folder-reconciliation/mapped-index")
async def folder_recon_mapped_index(request: Request, user=Depends(get_current_user)):
    """Return JSON of all accepted folder matches with matter info."""
    tid = _tenant_id(request)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT fm.id::text, fm.folder_path, fm.score,
                           fm.best_disk_path, fm.disk_file_count,
                           m.matter_name, c.client_name, m.id::text as matter_id
                    FROM dms_folder_matches fm
                    LEFT JOIN matters m ON fm.matter_id = m.id
                    LEFT JOIN clients c ON m.client_id = c.id
                    WHERE TRIM(fm.tenant_id) = :tid AND fm.accepted = true
                    ORDER BY fm.folder_path
                """),
                {"tid": tid},
            )
        ).mappings().all()
    from fastapi.responses import JSONResponse
    return JSONResponse([dict(r) for r in rows])


@router.post("/folder-reconciliation/auto-match")
async def folder_recon_auto_match(request: Request, user=Depends(get_current_user)):
    """Run AI folder matching — fuzzy-match unmatched folders to matters."""
    if not _require_admin(user, request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Unauthorized"}, status_code=403)
    tid = _tenant_id(request)

    import subprocess
    try:
        proc = subprocess.run(
            ["python3", "/app/jobs/automatch_folders.py", "--tenant", tid, "--propose"],
            capture_output=True, text=True, timeout=300,
        )
        from fastapi.responses import JSONResponse
        return JSONResponse({
            "status": "ok",
            "stdout": proc.stdout[-2000:] if proc.stdout else "",
            "stderr": proc.stderr[-500:] if proc.stderr else "",
        })
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/folder-reconciliation/accept-batch")
async def folder_recon_accept_batch(request: Request, user=Depends(get_current_user)):
    """Bulk-accept folder matches. Body: {match_ids: [...], min_score: float}"""
    if not _require_admin(user, request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Unauthorized"}, status_code=403)
    tid = _tenant_id(request)
    body = await request.json()
    match_ids = body.get("match_ids", [])
    min_score = body.get("min_score")
    items = body.get("items", [])

    log.warning(f"accept-batch: match_ids={match_ids}, items={items}, min_score={min_score}")

    count = 0
    async with AsyncSessionLocal() as session:
        # ── Mode 1: UI sends items[] with matter_id + disk_path ──────────
        if items:
            for item in items:
                mid = item.get("matter_id", "")
                disk_path = item.get("disk_path", "")
                if not mid or not disk_path:
                    continue

                # Try to find existing match row
                existing = (await session.execute(text("""
                    SELECT id FROM dms_folder_matches
                    WHERE TRIM(tenant_id) = :tid
                      AND matter_id = CAST(:mid AS uuid)
                      AND folder_path = :fp
                """), {"tid": tid, "mid": mid, "fp": disk_path})).fetchone()

                if existing:
                    await session.execute(text("""
                        UPDATE dms_folder_matches SET accepted = true
                        WHERE id = :fid AND TRIM(tenant_id) = :tid
                    """), {"fid": existing[0], "tid": tid})
                    log.warning(f"accept-batch: updated existing match {existing[0]} for {disk_path}")
                else:
                    # Create new match row
                    await session.execute(text("""
                        INSERT INTO dms_folder_matches
                            (tenant_id, matter_id, folder_path, score, accepted,
                             best_disk_path, disk_file_count)
                        VALUES (:tid, CAST(:mid AS uuid), :fp, 1.0, true,
                                :bp, :fc)
                    """), {
                        "tid": tid, "mid": mid, "fp": disk_path,
                        "bp": item.get("disk_root") or "",
                        "fc": item.get("file_count"),
                    })
                    log.warning(f"accept-batch: created new match for {disk_path} -> {mid}")
                count += 1

        # ── Mode 2: match_ids array ──────────────────────────────────────
        elif match_ids:
            for mid in match_ids:
                await session.execute(text("""
                    UPDATE dms_folder_matches SET accepted = true
                    WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
                """), {"mid": mid, "tid": tid})
            count = len(match_ids)

        # ── Mode 3: min_score threshold ──────────────────────────────────
        elif min_score is not None:
            result = await session.execute(text("""
                UPDATE dms_folder_matches SET accepted = true
                WHERE TRIM(tenant_id) = :tid
                  AND accepted = false
                  AND score >= :score
                RETURNING id
            """), {"tid": tid, "score": float(min_score)})
            count = len(result.fetchall())

        await session.commit()

    from fastapi.responses import JSONResponse
    return JSONResponse({"status": "ok", "accepted": count})


@router.get("/folder-reconciliation/search-matters")
async def folder_recon_search_matters(request: Request, q: str = "", user=Depends(get_current_user)):
    """Search matters for manual folder assignment."""
    tid = _tenant_id(request)
    if not q or len(q) < 2:
        from fastapi.responses import JSONResponse
        return JSONResponse([])

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT m.id::text, m.matter_name, m.matter_number, c.client_name
                    FROM matters m
                    LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
                    WHERE TRIM(m.tenant_id) = :tid
                      AND (m.matter_name ILIKE :q OR c.client_name ILIKE :q
                           OR m.matter_number ILIKE :q)
                    ORDER BY c.client_name, m.matter_name
                    LIMIT 20
                """),
                {"tid": tid, "q": f"%{q}%"},
            )
        ).mappings().all()
    from fastapi.responses import JSONResponse
    return JSONResponse([dict(r) for r in rows])


@router.post("/folder-reconciliation/sync-matter/{matter_id}")
async def folder_recon_sync_matter(request: Request, matter_id: str, user=Depends(get_current_user)):
    """Execute folder sync using matter_sync.sync_mapping_files directly."""
    if not _require_admin(user, request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Unauthorized"}, status_code=403)

    tid = _tenant_id(request)

    async with AsyncSessionLocal() as session:
        matches = (await session.execute(text("""
            SELECT fm.id::text, fm.folder_path, fm.best_disk_path
            FROM dms_folder_matches fm
            WHERE fm.matter_id = CAST(:mid AS uuid)
              AND TRIM(fm.tenant_id) = :tid AND fm.accepted = true
        """), {"mid": matter_id, "tid": tid})).mappings().all()

        log.warning(f"sync-matter: matter_id={matter_id}, tid={tid}, matches_found={len(matches)}")
        for mm in matches:
            log.warning(f"  match: folder_path={mm['folder_path']}, disk_path={mm['best_disk_path']}")

        if not matches:
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "No accepted matches"}, status_code=400)

        matter = (await session.execute(text("""
            SELECT m.matter_name, c.client_name, m.folder_path as legacy_path
            FROM matters m LEFT JOIN clients c ON m.client_id = c.id
            WHERE m.id = CAST(:mid AS uuid) AND TRIM(m.tenant_id) = :tid
        """), {"mid": matter_id, "tid": tid})).mappings().fetchone()

    if not matter:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Matter not found"}, status_code=404)

    legacy_path = (matter["legacy_path"] or "") if matter else ""

    # Read sync_mode from request body (default: "smart")
    sync_mode = "smart"
    try:
        body = await request.json()
        sync_mode = body.get("sync_mode", "smart")
    except Exception:
        pass
    skip_remap = (sync_mode == "raw")
    log.warning(f"sync-matter: sync_mode={sync_mode}, skip_remap={skip_remap}")

    # Import sync module — already patched with no-op assert and graceful Redis
    import importlib.util, sys
    mod_path = "/app/modules/dms/jobs/matter_sync.py"
    if "matter_sync_mod" in sys.modules:
        del sys.modules["matter_sync_mod"]
    if "matter_sync_mod" in sys.modules:
        del sys.modules["matter_sync_mod"]
    if "matter_sync_mod" not in sys.modules:
        spec = importlib.util.spec_from_file_location("matter_sync_mod", mod_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["matter_sync_mod"] = mod
        spec.loader.exec_module(mod)
    else:
        mod = sys.modules["matter_sync_mod"]

    parent_job_id = f"web-{matter_id[:8]}"
    results = []
    total_copied = 0
    total_skipped = 0
    total_errors = 0

    import asyncio

    for idx, m in enumerate(matches):
        try:
            r = await asyncio.to_thread(
                mod.sync_mapping_files,
                tenant_id=tid.strip(),
                matter_id=matter_id,
                folder_path=m["folder_path"] or "",
                disk_root=m["best_disk_path"] or "",
                parent_job_id=parent_job_id,
                mapping_idx=idx,
                legacy_source_path=m["folder_path"] or legacy_path or "",
                skip_remap=skip_remap,
            )
            results.append(r)
            total_copied += int(r.get("copied") or 0)
            total_skipped += int(r.get("skipped_existing") or 0)
            total_errors += int(r.get("errors") or 0)
            log.warning(f"sync mapping {idx}: result={r}")
        except Exception as e:
            log.error(f"sync mapping {idx} failed: {e}")
            results.append({"error": str(e)})
            total_errors += 1

    from fastapi.responses import JSONResponse
    return JSONResponse({
        "status": "ok",
        "matter_id": matter_id,
        "files_copied": total_copied,
        "files_skipped": total_skipped,
        "errors": total_errors,
        "mappings_processed": len(matches),
        "details": results,
        "sync_mode": sync_mode,
        "message": f"{total_copied} copied, {total_skipped} existing, {total_errors} errors ({sync_mode} mode)",
    })


@router.get("/folder-reconciliation/sync-status/{job_id}")
async def folder_recon_sync_status(request: Request, job_id: str, user=Depends(get_current_user)):
    """Poll sync job status. For now returns immediate complete since sync is synchronous."""
    from fastapi.responses import JSONResponse
    return JSONResponse({"status": "complete", "job_id": job_id})


@router.get("/folder-reconciliation/browse")
async def folder_recon_browse(request: Request, share: str = "clients", prefix: str = "", user=Depends(get_current_user)):
    """Browse legacy QNAP shares for folder-to-matter mapping."""
    import os

    SHARE_ROOTS = {
        "clients": "/mnt/clients",
        "docsend": "/mnt/docsend",
    }
    base = SHARE_ROOTS.get(share, "/mnt/clients")

    target = os.path.join(base, prefix) if prefix else base
    # Security: must stay under the share root
    target = os.path.realpath(target)
    if not target.startswith(os.path.realpath(base)):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not os.path.isdir(target):
        from fastapi.responses import JSONResponse
        return JSONResponse({"folders": [], "files": 0, "path": prefix, "share": share})

    folders = []
    file_count = 0
    try:
        for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
            if entry.name.startswith('.') or entry.name.startswith('@'):
                continue
            if entry.is_dir(follow_symlinks=False):
                try:
                    fc = sum(1 for f in os.scandir(entry.path)
                             if f.is_file() and not f.name.startswith('.'))
                    sc = sum(1 for f in os.scandir(entry.path)
                             if f.is_dir() and not f.name.startswith('.'))
                except (PermissionError, OSError):
                    fc = 0
                    sc = 0
                folders.append({
                    "name": entry.name,
                    "path": os.path.join(prefix, entry.name) if prefix else entry.name,
                    "full_path": entry.path,
                    "file_count": fc,
                    "subfolder_count": sc,
                })
            elif entry.is_file():
                file_count += 1
    except (PermissionError, OSError) as e:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": str(e)}, status_code=500)

    from fastapi.responses import JSONResponse
    return JSONResponse({"folders": folders, "files": file_count, "path": prefix, "share": share})


@router.post("/folder-reconciliation/seed-all-matters")
async def folder_recon_seed_all(request: Request, user=Depends(get_current_user)):
    """Seed 14-folder litigation structure for all matters missing it."""
    if not _require_admin(user, request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Unauthorized"}, status_code=403)
    tid = _tenant_id(request)

    import subprocess
    try:
        proc = subprocess.run(
            ["python3", "/app/jobs/automatch_folders.py", "--tenant", tid, "--execute"],
            capture_output=True, text=True, timeout=600,
        )
        from fastapi.responses import JSONResponse
        return JSONResponse({
            "status": "ok",
            "stdout": proc.stdout[-2000:] if proc.stdout else "",
        })
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/folder-reconciliation/matter-mappings/{matter_id}")
async def folder_recon_matter_mappings(request: Request, matter_id: str, user=Depends(get_current_user)):
    """Get all folder mappings for a specific matter."""
    tid = _tenant_id(request)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT fm.id::text, fm.folder_path, fm.score, fm.accepted,
                           fm.best_disk_path, fm.disk_file_count
                    FROM dms_folder_matches fm
                    WHERE fm.matter_id = CAST(:mid AS uuid)
                      AND TRIM(fm.tenant_id) = :tid
                    ORDER BY fm.folder_path
                """),
                {"mid": matter_id, "tid": tid},
            )
        ).mappings().all()
    from fastapi.responses import JSONResponse
    return JSONResponse([dict(r) for r in rows])


@router.delete("/folder-reconciliation/remove-mapping/{mapping_id}")
async def folder_recon_remove_mapping(request: Request, mapping_id: str, user=Depends(get_current_user)):
    """Remove a folder mapping (set accepted=false)."""
    if not _require_admin(user, request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Unauthorized"}, status_code=403)
    tid = _tenant_id(request)
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE dms_folder_matches SET accepted = false
                WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
            """),
            {"mid": mapping_id, "tid": tid},
        )
        await session.commit()
    from fastapi.responses import JSONResponse
    return JSONResponse({"status": "ok"})



@router.get("/folder-reconciliation/ai-match-stream")
async def folder_recon_ai_match_stream(request: Request, user=Depends(get_current_user)):
    """SSE streaming AI folder matching — sends results one at a time."""
    from starlette.responses import StreamingResponse
    from cryptography.fernet import Fernet
    import base64, httpx, json as _json

    if not _require_admin(user, request):
        async def err():
            yield f"data: {_json.dumps({'error': 'Unauthorized'})}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    tid = _tenant_id(request)

    # Get API key
    secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    key_bytes = (secret[:32]).encode().ljust(32, b"0")
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    f = Fernet(fernet_key)

    async with AsyncSessionLocal() as session:
        key_row = (await session.execute(text(
            "SELECT encrypted_key FROM credentials_vault "
            "WHERE tenant_id = :tid AND provider = 'anthropic' AND key_type = 'api_key'"
        ), {"tid": tid})).fetchone()

    if not key_row:
        async def err():
            yield f"data: {_json.dumps({'error': 'No Anthropic API key'})}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    api_key = f.decrypt(key_row[0].encode()).decode()

    # Fetch all unmatched
    async with AsyncSessionLocal() as session:
        matches = (await session.execute(text("""
            SELECT fm.id::text as match_id, fm.folder_path, fm.score,
                   fm.best_disk_path, fm.disk_file_count,
                   m.matter_name, c.client_name, m.id::text as matter_id,
                   c.id::text as client_id
            FROM dms_folder_matches fm
            LEFT JOIN matters m ON fm.matter_id = m.id
            LEFT JOIN clients c ON m.client_id = c.id
            WHERE TRIM(fm.tenant_id) = :tid
              AND (fm.accepted = false OR fm.accepted IS NULL)
            ORDER BY fm.score DESC
        """), {"tid": tid})).mappings().all()

    total = len(matches)

    async def generate():
        yield f"data: {_json.dumps({'type': 'start', 'total': total})}\n\n"

        for idx, match in enumerate(matches):
            folder = match["folder_path"]

            # Gather context
            async with AsyncSessionLocal() as session:
                file_samples = (await session.execute(text("""
                    SELECT name, extension, size_bytes
                    FROM file_inventory
                    WHERE TRIM(tenant_id) = :tid AND root_folder = :folder
                      AND entry_type = 'file'
                    ORDER BY size_bytes DESC NULLS LAST LIMIT 15
                """), {"tid": tid, "folder": folder})).mappings().all()

                subfolders = (await session.execute(text("""
                    SELECT DISTINCT name, child_file_count FROM file_inventory
                    WHERE TRIM(tenant_id) = :tid AND root_folder = :folder
                      AND entry_type = 'folder' AND depth = 2
                    ORDER BY name LIMIT 50
                """), {"tid": tid, "folder": folder})).mappings().all()
                subfolder_count = len(subfolders)

                time_records = (await session.execute(text("""
                    SELECT tc.ts_name as ts_client, ts.reference_id as ts_matter,
                           ts.slip_date, ts.narrative, ts.hours
                    FROM ts_slips ts
                    JOIN ts_clients tc ON ts.source_client_id = tc.ts_client_id
                      AND TRIM(ts.tenant_id) = TRIM(tc.tenant_id)
                    WHERE TRIM(ts.tenant_id) = :tid
                      AND (tc.ts_name ILIKE :q1 OR tc.ts_name ILIKE :q2)
                    ORDER BY ts.slip_date DESC LIMIT 10
                """), {"tid": tid,
                       "q1": f"%{folder}%",
                       "q2": f"%{folder.split('-')[0].split(',')[0].strip()}%"
                      })).mappings().all()

                client_matters = []
                if match["client_id"]:
                    client_matters = (await session.execute(text("""
                        SELECT m.id::text, m.matter_name, m.matter_number, m.status
                        FROM matters m
                        WHERE m.client_id = CAST(:cid AS uuid)
                          AND TRIM(m.tenant_id) = :tid
                        ORDER BY m.matter_name
                    """), {"cid": match["client_id"], "tid": tid})).mappings().all()

                folder_prefix = folder.split("-")[0].split(",")[0].split("(")[0].strip()
                candidate_clients = (await session.execute(text("""
                    SELECT c.id::text, c.client_name,
                           (SELECT COUNT(*) FROM matters m2 WHERE m2.client_id = c.id
                            AND TRIM(m2.tenant_id) = :tid) as matter_count
                    FROM clients c
                    WHERE TRIM(c.tenant_id) = :tid
                      AND (c.client_name ILIKE :q1 OR c.client_name ILIKE :q2)
                    ORDER BY c.client_name LIMIT 10
                """), {"tid": tid, "q1": f"%{folder_prefix}%", "q2": f"%{folder}%"
                      })).mappings().all()

            # Build context
            ctx = [f'LEGACY FOLDER: "{folder}"']
            ctx.append(f'Current proposed match: Client="{match["client_name"]}" Matter="{match["matter_name"]}" (score={match["score"]:.2f})')
            if file_samples:
                ctx.append("Sample files: " + ", ".join(r["name"] for r in file_samples[:10]))
            if subfolders:
                sf_list = ", ".join(f'{r["name"]} ({r["child_file_count"] or 0} files)' for r in subfolders[:30])
                ctx.append(f"Subfolders ({subfolder_count} total): {sf_list}")
                if subfolder_count > 5:
                    ctx.append("NOTE: This folder has many subfolders — each likely represents a separate matter. List the subfolder-to-matter mappings you can identify.")
            if time_records:
                trecs = [f'  {tr["ts_client"]}/{tr["ts_matter"]} {tr["slip_date"]} {tr["hours"]}h: {(tr["narrative"] or "")[:100]}' for tr in time_records[:5]]
                ctx.append("Time records:\n" + "\n".join(trecs))
            if client_matters:
                ctx.append(f'All matters for "{match["client_name"]}": ' + ", ".join(f'{m["matter_name"]} ({m["status"]})' for m in client_matters))
            if candidate_clients:
                ctx.append("Candidate clients: " + ", ".join(f'{c["client_name"]} ({c["matter_count"]}m)' for c in candidate_clients))

            context = "\n".join(ctx)
            model = "claude-haiku-4-5-20251001" if (match["score"] or 0) >= 0.7 else "claude-sonnet-4-20250514"

            prompt = f"""You are matching legacy file folders to client matters in a law firm.

CONTEXT:
{context}

TASK: Is the current match correct for folder "{folder}"? Use file names, time records, and candidates.
If correct, confirm. If wrong, propose the correct client and matter. If no match, say so.

Respond in EXACTLY this JSON (no markdown). For single-matter folders:
{{"match_correct": true/false, "proposed_client_name": "...", "proposed_matter_name": "...", "proposed_matter_id": "uuid or null", "confidence": 0.0-1.0, "reasoning": "one sentence", "subfolder_matches": null}}

For multi-matter client folders (many subfolders = separate matters), also include:
{{"match_correct": true/false, "proposed_client_name": "...", "proposed_matter_name": "General", "proposed_matter_id": null, "confidence": 0.0-1.0, "reasoning": "Client folder with N matter subfolders", "subfolder_matches": [{{"subfolder": "name", "proposed_matter": "matter name"}}]}}"""

            proposal = {
                "type": "proposal",
                "index": idx + 1,
                "total": total,
                "match_id": match["match_id"],
                "folder_path": folder,
                "disk_path": match["best_disk_path"] or "",
                "current_score": float(match["score"] or 0),
                "current_client": match["client_name"],
                "current_matter": match["matter_name"],
                "current_matter_id": match["matter_id"],
                "file_count": match["disk_file_count"],
                "model": model,
                "subfolder_count": subfolder_count if 'subfolder_count' in dir() else 0,
            }

            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={
                            "x-api-key": api_key,
                            "anthropic-version": "2023-06-01",
                            "content-type": "application/json",
                        },
                        json={
                            "model": model,
                            "max_tokens": 300,
                            "messages": [{"role": "user", "content": prompt}],
                        },
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    ai_text = data.get("content", [{}])[0].get("text", "")
                    clean = ai_text.strip()
                    if clean.startswith("```"):
                        clean = clean.split("\n", 1)[1].rsplit("```", 1)[0]
                    try:
                        ai_result = _json.loads(clean)
                    except _json.JSONDecodeError:
                        ai_result = {"match_correct": None, "reasoning": ai_text[:200], "confidence": 0}
                    proposal["ai_match"] = ai_result
                else:
                    proposal["error"] = f"API {resp.status_code}"
            except Exception as e:
                proposal["error"] = str(e)[:200]

            yield f"data: {_json.dumps(proposal)}\n\n"

        yield f"data: {_json.dumps({'type': 'done', 'total': total})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/folder-reconciliation/ai-accept")
async def folder_recon_ai_accept(request: Request, user=Depends(get_current_user)):
    """Accept an AI-proposed match — update dms_folder_matches with new matter_id if changed."""
    if not _require_admin(user, request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Unauthorized"}, status_code=403)
    tid = _tenant_id(request)
    body = await request.json()
    match_id = body.get("match_id")
    new_matter_id = body.get("matter_id")  # If AI proposed a different matter
    accept = body.get("accept", True)

    if not match_id:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "match_id required"}, status_code=400)

    async with AsyncSessionLocal() as session:
        if accept:
            if new_matter_id:
                # AI proposed a different matter — update the match
                await session.execute(text("""
                    UPDATE dms_folder_matches
                    SET matter_id = CAST(:mid AS uuid), accepted = true, score = 0.95
                    WHERE id = CAST(:fid AS uuid) AND TRIM(tenant_id) = :tid
                """), {"mid": new_matter_id, "fid": match_id, "tid": tid})
            else:
                # Accept current match as-is
                await session.execute(text("""
                    UPDATE dms_folder_matches SET accepted = true
                    WHERE id = CAST(:fid AS uuid) AND TRIM(tenant_id) = :tid
                """), {"fid": match_id, "tid": tid})
        else:
            # Reject — mark as reviewed but not accepted
            await session.execute(text("""
                UPDATE dms_folder_matches SET accepted = false
                WHERE id = CAST(:fid AS uuid) AND TRIM(tenant_id) = :tid
            """), {"fid": match_id, "tid": tid})
        await session.commit()

    from fastapi.responses import JSONResponse
    return JSONResponse({"status": "ok", "match_id": match_id, "accepted": accept})


@router.post("/folder-reconciliation/scan-inventory")
async def scan_legacy_inventory(request: Request, user=Depends(get_current_user)):
    """Trigger the legacy filesystem inventory agent."""
    if not _require_admin(user, request):
        return {"error": "Unauthorized"}
    tid = _tenant_id(request)

    import subprocess
    root = "/mnt/legacy-qnap/D Drive Backup/Clients"

    try:
        proc = subprocess.run(
            ["python3", "/app/jobs/inventory_legacy_filesystem.py",
             "--root", root, "--tenant", tid],
            capture_output=True, text=True, timeout=1800,  # 30 min max
        )

        result = {"files": 0, "folders": 0, "matched": 0, "total_root_folders": 0}
        for line in proc.stdout.split("\n"):
            if "files:" in line.lower() and "entries" not in line.lower():
                try:
                    result["files"] = int(line.split(":")[1].strip().split()[0])
                except (ValueError, IndexError):
                    pass
            if "folders:" in line.lower():
                try:
                    result["folders"] = int(line.split(":")[1].strip().split()[0])
                except (ValueError, IndexError):
                    pass

        # Get counts from DB
        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    text("""
                        SELECT
                            COUNT(*) FILTER (WHERE entry_type = 'file') AS files,
                            COUNT(*) FILTER (WHERE entry_type = 'folder') AS folders,
                            COUNT(DISTINCT root_folder) FILTER (WHERE match_method IS NOT NULL) AS matched,
                            COUNT(DISTINCT root_folder) AS total_root_folders
                        FROM file_inventory
                        WHERE tenant_id = :tid
                          AND scan_run_id = (
                            SELECT scan_run_id FROM file_inventory
                            WHERE tenant_id = :tid
                            ORDER BY created_at DESC LIMIT 1
                          )
                    """),
                    {"tid": tid},
                )
            ).fetchone()
            if row:
                result = {
                    "files": row[0] or 0,
                    "folders": row[1] or 0,
                    "matched": row[2] or 0,
                    "total_root_folders": row[3] or 0,
                }

        from fastapi.responses import JSONResponse
        return JSONResponse(result)

    except subprocess.TimeoutExpired:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Scan timed out (>30 min). Run from CLI instead."}, status_code=504)
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": str(e)}, status_code=500)



# ═══════════════════════════════════════════════════════════════════════════════
#  AI ADMINISTRATION CHAT — SSE proxy to Anthropic API with MCP server access
# ═══════════════════════════════════════════════════════════════════════════════

import httpx as _httpx

_ADMIN_SYSTEM_PROMPT = """You are the Praesidium Administration Assistant — an AI embedded in a legal practice management platform's admin panel. You have direct MCP tool access to the firm's database and infrastructure.

## Capabilities

### Onboarding (guided sequence)
1. Connect Time — Import billing data (Timeslips, Tabs3, Clio)
2. Reconcile Import — Fix duplicates, assign matter types
3. Embed Time — Chunk and embed time records for AI billing intelligence
4. Map Legacy Shares — Connect legacy file shares
5. Inventory Legacy Shares — Catalog folders/files
6. Match Legacy Shares — AI-match legacy folders to matters
7. Move Confirmed Matches — Sync matched folders into Praesidium
8. Clean Up the Rest — Unmatched folders, raw copies, manual assignments
9. Map Email & Other Connectors — Email, calendar, other sources

### Platform Configuration
- Register/configure connectors (connector_registry, tenant_connectors)
- Configure dashboard widgets (widget_registry, dashboard_panel_registry)
- Manage page layouts (page_registry, layout_registry)
- Troubleshoot data quality
- Run diagnostic queries

## Current Tenant State
{onboarding_state}

## MCP Tool Access

This platform has two MCP servers that provide direct database and infrastructure access when connected via Claude.ai or the Claude desktop app:

**Praesidium App** (mcp-user.praesidium-legal.com/mcp) — Practice tools:
query_matters, query_clients, query_documents, query_timeslips, query_invoices, query_trust_ledger, query_ts_clients, query_timekeepers, browse_matter_files, upload_document, move_document, draft_document, search_elasticsearch

**Praesidium Admin** (mcp-admin.praesidium-legal.com/mcp) — Infrastructure & onboarding tools:
inventory_legacy_shares, propose_folder_matches, bulk_update_matches, sample_folder_files, create_matter_for_folder, sync_matter_files, compare_inventories, raw_copy_folder, run_readonly_query, deploy_tmp_script, docker_status, docker_logs, read_container_file, read_host_file, list_directory, es_indices, search_elasticsearch, nginx_config, alembic_current, query_schema, list_tables

When answering from this chat widget, you do NOT have live tool access. Instead:
- Use the onboarding state data provided above to answer questions about current status.
- For actions that require tool access, provide the exact MCP tool call or SQL query the admin should run, or suggest they use the Claude.ai project with both MCP servers connected.
- Be specific about which tool to use and what parameters to pass.

## Behavior
- Direct and practical. Your audience is a firm admin or IT professional.
- Answer questions about onboarding status using the state data above.
- When actions are needed, provide specific instructions (tool names, SQL, or CLI commands).
- Format data as tables when showing lists.
- Flag data quality issues proactively.
"""


async def _build_onboarding_state(tid: str) -> dict:
    state = {}
    async with AsyncSessionLocal() as session:
        state["ts_clients"] = (await session.execute(
            text("SELECT COUNT(*) FROM ts_clients WHERE TRIM(tenant_id) = :tid"), {"tid": tid}
        )).scalar() or 0
        state["ts_slips"] = (await session.execute(
            text("SELECT COUNT(*) FROM ts_slips WHERE TRIM(tenant_id) = :tid"), {"tid": tid}
        )).scalar() or 0
        state["clients"] = (await session.execute(
            text("SELECT COUNT(*) FROM clients WHERE TRIM(tenant_id) = :tid"), {"tid": tid}
        )).scalar() or 0
        state["matters"] = (await session.execute(
            text("SELECT COUNT(*) FROM matters WHERE TRIM(tenant_id) = :tid"), {"tid": tid}
        )).scalar() or 0
        state["matters_typed"] = (await session.execute(
            text("SELECT COUNT(*) FROM matters WHERE TRIM(tenant_id) = :tid AND matter_type IS NOT NULL AND matter_type != ''"), {"tid": tid}
        )).scalar() or 0
        try:
            state["embedded_chunks"] = (await session.execute(
                text("SELECT COUNT(*) FROM billing_chunks WHERE TRIM(tenant_id) = :tid AND embedding IS NOT NULL"), {"tid": tid}
            )).scalar() or 0
            state["total_chunks"] = (await session.execute(
                text("SELECT COUNT(*) FROM billing_chunks WHERE TRIM(tenant_id) = :tid"), {"tid": tid}
            )).scalar() or 0
        except Exception:
            state["embedded_chunks"] = 0; state["total_chunks"] = 0
        try:
            fs = (await session.execute(text("""SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE accepted = true) as accepted,
                COUNT(*) FILTER (WHERE accepted = false) as rejected,
                COUNT(*) FILTER (WHERE accepted IS NULL) as unreviewed
            FROM dms_folder_matches WHERE TRIM(tenant_id) = :tid"""), {"tid": tid})).fetchone()
            state["folder_matches_total"] = fs[0] or 0
            state["folder_matches_accepted"] = fs[1] or 0
            state["folder_matches_rejected"] = fs[2] or 0
            state["folder_matches_unreviewed"] = fs[3] or 0
        except Exception:
            for k in ["folder_matches_total","folder_matches_accepted","folder_matches_rejected","folder_matches_unreviewed"]:
                state[k] = 0
        state["dms_documents"] = (await session.execute(
            text("SELECT COUNT(*) FROM dms_documents WHERE TRIM(tenant_id) = :tid"), {"tid": tid}
        )).scalar() or 0
        try:
            state["matters_with_files"] = (await session.execute(
                text("SELECT COUNT(DISTINCT matter_id) FROM matter_folders WHERE TRIM(tenant_id) = :tid AND disk_root LIKE '/mnt/praesidium%'"), {"tid": tid}
            )).scalar() or 0
        except Exception:
            state["matters_with_files"] = 0
        state["widgets"] = (await session.execute(text("SELECT COUNT(*) FROM widget_registry"))).scalar() or 0
        state["connectors"] = (await session.execute(text("SELECT COUNT(*) FROM connector_registry WHERE is_active = true"))).scalar() or 0
        state["has_anthropic_key"] = bool((await session.execute(
            text("SELECT 1 FROM credentials_vault WHERE tenant_id = :tid AND provider = 'anthropic' LIMIT 1"), {"tid": tid}
        )).fetchone())
    return state


def _format_state(s: dict) -> str:
    return "\n".join([
        "### Billing", f"- TS clients: {s.get('ts_clients',0)}", f"- Time slips: {s.get('ts_slips',0):,}",
        f"- Clients: {s.get('clients',0)}", f"- Matters: {s.get('matters',0)} ({s.get('matters_typed',0)} typed)",
        f"- Embedded chunks: {s.get('embedded_chunks',0):,}/{s.get('total_chunks',0):,}",
        "\n### Files",
        f"- DMS docs: {s.get('dms_documents',0):,}",
        f"- Folder matches: {s.get('folder_matches_accepted',0)} accepted, {s.get('folder_matches_unreviewed',0)} unreviewed ({s.get('folder_matches_total',0)} total)",
        f"- Matters with files: {s.get('matters_with_files',0)}",
        "\n### Platform",
        f"- Widgets: {s.get('widgets',0)}", f"- Connectors: {s.get('connectors',0)}",
        f"- Anthropic key: {'yes' if s.get('has_anthropic_key') else 'NO'}",
    ])


@router.get("/onboarding-state")
async def get_onboarding_state(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Unauthorized"}, status_code=403)
    tid = _tenant_id(request)
    from fastapi.responses import JSONResponse
    return JSONResponse(await _build_onboarding_state(tid))


@router.post("/ai-chat")
async def ai_chat_stream(request: Request, user=Depends(get_current_user)):
    """SSE streaming chat — Anthropic API with MCP servers."""
    from starlette.responses import StreamingResponse
    import json as _json

    if not _require_admin(user, request):
        async def err():
            yield f"data: {_json.dumps({'type':'error','text':'Unauthorized'})}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    tid = _tenant_id(request)
    body = await request.json()
    msgs = body.get("messages", [])
    if not msgs:
        async def err():
            yield f"data: {_json.dumps({'type':'error','text':'No messages'})}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    from cryptography.fernet import Fernet
    import base64

    async with AsyncSessionLocal() as session:
        kr = (await session.execute(text(
            "SELECT encrypted_key FROM credentials_vault WHERE tenant_id=:tid AND provider='anthropic' AND key_type='api_key'"
        ), {"tid": tid})).fetchone()
    if not kr:
        async def err():
            yield f"data: {_json.dumps({'type':'error','text':'No Anthropic API key. Add one under AI API Keys.'})}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    fk = base64.urlsafe_b64encode((secret[:32]).encode().ljust(32, b"0"))
    api_key = Fernet(fk).decrypt(kr[0].encode()).decode()

    try:
        state = await _build_onboarding_state(tid)
        state_text = _format_state(state)
    except Exception as _e:
        log.warning(f"Onboarding state query failed: {_e}")
        state_text = "State unavailable — query the database using your MCP tools."
    sys_prompt = _ADMIN_SYSTEM_PROMPT.replace("{onboarding_state}", state_text)

    mcp_admin = os.environ.get("MCP_ADMIN_URL", "https://mcp-admin.praesidium-legal.com/mcp")
    mcp_user = os.environ.get("MCP_USER_URL", "https://mcp-user.praesidium-legal.com/mcp")

    mcp_admin = os.environ.get("MCP_ADMIN_URL", "https://mcp-admin.praesidium-legal.com/mcp")
    mcp_user = os.environ.get("MCP_USER_URL", "https://mcp-user.praesidium-legal.com/mcp")

    api_body = {
        "model": body.get("model", "claude-sonnet-4-20250514"),
        "max_tokens": 16384, "stream": True,
        "system": sys_prompt, "messages": msgs,
        "mcp_servers": [
            {"type": "url", "url": mcp_user, "name": "praesidium-app"},
            {"type": "url", "url": mcp_admin, "name": "praesidium-admin"},
        ],
        "tools": [
            {"type": "mcp_toolset", "mcp_server_name": "praesidium-app"},
            {"type": "mcp_toolset", "mcp_server_name": "praesidium-admin"},
        ],
    }

    async def generate():
        try:
            async with _httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key":api_key,"anthropic-version":"2023-06-01","anthropic-beta":"mcp-client-2025-11-20","content-type":"application/json"},
                    json=api_body) as resp:
                    if resp.status_code != 200:
                        eb = ""
                        async for ch in resp.aiter_text(): eb += ch
                        yield f"data: {_json.dumps({'type':'error','text':f'API {resp.status_code}: {eb[:500]}'})}\n\n"
                        return
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "): continue
                        ds = line[6:]
                        if ds == "[DONE]": break
                        try:
                            ev = _json.loads(ds); et = ev.get("type","")
                            if et == "content_block_delta":
                                d = ev.get("delta",{})
                                if d.get("type") == "text_delta":
                                    yield f"data: {_json.dumps({'type':'text','text':d['text']})}\n\n"
                            elif et == "content_block_start":
                                bl = ev.get("content_block",{})
                                if bl.get("type") == "tool_use":
                                    yield f"data: {_json.dumps({'type':'tool_start','tool':bl.get('name',''),'id':bl.get('id','')})}\n\n"
                            elif et == "content_block_stop":
                                yield f"data: {_json.dumps({'type':'block_stop'})}\n\n"
                            elif et == "message_stop": break
                            elif et == "message_delta":
                                sr = ev.get("delta",{}).get("stop_reason")
                                if sr: yield f"data: {_json.dumps({'type':'stop','reason':sr})}\n\n"
                        except _json.JSONDecodeError: continue
        except _httpx.ReadTimeout:
            yield f"data: {_json.dumps({'type':'error','text':'Timeout'})}\n\n"
        except Exception as e:
            yield f"data: {_json.dumps({'type':'error','text':str(e)})}\n\n"
        yield f"data: {_json.dumps({'type':'done'})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")

# ── Stats Cards (HTMX fragment) ─────────────────────────────────────────────

@router.get("/stats", response_class=HTMLResponse)
async def tenant_admin_stats(request: Request, user=Depends(get_current_user)):
    """Return stat card HTML fragments for the command center top row."""
    if not _require_admin(user, request):
        return HTMLResponse("")
    tid = _tenant_id(request)

    doc_count = 0
    ocr_pending = 0
    ocr_done = 0
    folder_total = 0
    folder_accepted = 0
    matter_count = 0
    user_count = 0
    api_keys = 0

    try:
        async with AsyncSessionLocal() as session:
            # Documents
            r = await session.execute(
                text("SELECT COUNT(*) FROM dms_documents WHERE TRIM(tenant_id) = :tid"),
                {"tid": tid},
            )
            doc_count = r.scalar() or 0

            # OCR status
            r = await session.execute(
                text("""
                    SELECT
                        COUNT(*) FILTER (WHERE ocr_status = 'pending') as pending,
                        COUNT(*) FILTER (WHERE ocr_status = 'complete') as done
                    FROM scan_queue WHERE TRIM(tenant_id) = :tid
                """),
                {"tid": tid},
            )
            row = r.fetchone()
            if row:
                ocr_pending = row[0] or 0
                ocr_done = row[1] or 0

            # Folder matches
            r = await session.execute(
                text("""
                    SELECT
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE accepted = true) as accepted
                    FROM dms_folder_matches WHERE TRIM(tenant_id) = :tid
                """),
                {"tid": tid},
            )
            row = r.fetchone()
            if row:
                folder_total = row[0] or 0
                folder_accepted = row[1] or 0

            # Matters
            r = await session.execute(
                text("SELECT COUNT(*) FROM matters WHERE TRIM(tenant_id) = :tid"),
                {"tid": tid},
            )
            matter_count = r.scalar() or 0

            # Users
            r = await session.execute(
                text("SELECT COUNT(*) FROM users WHERE tenant_id = :tid AND is_active = true"),
                {"tid": tid},
            )
            user_count = r.scalar() or 0

            # API keys
            r = await session.execute(
                text("SELECT COUNT(*) FROM credentials_vault WHERE tenant_id = :tid AND key_type = 'api_key'"),
                {"tid": tid},
            )
            api_keys = r.scalar() or 0
    except Exception as e:
        log.error(f"[stats] error: {e}")

    folder_pct = round(folder_accepted / folder_total * 100) if folder_total > 0 else 0
    ocr_total = ocr_pending + ocr_done
    ocr_pct = round(ocr_done / ocr_total * 100) if ocr_total > 0 else 0

    html = f"""
    <div class="cc-stat">
      <div class="cc-stat-icon" style="background:#EFF6FF;">
        <svg width="18" height="18" fill="none" stroke="#1E40AF" viewBox="0 0 24 24" stroke-width="1.75">
          <path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
        </svg>
      </div>
      <div>
        <div class="cc-stat-value">{doc_count:,}</div>
        <div class="cc-stat-label">Documents</div>
        <div class="cc-stat-sub">{matter_count:,} matters</div>
      </div>
    </div>

    <div class="cc-stat">
      <div class="cc-stat-icon" style="background:#FEF3C7;">
        <svg width="18" height="18" fill="none" stroke="#92400E" viewBox="0 0 24 24" stroke-width="1.75">
          <path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
          <path stroke-linecap="round" stroke-linejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/>
        </svg>
      </div>
      <div>
        <div class="cc-stat-value">{ocr_done:,}</div>
        <div class="cc-stat-label">OCR Complete</div>
        <div class="cc-stat-sub">{ocr_pending:,} pending &middot; {ocr_pct}%</div>
      </div>
    </div>

    <div class="cc-stat">
      <div class="cc-stat-icon" style="background:#D1FAE5;">
        <svg width="18" height="18" fill="none" stroke="#065F46" viewBox="0 0 24 24" stroke-width="1.75">
          <path stroke-linecap="round" stroke-linejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/>
        </svg>
      </div>
      <div>
        <div class="cc-stat-value">{folder_accepted}</div>
        <div class="cc-stat-label">Folders Mapped</div>
        <div class="cc-stat-sub">{folder_total} total &middot; {folder_pct}%</div>
      </div>
    </div>

    <div class="cc-stat">
      <div class="cc-stat-icon" style="background:#EDE9FE;">
        <svg width="18" height="18" fill="none" stroke="#5B21B6" viewBox="0 0 24 24" stroke-width="1.75">
          <path stroke-linecap="round" stroke-linejoin="round" d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z"/>
        </svg>
      </div>
      <div>
        <div class="cc-stat-value">{user_count}</div>
        <div class="cc-stat-label">Active Users</div>
        <div class="cc-stat-sub">{api_keys} API key{'s' if api_keys != 1 else ''} configured</div>
      </div>
    </div>
    """
    return HTMLResponse(html)


# ── Onboarding Checklist (HTMX fragment) ────────────────────────────────────

@router.get("/onboarding-checklist", response_class=HTMLResponse)
async def tenant_admin_onboarding_checklist(request: Request, user=Depends(get_current_user)):
    """Return onboarding step checklist as HTML fragment."""
    if not _require_admin(user, request):
        return HTMLResponse("")
    tid = _tenant_id(request)

    # Gather state for each onboarding step
    state = {}
    try:
        async with AsyncSessionLocal() as session:
            # Step 1: Billing data imported?
            r = await session.execute(
                text("SELECT COUNT(*) FROM ts_clients WHERE TRIM(tenant_id) = :tid"),
                {"tid": tid},
            )
            state["ts_clients"] = r.scalar() or 0

            r = await session.execute(
                text("SELECT COUNT(*) FROM ts_slips WHERE TRIM(tenant_id) = :tid"),
                {"tid": tid},
            )
            state["ts_slips"] = r.scalar() or 0

            # Step 3: Billing chunks embedded?
            r = await session.execute(
                text("SELECT COUNT(*) FROM billing_chunks WHERE TRIM(tenant_id) = :tid"),
                {"tid": tid},
            )
            state["billing_chunks"] = r.scalar() or 0

            # Step 4-6: Folder inventory and matching
            r = await session.execute(
                text("SELECT COUNT(*) FROM file_inventory WHERE TRIM(tenant_id) = :tid"),
                {"tid": tid},
            )
            state["file_inventory"] = r.scalar() or 0

            r = await session.execute(
                text("""
                    SELECT
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE accepted = true) as accepted,
                        COUNT(*) FILTER (WHERE accepted IS NULL OR accepted = false) as unmatched
                    FROM dms_folder_matches WHERE TRIM(tenant_id) = :tid
                """),
                {"tid": tid},
            )
            row = r.fetchone()
            state["folder_total"] = row[0] or 0 if row else 0
            state["folder_accepted"] = row[1] or 0 if row else 0
            state["folder_unmatched"] = row[2] or 0 if row else 0

            # Step 7: Files synced?
            r = await session.execute(
                text("SELECT COUNT(*) FROM dms_documents WHERE TRIM(tenant_id) = :tid"),
                {"tid": tid},
            )
            state["dms_docs"] = r.scalar() or 0

            # Step 9: Connectors
            r = await session.execute(
                text("""
                    SELECT COUNT(*) FROM tenant_connectors
                    WHERE TRIM(tenant_id) = :tid AND status = 'active'
                """),
                {"tid": tid},
            )
            state["connectors"] = r.scalar() or 0

            # API keys
            r = await session.execute(
                text("SELECT COUNT(*) FROM credentials_vault WHERE tenant_id = :tid AND key_type = 'api_key'"),
                {"tid": tid},
            )
            state["api_keys"] = r.scalar() or 0
    except Exception as e:
        log.error(f"[onboarding-checklist] error: {e}")

    steps = [
        {
            "num": 1, "title": "Import Billing Data",
            "desc": f"{state.get('ts_clients', 0)} clients, {state.get('ts_slips', 0)} time slips",
            "done": state.get("ts_slips", 0) > 0,
        },
        {
            "num": 2, "title": "Reconcile Import",
            "desc": "Fix duplicates, assign matter types",
            "done": state.get("ts_slips", 0) > 0,  # implicitly done if imported
        },
        {
            "num": 3, "title": "Embed Billing for AI",
            "desc": f"{state.get('billing_chunks', 0)} chunks embedded",
            "done": state.get("billing_chunks", 0) > 0,
        },
        {
            "num": 4, "title": "Inventory Legacy Shares",
            "desc": f"{state.get('file_inventory', 0):,} files cataloged",
            "done": state.get("file_inventory", 0) > 0,
        },
        {
            "num": 5, "title": "AI Folder Matching",
            "desc": f"{state.get('folder_total', 0)} proposals generated",
            "done": state.get("folder_total", 0) > 0,
        },
        {
            "num": 6, "title": "Review & Accept Matches",
            "desc": f"{state.get('folder_accepted', 0)} accepted, {state.get('folder_unmatched', 0)} remaining",
            "done": state.get("folder_accepted", 0) > 0 and state.get("folder_unmatched", 0) == 0,
        },
        {
            "num": 7, "title": "Sync Files to Praesidium",
            "desc": f"{state.get('dms_docs', 0):,} documents in DMS",
            "done": state.get("dms_docs", 0) > 0,
        },
        {
            "num": 8, "title": "Configure API Keys",
            "desc": f"{state.get('api_keys', 0)} key{'s' if state.get('api_keys', 0) != 1 else ''} configured",
            "done": state.get("api_keys", 0) > 0,
        },
        {
            "num": 9, "title": "Connect Data Sources",
            "desc": f"{state.get('connectors', 0)} active connector{'s' if state.get('connectors', 0) != 1 else ''}",
            "done": state.get("connectors", 0) > 0,
        },
    ]

    # Find current active step
    active_idx = None
    for i, s in enumerate(steps):
        if not s["done"]:
            active_idx = i
            break

    completed = sum(1 for s in steps if s["done"])
    total = len(steps)
    pct = round(completed / total * 100) if total else 0

    html_parts = []
    html_parts.append(f"""
    <div style="margin-bottom:12px;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
        <span style="font-size:12px; font-weight:600; color:var(--text);">{completed}/{total} Complete</span>
        <span style="font-size:11px; color:var(--muted);">{pct}%</span>
      </div>
      <div style="height:4px; background:#F3F4F6; border-radius:2px; overflow:hidden;">
        <div style="height:100%; width:{pct}%; background:var(--accent); border-radius:2px; transition:width 300ms;"></div>
      </div>
    </div>
    """)

    for i, s in enumerate(steps):
        if s["done"]:
            dot_class = "done"
            dot_content = "&#10003;"
        elif i == active_idx:
            dot_class = "active"
            dot_content = str(s["num"])
        else:
            dot_class = "pending"
            dot_content = str(s["num"])

        html_parts.append(f"""
        <div class="cc-step">
          <div class="cc-step-dot {dot_class}">{dot_content}</div>
          <div>
            <div class="cc-step-title">{s["title"]}</div>
            <div class="cc-step-desc">{s["desc"]}</div>
          </div>
        </div>
        """)

    return HTMLResponse("\n".join(html_parts))


# ── Service Status (HTMX fragment) ──────────────────────────────────────────

@router.get("/service-status", response_class=HTMLResponse)
async def tenant_admin_service_status(request: Request, user=Depends(get_current_user)):
    """Return service health indicators as HTML fragment."""
    if not _require_admin(user, request):
        return HTMLResponse("")

    # For now, check database connectivity and report container info
    db_ok = False
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            db_ok = True
    except Exception:
        pass

    services = [
        {"name": "PostgreSQL Database", "up": db_ok, "info": "Port 5432"},
        {"name": "Redis / Job Queue", "up": True, "info": "Port 6379"},
        {"name": "Elasticsearch", "up": True, "info": "Port 9200"},
        {"name": "MCP App Server", "up": True, "info": "mcp-user.praesidium-legal.com"},
        {"name": "MCP Admin Server", "up": True, "info": "mcp-admin.praesidium-legal.com"},
        {"name": "Web Application", "up": True, "info": "Running"},
    ]

    html_parts = [
        '<div style="padding:14px 16px;">',
        '<div style="font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:0.08em; color:var(--muted); margin-bottom:10px;">Platform Services</div>',
    ]

    for svc in services:
        dot = "up" if svc["up"] else "down"
        html_parts.append(f"""
        <div class="cc-svc">
          <div class="cc-svc-dot {dot}"></div>
          <div class="cc-svc-name">{svc["name"]}</div>
          <div class="cc-svc-info">{svc["info"]}</div>
        </div>
        """)

    html_parts.append("</div>")
    return HTMLResponse("\n".join(html_parts))
# ── Data Sources ──────────────────────────────────────────────────────────────

@router.get("/data-sources", response_class=HTMLResponse)
async def data_sources_list(request: Request, user=Depends(get_current_user)):
    """List registered data sources and browse interface."""
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)

    async with AsyncSessionLocal() as session:
        sources = (
            await session.execute(
                text("""
                    SELECT cs.id::text, cs.source_name, cs.mount_path,
                           cs.read_only, cs.description, cs.status,
                           cs.source_config, cs.last_sync_at, cs.created_at,
                           (SELECT COUNT(*) FROM file_inventory fi
                            WHERE fi.source_id = cs.id) as file_count,
                           (SELECT COUNT(*) FROM dms_folder_matches fm
                            WHERE fm.source_id = cs.id AND fm.accepted = true) as matched_folders,
                           (SELECT COUNT(*) FROM dms_folder_matches fm
                            WHERE fm.source_id = cs.id) as total_folders
                    FROM connector_sources cs
                    WHERE TRIM(cs.tenant_id) = :tid
                      AND cs.connector_type = 'file_source'
                    ORDER BY cs.created_at
                """),
                {"tid": tid},
            )
        ).mappings().all()

    t = _templates(request)
    return t.TemplateResponse(
        request,
        "tenant_admin/data_sources.html",
        {"user": user, "sources": [dict(s) for s in sources]},
    )


@router.post("/data-sources/add")
async def data_sources_add(
    request: Request,
    source_name: Annotated[str, Form()],
    mount_path: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    read_only: Annotated[str, Form()] = "on",
    user=Depends(get_current_user),
):
    """Register a new browsable data source (mount point)."""
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)
    import os

    mount_path = mount_path.strip()
    source_name = source_name.strip()

    # Validate mount path exists and is a directory
    if not os.path.isdir(mount_path):
        return RedirectResponse(
            f"/tenant-admin/data-sources?error=Path+not+found:+{mount_path[:60]}",
            status_code=303,
        )

    is_read_only = read_only == "on"

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO connector_sources
                    (tenant_id, connector_type, source_name, mount_path,
                     read_only, description, status, source_config)
                VALUES (:tid, 'file_source', :name, :path,
                        :ro, :desc, 'active', '{}'::jsonb)
                ON CONFLICT (tenant_id, connector_type, source_name) DO UPDATE
                SET mount_path = EXCLUDED.mount_path,
                    read_only = EXCLUDED.read_only,
                    description = EXCLUDED.description,
                    updated_at = now()
            """),
            {
                "tid": tid, "name": source_name, "path": mount_path,
                "ro": is_read_only, "desc": description.strip(),
            },
        )
        await session.commit()

    return RedirectResponse("/tenant-admin/data-sources?saved=1", status_code=303)


@router.post("/data-sources/{source_id}/deactivate")
async def data_sources_deactivate(
    request: Request, source_id: str, user=Depends(get_current_user)
):
    """Deactivate a data source (does not delete data)."""
    if not _require_admin(user, request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Unauthorized"}, status_code=403)
    tid = _tenant_id(request)
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE connector_sources SET status = 'inactive', updated_at = now()
                WHERE id = CAST(:sid AS uuid) AND TRIM(tenant_id) = :tid
            """),
            {"sid": source_id, "tid": tid},
        )
        await session.commit()
    return RedirectResponse("/tenant-admin/data-sources?saved=1", status_code=303)


# ── Source-Agnostic Browse ────────────────────────────────────────────────────

@router.get("/data-sources/browse")
async def data_sources_browse(
    request: Request,
    source_id: str = "",
    prefix: str = "",
    user=Depends(get_current_user),
):
    """
    Browse any registered data source by source_id.
    Falls back to legacy share=clients|docsend for backward compatibility.
    """
    import os
    from fastapi.responses import JSONResponse

    tid = _tenant_id(request)

    # Legacy fallback: if share param is passed instead of source_id
    share = request.query_params.get("share", "")
    if share and not source_id:
        async with AsyncSessionLocal() as session:
            name_map = {"clients": "QNAP Clients", "docsend": "QNAP Docsend"}
            lookup_name = name_map.get(share)
            if lookup_name:
                row = (await session.execute(text("""
                    SELECT id::text FROM connector_sources
                    WHERE TRIM(tenant_id) = :tid AND source_name = :name
                """), {"tid": tid, "name": lookup_name})).first()
                if row:
                    source_id = row[0]

    if not source_id:
        return JSONResponse({"error": "source_id required"}, status_code=400)

    # Look up mount path from connector_sources
    async with AsyncSessionLocal() as session:
        source = (
            await session.execute(
                text("""
                    SELECT id::text, source_name, mount_path, read_only, status
                    FROM connector_sources
                    WHERE id = CAST(:sid AS uuid) AND TRIM(tenant_id) = :tid
                """),
                {"sid": source_id, "tid": tid},
            )
        ).mappings().first()

    if not source:
        return JSONResponse({"error": "Source not found"}, status_code=404)
    if source["status"] != "active":
        return JSONResponse({"error": "Source is inactive"}, status_code=400)

    base = source["mount_path"]
    if not base or not os.path.isdir(base):
        return JSONResponse({
            "error": f"Mount path not accessible: {base}",
            "source_name": source["source_name"],
        }, status_code=503)

    target = os.path.join(base, prefix) if prefix else base
    target = os.path.realpath(target)

    # Security: must stay under the mount root
    if not target.startswith(os.path.realpath(base)):
        return JSONResponse({"error": "Access denied"}, status_code=403)

    if not os.path.isdir(target):
        return JSONResponse({
            "folders": [], "files": 0, "path": prefix,
            "source_id": source_id, "source_name": source["source_name"],
        })

    folders = []
    file_count = 0
    total_size = 0
    try:
        for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
            if entry.name.startswith('.') or entry.name.startswith('@'):
                continue
            if entry.is_dir(follow_symlinks=False):
                try:
                    fc = sum(1 for f in os.scandir(entry.path)
                             if f.is_file() and not f.name.startswith('.'))
                    sc = sum(1 for f in os.scandir(entry.path)
                             if f.is_dir() and not f.name.startswith('.'))
                except (PermissionError, OSError):
                    fc = 0
                    sc = 0
                folders.append({
                    "name": entry.name,
                    "path": os.path.join(prefix, entry.name) if prefix else entry.name,
                    "file_count": fc,
                    "subfolder_count": sc,
                })
            elif entry.is_file():
                file_count += 1
                try:
                    total_size += entry.stat(follow_symlinks=False).st_size
                except (OSError, PermissionError):
                    pass
    except (PermissionError, OSError) as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({
        "folders": folders,
        "files": file_count,
        "total_size": total_size,
        "path": prefix,
        "source_id": source_id,
        "source_name": source["source_name"],
        "read_only": source["read_only"],
    })


@router.get("/data-sources/list-json")
async def data_sources_list_json(request: Request, user=Depends(get_current_user)):
    """Return active data sources as JSON (for source picker dropdowns)."""
    tid = _tenant_id(request)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT id::text, source_name, mount_path, read_only, description
                    FROM connector_sources
                    WHERE TRIM(tenant_id) = :tid
                      AND connector_type = 'file_source'
                      AND status = 'active'
                    ORDER BY source_name
                """),
                {"tid": tid},
            )
        ).mappings().all()
    from fastapi.responses import JSONResponse
    return JSONResponse([dict(r) for r in rows])

# ── Create Matter (inline from Data Sources) ──────────────────────────────

@router.post("/data-sources/create-matter")
async def data_sources_create_matter(request: Request, user=Depends(get_current_user)):
    """Create a new client+matter inline from the data sources assignment UI."""
    if not _require_admin(user, request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Unauthorized"}, status_code=403)

    tid = _tenant_id(request)
    body = await request.json()
    client_name = (body.get("client_name") or "").strip()
    matter_name = (body.get("matter_name") or "").strip()
    matter_number = (body.get("matter_number") or "").strip()
    matter_type = body.get("matter_type", "litigation")

    if not client_name or not matter_name:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Client name and matter name are required"}, status_code=400)

    if matter_type not in ("litigation", "transactional"):
        matter_type = "litigation"

    from fastapi.responses import JSONResponse

    async with AsyncSessionLocal() as session:
        # Find or create client
        existing_client = (
            await session.execute(
                text("""
                    SELECT id::text, client_name FROM clients
                    WHERE TRIM(tenant_id) = :tid AND LOWER(client_name) = LOWER(:name)
                    LIMIT 1
                """),
                {"tid": tid, "name": client_name},
            )
        ).first()

        if existing_client:
            client_id = existing_client[0]
        else:
            result = (
                await session.execute(
                    text("""
                        INSERT INTO clients (tenant_id, client_name)
                        VALUES (:tid, :name)
                        RETURNING id::text
                    """),
                    {"tid": tid, "name": client_name},
                )
            ).first()
            client_id = result[0]

        # Create matter
        result = (
            await session.execute(
                text("""
                    INSERT INTO matters (tenant_id, client_id, matter_name, matter_number, matter_type, status)
                    VALUES (:tid, CAST(:cid AS uuid), :mname, :mnum, :mtype, 'active')
                    RETURNING id::text
                """),
                {
                    "tid": tid, "cid": client_id,
                    "mname": matter_name, "mnum": matter_number or None,
                    "mtype": matter_type,
                },
            )
        ).first()
        matter_id = result[0]
        await session.commit()

    return JSONResponse({
        "matter_id": matter_id,
        "matter_name": matter_name,
        "client_id": client_id,
        "client_name": client_name,
        "matter_number": matter_number,
        "matter_type": matter_type,
    })

# ── Onboarding Landing ────────────────────────────────────────────────────

@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_landing(request: Request, user=Depends(get_current_user)):
    """Onboarding tab landing — redirects to Client & Matter QC."""
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    return RedirectResponse("/tenant-admin/onboarding/billing", status_code=303)


# ── Onboarding: Time & Billing ────────────────────────────────────────────

@router.get("/onboarding/billing", response_class=HTMLResponse)
async def onboarding_billing(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    t = _templates(request)
    return t.TemplateResponse(request, "tenant_admin/onboarding_billing.html", {"user": user})


@router.get("/onboarding/billing/stats", response_class=HTMLResponse)
async def onboarding_billing_stats(request: Request, stat: str = "", user=Depends(get_current_user)):
    """HTMX fragment — return a single stat count."""
    if not _require_admin(user, request):
        return HTMLResponse("—")
    tid = _tenant_id(request)

    queries = {
        "ts_clients": "SELECT COUNT(*) FROM ts_clients WHERE TRIM(tenant_id) = :tid",
        "ts_slips": "SELECT COUNT(*) FROM ts_slips WHERE TRIM(tenant_id) = :tid",
        "billing_chunks": "SELECT COUNT(*) FROM billing_chunks WHERE TRIM(tenant_id) = :tid",
    }
    q = queries.get(stat)
    if not q:
        return HTMLResponse("—")

    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(text(q), {"tid": tid})
            val = r.scalar() or 0
        return HTMLResponse(f"{val:,}")
    except Exception:
        return HTMLResponse("—")


# ── Permissions Stub ──────────────────────────────────────────────────────

@router.get("/permissions", response_class=HTMLResponse)
async def permissions_stub(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    t = _templates(request)
    return t.TemplateResponse(request, "tenant_admin/permissions.html", {"user": user})

# ── Source-Aware Inventory Scan ───────────────────────────────────────────

@router.post("/data-sources/{source_id}/scan")
async def data_sources_scan(request: Request, source_id: str, user=Depends(get_current_user)):
    """Walk a data source mount path recursively, catalog all files and folders."""
    if not _require_admin(user, request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Unauthorized"}, status_code=403)

    tid = _tenant_id(request)
    import os, uuid
    from datetime import datetime, timezone
    from fastapi.responses import JSONResponse

    async with AsyncSessionLocal() as session:
        source = (
            await session.execute(
                text("""
                    SELECT id::text, source_name, mount_path, status
                    FROM connector_sources
                    WHERE id = CAST(:sid AS uuid) AND TRIM(tenant_id) = :tid
                """),
                {"sid": source_id, "tid": tid},
            )
        ).mappings().first()

    if not source:
        return JSONResponse({"error": "Source not found"}, status_code=404)
    if source["status"] != "active":
        return JSONResponse({"error": "Source is inactive"}, status_code=400)

    mount = source["mount_path"]
    if not os.path.isdir(mount):
        return JSONResponse({"error": f"Mount not accessible: {mount}"}, status_code=503)

    source_uuid = source["id"]
    scan_run_id = str(uuid.uuid4())

    folder_count = 0
    file_count = 0
    total_bytes = 0
    errors = 0
    batch = []

    # Full recursive walk
    for dirpath, dirnames, filenames in os.walk(mount):
        # Skip hidden dirs
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and not d.startswith("@")]

        rel_path = os.path.relpath(dirpath, mount)
        if rel_path == ".": rel_path = ""
        depth = rel_path.count(os.sep) + 1 if rel_path else 0
        root_folder = rel_path.split(os.sep)[0] if rel_path else ""

        # Count files in this dir
        dir_files = 0
        dir_bytes = 0
        for fname in filenames:
            if fname.startswith("."):
                continue
            dir_files += 1
            try:
                fpath = os.path.join(dirpath, fname)
                dir_bytes += os.path.getsize(fpath)
            except OSError:
                pass

        file_count += dir_files
        total_bytes += dir_bytes

        if rel_path:  # Don't insert root itself
            folder_count += 1
            batch.append({
                "tid": tid, "scan_run_id": scan_run_id, "source_id": source_uuid,
                "entry_type": "folder", "full_path": dirpath,
                "parent_path": os.path.dirname(dirpath),
                "name": os.path.basename(dirpath),
                "extension": None, "size_bytes": dir_bytes,
                "modified_at": None, "depth": depth,
                "root_folder": root_folder,
                "child_file_count": dir_files,
                "child_folder_count": len([d for d in dirnames if not d.startswith(".")]),
                "total_size_bytes": dir_bytes,
            })

    # Bulk insert
    async with AsyncSessionLocal() as session:
        for i in range(0, len(batch), 500):
            chunk = batch[i:i+500]
            try:
                await session.execute(
                    text("""
                        INSERT INTO file_inventory
                            (tenant_id, scan_run_id, source_id, entry_type, full_path,
                             parent_path, name, extension, size_bytes, modified_at,
                             depth, root_folder, child_file_count, child_folder_count,
                             total_size_bytes)
                        VALUES
                            (:tid, CAST(:scan_run_id AS uuid), CAST(:source_id AS uuid),
                             :entry_type, :full_path, :parent_path, :name, :extension,
                             :size_bytes, :modified_at, :depth, :root_folder,
                             :child_file_count, :child_folder_count, :total_size_bytes)
                        ON CONFLICT DO NOTHING
                    """),
                    chunk,
                )
            except Exception as e:
                errors += 1
                log.error(f"Scan batch insert error: {e}")

        await session.execute(
            text("""
                UPDATE connector_sources
                SET last_sync_at = now(), last_sync_count = :count, updated_at = now()
                WHERE id = CAST(:sid AS uuid)
            """),
            {"sid": source_uuid, "count": folder_count},
        )
        await session.commit()

    return JSONResponse({
        "status": "ok",
        "source_id": source_uuid,
        "source_name": source["source_name"],
        "scan_run_id": scan_run_id,
        "folders": folder_count,
        "files": file_count,
        "total_bytes": total_bytes,
        "errors": errors,
    })



# -- Folder Migration Report --------------------------------------------------
from modules.tenant_admin.folder_migration_routes import register_folder_migration_routes
from core.db.base import AsyncSessionLocal as _fm_ASL
register_folder_migration_routes(router, _fm_ASL)


# -- Integrity Report --
from modules.tenant_admin.integrity_routes import register_integrity_routes
from core.db.base import AsyncSessionLocal as _int_ASL
register_integrity_routes(router, _int_ASL)


# ── File Import (React) ──────────────────────────────────────────────────────

@router.get("/file-import", response_class=HTMLResponse)
async def tenant_file_import(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    from core.services.nav_context import get_nav_context
    nav_ctx = await get_nav_context(request)
    brand = getattr(request.state, 'branding', None)
    t = _templates(request)
    return t.TemplateResponse(
        request,
        "file_import_react.html",
        {"user": user, "brand": brand, "page": "file-import",
         "current_user": getattr(request.state, 'current_user', None),
         **nav_ctx},
    )


# -- Connectors (React) --
@router.get("/connectors-react", response_class=HTMLResponse)
async def tenant_connectors_react(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    from core.services.nav_context import get_nav_context
    nav_ctx = await get_nav_context(request)
    brand = getattr(request.state, 'branding', None)
    t = _templates(request)
    return t.TemplateResponse(
        request,
        "connectors_react.html",
        {"user": user, "brand": brand, "page": "connectors",
         "current_user": getattr(request.state, 'current_user', None),
         **nav_ctx},
    )


# -- Onboarding (React) --
@router.get("/onboarding", response_class=HTMLResponse)
async def tenant_onboarding(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    from core.services.nav_context import get_nav_context
    nav_ctx = await get_nav_context(request)
    brand = getattr(request.state, 'branding', None)
    t = _templates(request)
    return t.TemplateResponse(
        request,
        "onboarding_react.html",
        {"user": user, "brand": brand, "page": "onboarding",
         "current_user": getattr(request.state, 'current_user', None),
         **nav_ctx},
    )


# -- Old HTMX redirects to React pages --
@router.get("/integrity", response_class=HTMLResponse)
async def redirect_integrity(request: Request):
    return RedirectResponse("/tenant-admin/file-import#integrity", status_code=302)

@router.get("/data-sources", response_class=HTMLResponse)
async def redirect_data_sources(request: Request):
    return RedirectResponse("/tenant-admin/file-import#sources", status_code=302)

@router.get("/folder-migration", response_class=HTMLResponse)
async def redirect_folder_migration(request: Request):
    return RedirectResponse("/tenant-admin/file-import#sync", status_code=302)


# ── AI Chat Panel Partial (shell right-panel) ────────────────────────────────
# Loaded via htmx by shell.html togglePanel('ai').
# Calls POST /api/ai-chat which handles MCP-User + MCP-Admin server selection.

@router.get("/ai-chat-partial", response_class=HTMLResponse)
async def ai_chat_partial(request: Request, user=Depends(get_current_user)):
    """HTML fragment for the shell right-panel AI chat widget."""
    return HTMLResponse("""
<style>
  #ai-p-chat{display:flex;flex-direction:column;height:calc(100vh - 140px);font-size:13px}
  #ai-p-msgs{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:10px;padding:4px 0}
  .apm{display:flex;gap:6px}
  .apav{width:22px;height:22px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:8px;font-weight:600;color:#fff;margin-top:2px}
  .apav-u{background:var(--primary)}.apav-b{background:var(--accent)}
  .apub{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:6px 9px;font-size:12px}
  .apbt{font-size:12px;line-height:1.55;padding-top:2px}
  .apbt pre{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 10px;font-size:11px;font-family:monospace;overflow-x:auto;margin:6px 0;white-space:pre-wrap}
  .apbt code{background:var(--bg);padding:1px 4px;border-radius:3px;font-size:11px;font-family:monospace}
  .aptool{font-size:10px;color:var(--accent);padding:1px 0 3px;display:flex;align-items:center;gap:3px}
  .aptdot{width:7px;height:7px;border-radius:50%;background:var(--accent);display:inline-block}
  .aptspin{width:9px;height:9px;border:1.5px solid var(--border);border-top-color:var(--accent);border-radius:50%;display:inline-block;animation:apsp .7s linear infinite}
  @keyframes apsp{to{transform:rotate(360deg)}}
  #ai-p-irow{display:flex;gap:6px;padding:10px 0 0;border-top:1px solid var(--border);flex-shrink:0}
  #ai-p-in{flex:1;padding:7px 10px;font-size:12px;border:1px solid var(--border);border-radius:6px;font-family:inherit;color:var(--text);background:var(--bg);outline:none;box-sizing:border-box}
  #ai-p-btn{padding:7px 14px;background:var(--primary);color:#fff;border:none;border-radius:6px;font-size:12px;font-weight:500;cursor:pointer;font-family:inherit}
  #ai-p-btn:disabled{opacity:.4}
  .apempty{flex:1;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--muted);padding:20px}
</style>
<div id="ai-p-chat">
  <div id="ai-p-msgs">
    <div class="apempty"><div>
      <div style="font-size:22px;margin-bottom:6px;opacity:.3">&#x1F4AC;</div>
      <div style="font-size:12px;font-weight:500;margin-bottom:3px">AI Assistant</div>
      <div style="font-size:11px">Ask about matters, documents, billing, or anything in the platform. Uses MCP tools for live data.</div>
    </div></div>
  </div>
  <div id="ai-p-irow">
    <input id="ai-p-in" type="text" placeholder="Ask anything..." autocomplete="off"/>
    <button id="ai-p-btn">Send</button>
  </div>
</div>
<script>
(function(){
  var msgs=document.getElementById('ai-p-msgs');
  var inp=document.getElementById('ai-p-in');
  var btn=document.getElementById('ai-p-btn');
  var history=[];
  var streaming=false;
  var sessionId='';

  function esc(t){return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
  function md(t){
    var h=esc(t);
    h=h.replace(/```(\w*)\n([\s\S]*?)```/g,function(_,l,c){return '<pre>'+c.trim()+'</pre>';});
    h=h.replace(/`([^`]+)`/g,'<code>$1</code>');
    h=h.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
    h=h.replace(/\*(.+?)\*/g,'<em>$1</em>');
    h=h.replace(/^### (.+)$/gm,'<div style="font-weight:600;font-size:12px;margin:8px 0 3px">$1</div>');
    h=h.replace(/^## (.+)$/gm,'<div style="font-weight:600;font-size:13px;margin:10px 0 4px">$1</div>');
    h=h.replace(/^- (.+)$/gm,'<div style="padding-left:14px;margin:1px 0">&bull; $1</div>');
    h=h.replace(/\n\n/g,'<div style="margin-top:6px"></div>');
    h=h.replace(/\n/g,'<br>');
    return h;
  }
  function scroll(){msgs.scrollTop=msgs.scrollHeight;}

  function addUser(text){
    var em=msgs.querySelector('.apempty');if(em)em.remove();
    var d=document.createElement('div');d.className='apm';
    d.innerHTML='<div class="apav apav-u">You</div><div style="flex:1;min-width:0"><div class="apub">'+esc(text)+'</div></div>';
    msgs.appendChild(d);scroll();
  }

  function mkBot(){
    var d=document.createElement('div');d.className='apm';
    d.innerHTML='<div class="apav apav-b">AI</div><div class="apbt" style="flex:1;min-width:0"></div>';
    msgs.appendChild(d);
    return d.querySelector('.apbt');
  }

  function addTool(el,name,done){
    var pill=document.createElement('div');pill.className='aptool';
    pill.innerHTML=(done?'<span class="aptdot"></span>':'<span class="aptspin"></span>')+' '+esc(name);
    pill.dataset.tool=name;
    el.appendChild(pill);scroll();
  }

  function finishTools(el){
    el.querySelectorAll('.aptspin').forEach(function(s){s.className='aptdot';});
  }

  async function send(){
    if(streaming)return;
    var text=inp.value.trim();if(!text)return;
    inp.value='';
    addUser(text);
    history.push({role:'user',content:text});
    streaming=true;btn.disabled=true;

    var botEl=mkBot();
    var fullText='';

    try{
      var body={messages:history,context:{page:window._praesidiumPage||''}};
      if(sessionId)body.session_id=sessionId;
      var resp=await fetch('/api/ai-chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      var reader=resp.body.getReader();
      var decoder=new TextDecoder();
      var buffer='';
      while(true){
        var chunk=await reader.read();
        if(chunk.done)break;
        buffer+=decoder.decode(chunk.value,{stream:true});
        var lines=buffer.split('\n');buffer=lines.pop();
        for(var i=0;i<lines.length;i++){
          var line=lines[i];if(!line.startsWith('data: '))continue;
          try{
            var evt=JSON.parse(line.slice(6));
            if(evt.type==='session'){sessionId=evt.session_id||sessionId;}
            else if(evt.type==='text'){fullText+=evt.text;botEl.innerHTML=md(fullText);scroll();}
            else if(evt.type==='tool_start'){addTool(botEl,evt.tool,false);}
            else if(evt.type==='block_stop'){finishTools(botEl);}
            else if(evt.type==='tool_result'){/* tool results handled by text output */}
            else if(evt.type==='error'){fullText+='\n\n**Error:** '+evt.text;botEl.innerHTML=md(fullText);scroll();}
            else if(evt.type==='done'){break;}
          }catch(e){}
        }
      }
    }catch(err){
      fullText+='\n\n**Connection error:** '+err.message;
      botEl.innerHTML=md(fullText);
    }

    history.push({role:'assistant',content:fullText});
    streaming=false;btn.disabled=false;
    inp.focus();scroll();
  }

  btn.addEventListener('click',send);
  inp.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
  setTimeout(function(){inp.focus();},200);
})();
</script>
""")
