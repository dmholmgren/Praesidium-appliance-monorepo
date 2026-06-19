"""
Auth Routes — Login/Logout endpoints.
POST /auth/login — authenticates via the tenant's configured auth adapter.
GET /auth/logout — clears session cookie and redirects to login.

REFACTORED: Auth adapter resolution now reads tenant_connectors for active
auth_* connector, then resolves config from tenant_connectors.config +
credentials_vault. Falls back to tenants.auth_adapter → env vars for
backward compatibility with production systems not yet migrated.

Multi-tenant: each tenant can have a different auth connector (LDAP, Azure,
Okta, local) configured entirely in the DB. No .env changes required.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from core.db.base import AsyncSessionLocal
from core.models.user import User
from core.audit import write_audit
from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "praesidium_session")


# ═══════════════════════════════════════════════════════════════════════════
# Stalwart single-password mail sync — push the just-validated login password
# to the user's mailbox so mail auth == app login. Best-effort; never blocks.
# ═══════════════════════════════════════════════════════════════════════════

STALWART_MAIL_DOMAIN = os.environ.get("STALWART_MAIL_DOMAIN", "hjmmlegal.com")
_mail_sync_tasks: set = set()


async def _sync_stalwart_mail_password(email: str, password: str):
    try:
        from core.services.stalwart_service import (
            update_mail_password, provision_mail_account,
        )
        res = await update_mail_password(email, password)
        if not res.get("success") and "No Stalwart account" in str(res.get("error", "")):
            await provision_mail_account(email=email, password=password)
    except Exception as e:
        logger.warning(f"[auth] stalwart mail sync failed for {email}: {e}")


def _kick_mail_sync(email: str, password: str):
    """Fire-and-forget mail-password sync for firm-domain users."""
    if not email or not email.lower().endswith("@" + STALWART_MAIL_DOMAIN):
        return
    try:
        import asyncio
        t = asyncio.create_task(_sync_stalwart_mail_password(email, password))
        _mail_sync_tasks.add(t)
        t.add_done_callback(_mail_sync_tasks.discard)
    except Exception as e:
        logger.warning(f"[auth] could not schedule mail sync: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Tenant auth config resolver — the multi-tenant refactor core
# ═══════════════════════════════════════════════════════════════════════════

async def resolve_tenant_auth(tenant_id: str) -> dict:
    """
    Resolve auth adapter type and full config for a tenant.

    Resolution order:
      1. tenant_connectors — look for an active auth_* connector row
      2. tenants.auth_adapter column — legacy single-tenant path
      3. AUTH_ADAPTER env var — backward compat fallback

    Returns:
      {
          "adapter": "ldaps" | "azure" | "local" | ...,
          "config": { ... merged config from tenant_connectors.config },
          "credentials": { key_type: value, ... from credentials_vault },
          "source": "tenant_connectors" | "tenants_table" | "env"
      }
    """
    tid = (tenant_id or "").strip()
    if not tid:
        return {"adapter": "local", "config": {}, "credentials": {}, "source": "default"}

    # ── 1. Check tenant_connectors for an active auth_* connector ──────────
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                sa_text("""
                    SELECT connector, connector_type, config, status
                    FROM tenant_connectors
                    WHERE TRIM(tenant_id) = :tid
                      AND connector LIKE 'auth_%'
                      AND is_active = true
                    ORDER BY created_at DESC
                    LIMIT 1
                """),
                {"tid": tid}
            )
            row = result.mappings().first()

            if row:
                connector_type = row["connector"]  # e.g. 'auth_ldap'
                config = row["config"] if isinstance(row["config"], dict) else {}

                # Map connector type → adapter name
                adapter_map = {
                    "auth_ldap":  "ldaps",
                    "auth_azure": "azure",
                    "auth_okta":  "okta",
                    "auth_local": "local",
                }
                adapter = adapter_map.get(connector_type, "local")

                # Resolve credentials from vault
                credentials = await _resolve_credentials(tid, connector_type)

                logger.info(f"[auth] Tenant {tid} → {adapter} via tenant_connectors ({connector_type})")
                return {
                    "adapter":     adapter,
                    "config":      config,
                    "credentials": credentials,
                    "source":      "tenant_connectors",
                }
    except Exception as e:
        logger.warning(f"[auth] tenant_connectors lookup failed for {tid}: {e}")

    # ── 2. Fall back to tenants.auth_adapter column ────────────────────────
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                sa_text("SELECT auth_adapter FROM tenants WHERE TRIM(id) = :tid LIMIT 1"),
                {"tid": tid}
            )
            row = result.first()
            if row and row[0] and row[0].strip() not in ("", "local"):
                adapter = row[0].strip()
                logger.info(f"[auth] Tenant {tid} → {adapter} via tenants.auth_adapter")
                return {
                    "adapter":     adapter,
                    "config":      {},
                    "credentials": {},
                    "source":      "tenants_table",
                }
    except Exception as e:
        logger.warning(f"[auth] tenants table lookup failed: {e}")

    # ── 3. Fall back to AUTH_ADAPTER env var ────────────────────────────────
    env_adapter = os.environ.get("AUTH_ADAPTER", "local").strip()
    if env_adapter and env_adapter != "local":
        logger.info(f"[auth] Tenant {tid} → {env_adapter} via AUTH_ADAPTER env")
        return {"adapter": env_adapter, "config": {}, "credentials": {}, "source": "env"}

    return {"adapter": "local", "config": {}, "credentials": {}, "source": "default"}


async def _resolve_credentials(tenant_id: str, connector_type: str) -> dict:
    """
    Load credentials from credentials_vault for a connector.

    The registry_router stores credentials with:
      provider = connector_type (e.g. 'auth_ldap')
      key_type = field_name (e.g. 'bind_password')

    We also check the legacy provider names (e.g. 'ldap') for backward
    compat with manually-seeded credentials.

    Returns dict of { key_type: encrypted_key } pairs.
    """
    # Check both the connector_type name and the short legacy name
    legacy_map = {
        "auth_ldap":  "ldap",
        "auth_azure": "azure_ad",
        "auth_okta":  "okta",
    }
    providers_to_check = [connector_type]
    if connector_type in legacy_map:
        providers_to_check.append(legacy_map[connector_type])

    try:
        async with AsyncSessionLocal() as session:
            placeholders = " OR ".join([f"provider = :p{i}" for i in range(len(providers_to_check))])
            params = {"tid": tenant_id.strip()}
            for i, p in enumerate(providers_to_check):
                params[f"p{i}"] = p

            result = await session.execute(
                sa_text(f"""
                    SELECT key_type, encrypted_key, provider
                    FROM credentials_vault
                    WHERE TRIM(tenant_id) = :tid
                      AND ({placeholders})
                """),
                params
            )
            rows = result.mappings().all()
            # Decrypt Fernet-encrypted values before returning
            decrypted = {}
            for row in rows:
                val = row["encrypted_key"]
                if val and val.startswith("gAAAAA"):
                    try:
                        from cryptography.fernet import Fernet
                        import base64
                        secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
                        key_bytes = (secret[:32]).encode().ljust(32, b"0")
                        fernet_key = base64.urlsafe_b64encode(key_bytes)
                        f = Fernet(fernet_key)
                        val = f.decrypt(val.encode()).decode()
                    except Exception as e:
                        logger.warning(f"[auth] Failed to decrypt {row['key_type']}: {e}")
                decrypted[row["key_type"]] = val
            return decrypted
    except Exception as e:
        logger.warning(f"[auth] credentials_vault lookup failed ({connector_type}): {e}")
        return {}


# ═══════════════════════════════════════════════════════════════════════════
# Login / Logout routes
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """
    Handle login form submission.
    1. Resolve tenant from request
    2. Resolve auth adapter + config from DB (multi-tenant aware)
    3. Authenticate via resolved adapter
    4. Find or create user in local DB
    5. Set session cookie
    6. Redirect to DMS home
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        return _login_error(request, "Unable to resolve tenant. Check your domain.")

    # ── Per-user adapter override (BREAK-GLASS) ──────────────────────────
    # Check the user record FIRST. If their auth_provider='local', honor
    # that regardless of the tenant-level adapter. This keeps local
    # break-glass accounts (e.g. praesidium_admin) authenticatable even
    # when the tenant has LDAP/Azure active. Match by username OR email.
    user_adapter_override = None
    try:
        async with AsyncSessionLocal() as session:
            ur = await session.execute(
                sa_text("""
                    SELECT auth_provider FROM users
                    WHERE TRIM(tenant_id) = :tid
                      AND (username = :u OR email = :u)
                      AND is_active = TRUE
                    LIMIT 1
                """),
                {"tid": tenant_id.strip(), "u": username}
            )
            row = ur.first()
            if row and row[0] == "local":
                user_adapter_override = "local"
                logger.info(
                    f"[auth] Per-user override: {username}@{tenant_id} "
                    f"-> local (auth_provider='local' on user record)"
                )
    except Exception as e:
        logger.warning(f"[auth] per-user adapter check failed: {e}")
        # Fall through to tenant-level adapter

    if user_adapter_override == "local":
        auth_info = {"adapter": "local", "config": {}, "credentials": {}, "source": "user_record"}
        adapter = "local"
    else:
        # Resolve auth config from DB (tenant-level)
        auth_info = await resolve_tenant_auth(tenant_id)
        adapter = auth_info["adapter"]

    if adapter in ("ldaps", "ldap"):
        result = await _authenticate_ldaps(
            username, password, tenant_id,
            config=auth_info["config"],
            credentials=auth_info["credentials"],
        )
    elif adapter in ("azure", "azure_ad"):
        result = await _authenticate_azure(
            username, password, tenant_id,
            config=auth_info["config"],
            credentials=auth_info["credentials"],
        )
    else:
        result = await _authenticate_local(username, password, tenant_id)

    if not result["success"]:
        logger.warning(f"Login failed for {username}@{tenant_id} [{adapter}]: {result.get('error', 'unknown')}")
        return _login_error(request, result.get("error", "Invalid credentials"))

    # Find or create user in local DB
    try:
        from sqlalchemy import select, or_
        async with AsyncSessionLocal() as session:
            # Match by username OR email — local auth accepts either, so the
            # find-or-create must too, or email logins mint duplicate users.
            stmt = select(User).where(
                or_(User.username == username, User.email == username),
                User.tenant_id == tenant_id.strip(),
            )
            result_db = await session.execute(stmt)
            user = result_db.scalar_one_or_none()
            if not user:
                user = User(
                    tenant_id=tenant_id.strip(),
                    username=username,
                    email=result.get("email", f"{username}@{os.environ.get('TENANT_DOMAIN', '')}"),
                    full_name=result.get("display_name", username),
                    role="staff",
                    is_active=True,
                    is_timekeeper=False,
                    auth_provider=adapter,
                    external_id=result.get("external_id", ""),
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
                session.add(user)
                await session.flush()
            user.updated_at = datetime.utcnow()
            await session.commit()
            await session.refresh(user)
            user_id = str(user.id)
            _kick_mail_sync(user.email, password)
    except Exception as e:
        logger.error(f"Database error during login for {username}: {e}")
        return _login_error(request, "Authentication succeeded but session creation failed.")

    # Portal users (provisioned via client portal, auth_provider='magic_link')
    # get opaque token sessions and land on the portal — never raw-uid cookies.
    if getattr(user, "auth_provider", "") == "magic_link":
        import secrets as _secrets
        from datetime import timedelta as _td
        _tok = _secrets.token_urlsafe(32)
        async with AsyncSessionLocal() as _s:
            await _s.execute(sa_text(
                "DELETE FROM sessions WHERE user_id = :uid AND expires_at < NOW()"),
                {"uid": user.id})
            await _s.execute(sa_text(
                "INSERT INTO sessions (token, user_id, tenant_id, expires_at) "
                "VALUES (:tok, :uid, :tid, NOW() + INTERVAL '7 days')"),
                {"tok": _tok, "uid": user.id, "tid": tenant_id.strip()})
            await _s.execute(sa_text(
                "INSERT INTO user_activity_log (tenant_id, user_id, action, ip_address) "
                "VALUES (:tid, :uid, 'portal_login_password', :ip)"),
                {"tid": tenant_id.strip(), "uid": user.id,
                 "ip": (request.client.host if request.client else "")[:50]})
            await _s.commit()
        resp = RedirectResponse(url="/portal/", status_code=302)
        resp.set_cookie(key=SESSION_COOKIE_NAME, value=_tok, httponly=True,
                        secure=request.url.scheme == "https", samesite="lax",
                        max_age=7 * 86400)
        logger.info(f"Portal password login: {username} (user_id={user_id}) tenant={tenant_id}")
        return resp

    # AUTHFIX: firm users now get an opaque token session (sessions table),
    # never a raw user-id cookie.
    import secrets as _secrets
    _tok = _secrets.token_urlsafe(32)
    async with AsyncSessionLocal() as _s:
        await _s.execute(sa_text(
            "DELETE FROM sessions WHERE user_id = :uid AND expires_at < NOW()"),
            {"uid": user.id})
        await _s.execute(sa_text(
            "INSERT INTO sessions (token, user_id, tenant_id, expires_at) "
            "VALUES (:tok, :uid, :tid, NOW() + INTERVAL '1 day')"),
            {"tok": _tok, "uid": user.id, "tid": tenant_id.strip()})
        await _s.execute(sa_text(
            "INSERT INTO user_activity_log (tenant_id, user_id, action, ip_address) "
            "VALUES (:tid, :uid, 'login_password', :ip)"),
            {"tid": tenant_id.strip(), "uid": user.id,
             "ip": (request.client.host if request.client else "")[:50]})
        await _s.commit()

    response = RedirectResponse(url="/dms/", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=_tok,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=86400,
    )

    logger.info(f"Login successful: {username} (user_id={user_id}) tenant={tenant_id} adapter={adapter}")
    return response


@router.get("/logout")
async def logout(request: Request):
    """Clear session and redirect to login. Token sessions are expired server-side."""
    tok = request.cookies.get(SESSION_COOKIE_NAME)
    if tok and not tok.isdigit():
        try:
            async with AsyncSessionLocal() as _s:
                await _s.execute(sa_text(
                    "UPDATE sessions SET expires_at = NOW() WHERE token = :tok"),
                    {"tok": tok})
                await _s.commit()
        except Exception as e:
            logger.warning(f"[auth] logout session expiry failed: {e}")
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    return response


# ═══════════════════════════════════════════════════════════════════════════
# Auth adapter calls — now accept resolved config + credentials
# ═══════════════════════════════════════════════════════════════════════════

async def _authenticate_ldaps(
    username: str, password: str, tenant_id: str,
    config: dict = None, credentials: dict = None,
) -> dict:
    """Authenticate against Active Directory via LDAPS."""
    try:
        from modules.auth.adapters.auth_adapters import LDAPSAuthAdapter
        adapter = LDAPSAuthAdapter(config=config, credentials=credentials)
        result = await adapter.authenticate(username, password, tenant_id)
        return {
            "success": result.success,
            "email": result.email or "",
            "display_name": result.display_name or username,
            "groups": result.groups,
            "error": result.error,
            "external_id": result.metadata.get("ad_guid", "") if result.metadata else "",
        }
    except Exception as e:
        logger.error(f"LDAPS authentication error: {e}")
        return {"success": False, "error": f"LDAP connection failed: {str(e)}"}


async def _authenticate_azure(
    username: str, password: str, tenant_id: str,
    config: dict = None, credentials: dict = None,
) -> dict:
    """Authenticate via Azure AD / Entra ID."""
    try:
        from modules.auth.adapters.auth_adapters import AzureADAuthAdapter
        adapter = AzureADAuthAdapter(config=config, credentials=credentials)
        result = await adapter.authenticate(username, password, tenant_id)
        return {
            "success": result.success,
            "email": result.email or "",
            "display_name": result.display_name or username,
            "groups": result.groups,
            "error": result.error,
            "external_id": result.metadata.get("azure_id", "") if result.metadata else "",
        }
    except Exception as e:
        logger.error(f"Azure AD authentication error: {e}")
        return {"success": False, "error": f"Azure AD connection failed: {str(e)}"}


async def _authenticate_local(username: str, password: str, tenant_id: str) -> dict:
    """
    Authenticate against local bcrypt password hash in the users table.
    Matches by username OR email — supports both login styles.
    Default adapter for demo tenants, new tenants, and break-glass access.
    """
    try:
        import bcrypt as _bcrypt
        from sqlalchemy import text

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT id, username, email, full_name, password_hash, role, is_active
                    FROM users
                    WHERE TRIM(tenant_id) = :tid
                      AND is_active = true
                      AND (username = :uname OR email = :uname)
                    LIMIT 1
                """),
                {"tid": tenant_id.strip(), "uname": username}
            )
            user = result.mappings().first()

            if not user:
                return {"success": False, "error": "Invalid credentials"}
            if not user["password_hash"]:
                return {"success": False, "error": "Account has no password set. Contact your administrator."}
            if not _bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
                return {"success": False, "error": "Invalid credentials"}

            return {
                "success": True,
                "email":        user["email"],
                "display_name": user["full_name"],
                "groups":       [],
                "user_id":      user["id"],
            }
    except Exception as e:
        logger.error(f"Local auth error: {e}")
        return {"success": False, "error": str(e)}


# ── Legacy compat (kept for any external callers) ────────────────────────

async def _get_tenant_auth_adapter(tenant_id: str) -> str:
    """DEPRECATED — use resolve_tenant_auth() instead. Kept for backward compat."""
    info = await resolve_tenant_auth(tenant_id)
    return info["adapter"]


def _login_error(request: Request, message: str):
    """Return to login page with error message."""
    from urllib.parse import quote
    return RedirectResponse(
        url=f"/login?error={quote(message)}",
        status_code=302,
    )
