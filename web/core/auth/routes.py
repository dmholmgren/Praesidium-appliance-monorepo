"""
Auth Routes — Login/Logout endpoints.
POST /auth/login — authenticates via the tenant's configured auth adapter.
GET /auth/logout — clears session cookie and redirects to login.

For HJMM: authenticates against Active Directory via LDAPS (COMP 16).
Auth adapter selected from tenant config (AUTH_ADAPTER env var).
"""

import os
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from core.db.base import get_session_factory, TenantSession
from core.models.user import User
from core.audit import write_audit
from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "praesidium_session")


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """
    Handle login form submission.
    1. Resolve tenant from request
    2. Authenticate via configured adapter (LDAPS, Azure AD, etc.)
    3. Find or create user in local DB
    4. Set session cookie
    5. Redirect to DMS home
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        return _login_error(request, "Unable to resolve tenant. Check your domain.")

    # Get the auth adapter from tenant DB record (not env var)
    auth_adapter = await _get_tenant_auth_adapter(tenant_id)

    if auth_adapter in ("ldaps", "ldap"):
        result = await _authenticate_ldaps(username, password, tenant_id)
    elif auth_adapter in ("azure", "azure_ad"):
        result = await _authenticate_azure(username, password, tenant_id)
    else:
        # Default: local auth (covers 'local' and any unknown adapter)
        result = await _authenticate_local(username, password, tenant_id)

    if not result["success"]:
        logger.warning(f"Login failed for {username}@{tenant_id}: {result.get('error', 'unknown')}")
        return _login_error(request, result.get("error", "Invalid credentials"))
    # Find or create user in local DB
    try:
        from core.db.base import AsyncSessionLocal
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            stmt = select(User).where(
                User.username == username,
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
                    auth_provider=auth_adapter,
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
    except Exception as e:
        logger.error(f"Database error during login for {username}: {e}")
        return _login_error(request, "Authentication succeeded but session creation failed.")

    # Set session cookie and redirect
    # The middleware reads this cookie and loads the user by ID
    response = RedirectResponse(url="/dms/", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=user_id,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=86400,  # 24 hours
    )

    logger.info(f"Login successful: {username} (user_id={user_id}) tenant={tenant_id}")
    return response


@router.get("/logout")
async def logout(request: Request):
    """Clear session and redirect to login."""
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    return response


# ── Auth Adapter Calls ─────────────────────────────────────

async def _authenticate_ldaps(username: str, password: str, tenant_id: str) -> dict:
    """Authenticate against Active Directory via LDAPS."""
    try:
        from modules.auth.adapters.auth_adapters import LDAPSAuthAdapter
        adapter = LDAPSAuthAdapter()
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


async def _authenticate_azure(username: str, password: str, tenant_id: str) -> dict:
    """Authenticate via Azure AD / Entra ID."""
    try:
        from modules.auth.adapters.auth_adapters import AzureADAuthAdapter
        adapter = AzureADAuthAdapter()
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
        from core.db.base import AsyncSessionLocal
        from sqlalchemy import text

        async with AsyncSessionLocal() as session:
            # Match by username OR email, scoped to tenant
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


async def _get_tenant_auth_adapter(tenant_id: str) -> str:
    """Read auth_adapter from tenants table. Falls back to 'local'."""
    try:
        from core.db.base import AsyncSessionLocal
        from sqlalchemy import text
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT auth_adapter FROM tenants WHERE TRIM(id) = :tid LIMIT 1"),
                {"tid": tenant_id.strip()}
            )
            row = result.first()
            return (row[0] or "local").strip() if row else "local"
    except Exception as e:
        logger.warning(f"Could not read tenant auth_adapter: {e}")
        return "local"

def _login_error(request: Request, message: str):
    """Return to login page with error message."""
    # Redirect back to login with error in query param
    from urllib.parse import quote
    return RedirectResponse(
        url=f"/login?error={quote(message)}",
        status_code=302,
    )
