"""
Authentication middleware.

Injects current_user into request.state for every request.
Supports session-based auth with pluggable auth providers (LDAPS, Azure AD, etc.).

PATCHED by Chat 1: _load_user_from_session now filters by user_id (session_token)
instead of returning the first active user.

PATCHED by Chat 2: _load_user_from_session now resolves tenant_id from the sessions
table when TenantResolverMiddleware has not set it (e.g. TestClient, API clients
without a subdomain). The sessions table is the authoritative source for both
user_id and tenant_id when a valid token is present.
"""

import os
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from core.db.base import get_session_factory, TenantSession
from core.models.user import User
from sqlalchemy import text as sa_text

# Paths that don't require authentication
PUBLIC_PATHS = {
    "/health",
    "/login",
    "/auth/login",
    "/auth/logout",
    "/auth/callback",
    "/static",
    "/admin",
    "/favicon.ico",
    "/manifest.json",
    "/api/v1/addin/manifest.xml",
}

SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "praesidium_session")


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Authentication middleware.

    Checks session cookie, loads user from DB, attaches to request.state.
    Redirects to login page if not authenticated (except for public paths).

    Tenant resolution priority:
      1. TenantResolverMiddleware (subdomain-based) — production path
      2. Sessions table lookup via cookie token — TestClient / API client path
    """

    async def dispatch(self, request: Request, call_next):
        # Ensure state attrs always exist
        if not hasattr(request.state, "tenant_id"):
            request.state.tenant_id = None
        if not hasattr(request.state, "subdomain"):
            request.state.subdomain = None
        request.state.current_user = None

        # Skip auth for public paths
        path = request.url.path
        if any(path.startswith(p) for p in PUBLIC_PATHS):
            return await call_next(request)

        # Check session cookie
        session_token = request.cookies.get(SESSION_COOKIE_NAME)
        if not session_token:
            # No cookie — only redirect if tenant is resolved (browser flow)
            # API/test clients without a cookie just pass through
            tenant_id = request.state.tenant_id
            if tenant_id:
                return self._redirect_to_login(request)
            return await call_next(request)

        # Load user — resolves tenant_id from sessions table if not already set
        try:
            user, resolved_tenant_id = await self._load_user_from_session(
                request.state.tenant_id, session_token
            )
        except Exception:
            user = None
            resolved_tenant_id = None

        if not user:
            tenant_id = request.state.tenant_id
            if tenant_id:
                return self._redirect_to_login(request)
            return await call_next(request)

        # Inject resolved values into request state
        request.state.current_user = user
        if resolved_tenant_id and not request.state.tenant_id:
            request.state.tenant_id = resolved_tenant_id

        return await call_next(request)

    async def _load_user_from_session(
        self, tenant_id: Optional[str], session_token: str
    ) -> tuple[Optional[User], Optional[str]]:
        """
        Load user from session token. Returns (user, tenant_id).

        Two resolution paths:
          A) tenant_id already known (subdomain resolved): query users directly
             using int(session_token) as user_id — legacy path, fast.
          B) tenant_id not known: look up sessions table by token string,
             get both user_id and tenant_id, then load the User record.
        """
        from core.db.base import AsyncSessionLocal
        from sqlalchemy import select

        # Path A — tenant already resolved by TenantResolverMiddleware
        if tenant_id:
            try:
                user_id = int(session_token)
                async with AsyncSessionLocal() as session:
                    stmt = select(User).where(
                        User.id == user_id,
                        User.is_active == True,
                        User.tenant_id == tenant_id.strip(),
                    )
                    result = await session.execute(stmt)
                    user = result.scalar_one_or_none()
                    return user, tenant_id if user else None
            except (ValueError, TypeError):
                return None, None
            except Exception:
                return None, None

        # Path B — no tenant from subdomain; resolve via sessions table
        try:
            async with AsyncSessionLocal() as session:
                row = await session.execute(
                    sa_text("""
                        SELECT s.user_id, s.tenant_id
                        FROM sessions s
                        WHERE s.token = :token
                          AND s.expires_at > NOW()
                        LIMIT 1
                    """),
                    {"token": session_token},
                )
                sess_row = row.fetchone()

            if not sess_row:
                return None, None

            resolved_user_id = sess_row.user_id
            resolved_tenant = (sess_row.tenant_id or "").strip()

            async with AsyncSessionLocal() as session:
                stmt = select(User).where(
                    User.id == resolved_user_id,
                    User.is_active == True,
                    User.tenant_id == resolved_tenant,
                )
                result = await session.execute(stmt)
                user = result.scalar_one_or_none()
                return user, resolved_tenant if user else None

        except Exception:
            return None, None

    def _redirect_to_login(self, request: Request) -> RedirectResponse:
        """Redirect to the login page."""
        return RedirectResponse(url="/login", status_code=302)
