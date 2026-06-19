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
    "/auth/magic",        # Portal magic-link redemption — token validated in handler
    "/auth/callback",
    "/static",
    "/admin",
    "/favicon.ico",
    "/manifest.json",
    "/api/v1/addin/manifest.xml",
    "/api/v1/desktop",
    "/api/connectors",
    "/docs",              # WebDAV — uses Basic auth, not session cookie
    "/mobile",            # M-MOBILE C1 — PWA shell (JWT auth inside app)
    "/sw.js",             # Service worker must be served from root
    "/present/page",      # Trial display — token-scoped page raster (unauthenticated, PR-3)
    "/present/trial",     # Trial Presentation — role-aware display client (token-scoped)
    "/trial-present/page",
    "/p/",                 # Trial Presentation tiny display URL # Trial Presentation — token-scoped page raster (unauthenticated)
    "/api/v1/mobile",     # Mobile API — JWT-auth, not session-cookie
}

SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "praesidium_session")

# ── External-role API module guard ──────────────────────────────────────────
# External roles (client / co_counsel) use the REAL multitenant app. Nav hides
# ungranted modules and DMS hides work-product folders, but the underlying APIs
# must ALSO refuse ungranted modules — otherwise a client could pull privileged
# e-discovery / trial / deposition work product on their own matter by guessing
# the URL. Enforcement is DB-driven: the SAME permission_matrix grants that the
# nav rail uses (see nav_service._granted_modules). A path that maps to a known
# module the role wasn't granted -> 404 (match cross-matter isolation, no leak).
# Unmapped paths (auth, portal, nav-tabs, permissions, matters/dms/calendar
# shells) pass through; the core three are granted to both external roles.
_EXTERNAL_PROJECTION_ROLES = frozenset({"client", "co_counsel"})

# Ordered most-specific-first; first matching prefix wins.
# FIXME (fix-later flag, see chatprompts v19.0): this path->module map is hardcoded
# and must be kept in lockstep with nav_service._NAV_MODULE + nav_tabs_api on every
# new module. Move both maps into a DB registry so a new module needs only DB rows.
_MODULE_PATH_PREFIXES = (
    ("/api/v1/ediscovery", "ediscovery"), ("/ediscovery", "ediscovery"),
    ("/api/v1/trial", "trial"), ("/trial", "trial"),
    ("/api/v1/depositions", "depositions"), ("/depositions", "depositions"),
    ("/api/v1/appellate", "appellate"), ("/appellate", "appellate"),
    ("/api/v1/deals", "deals"), ("/deals", "deals"),
    ("/api/v1/drafting", "drafting"), ("/drafting", "drafting"),
    ("/api/v1/projects", "projects"), ("/projects", "projects"),
    ("/api/v1/billing", "billing"), ("/billing", "billing"),
    ("/api/v1/court", "court"), ("/court", "court"),
)


def _path_module(path: str):
    for prefix, module in _MODULE_PATH_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return module
    return None


# tiny in-process TTL cache so the guard adds no per-request DB hit on the hot path
_GRANT_CACHE: dict = {}
_GRANT_TTL = 60.0


async def _granted_modules_cached(tenant_id: str, role: str) -> set:
    import time
    key = (tenant_id.strip(), role)
    hit = _GRANT_CACHE.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    from core.db.base import AsyncSessionLocal
    async with AsyncSessionLocal() as s:
        r = await s.execute(sa_text(
            "SELECT DISTINCT module FROM permission_matrix "
            "WHERE TRIM(tenant_id) = trim(:t) AND role = :r "
            "AND action = 'view' AND allowed = TRUE"), {"t": tenant_id, "r": role})
        granted = {row[0] for row in r.fetchall()}
    _GRANT_CACHE[key] = (now + _GRANT_TTL, granted)
    return granted


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

        # Portal confinement + co-counsel projection.
        if user is not None and getattr(user, "auth_provider", "") == "magic_link":
            role = (getattr(user, "role", "") or "").strip()
            # External roles that use the REAL multitenant app, scoped via a
            # per-user projection schema (SET LOCAL search_path, transaction-scoped):
            #   co_counsel -> cocounsel schema (full matter visibility)
            #   client     -> client_portal schema (client-scoped + folder-restricted)
            _PROJECTION_SCHEMA = {
                "co_counsel": "cocounsel, public",
                "client": "client_portal, public",
            }
            if role in _PROJECTION_SCHEMA:
                # API module guard: refuse ungranted modules at the API layer
                # (defense-in-depth behind nav + DMS folder hiding).
                if role in _EXTERNAL_PROJECTION_ROLES:
                    mod = _path_module(request.url.path)
                    if mod is not None:
                        try:
                            granted = await _granted_modules_cached(
                                request.state.tenant_id or "", role)
                        except Exception:
                            granted = set()
                        if mod not in granted:
                            from starlette.responses import JSONResponse
                            return JSONResponse({"detail": "Not Found"}, status_code=404)
                try:
                    from core.db.base import cc_search_path, cc_user_id
                    cc_search_path.set(_PROJECTION_SCHEMA[role])
                    cc_user_id.set(user.id)
                except Exception:
                    pass
            else:
                # Any other magic-link user (e.g. deal_room_guest) stays confined to /portal.
                p = request.url.path
                if not p.startswith(("/portal", "/auth/", "/api/portal",
                                     "/static", "/favicon.ico", "/manifest.json")):
                    return RedirectResponse(url="/portal/", status_code=302)

        return await call_next(request)

    async def _load_user_from_session(
        self, tenant_id: Optional[str], session_token: str
    ) -> tuple[Optional[User], Optional[str]]:
        """
        Load user from session token. Returns (user, tenant_id).

        Two resolution paths:
          A) tenant_id already known (subdomain resolved): query users directly
             validating the opaque session token against the sessions table.
          B) tenant_id not known: look up sessions table by token string,
             get both user_id and tenant_id, then load the User record.
        """
        from core.db.base import AsyncSessionLocal
        from sqlalchemy import select

        # Path A — tenant already resolved by TenantResolverMiddleware
        if tenant_id:
            # AUTHFIX: raw-uid cookies removed. The cookie is always treated as
            # an opaque session token and validated against the sessions table,
            # tenant-scoped and unexpired. Integer cookies no longer authenticate.
            try:
                async with AsyncSessionLocal() as session:
                    row = await session.execute(
                        sa_text(
                            "SELECT s.user_id FROM sessions s "
                            "WHERE s.token = :token AND s.expires_at > NOW() "
                            "AND TRIM(s.tenant_id) = :tid LIMIT 1"
                        ),
                        {"token": session_token, "tid": tenant_id.strip()},
                    )
                    sess_row = row.fetchone()
                    if not sess_row:
                        return None, None
                    stmt = select(User).where(
                        User.id == sess_row.user_id,
                        User.is_active == True,
                        User.tenant_id == tenant_id.strip(),
                    )
                    result = await session.execute(stmt)
                    user = result.scalar_one_or_none()
                    return user, tenant_id if user else None
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
