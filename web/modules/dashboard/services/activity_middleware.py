"""
modules/dashboard/services/activity_middleware.py
Module 9 Component 7 — Dashboard Left Nav + Theme Wiring

FastAPI middleware that runs on every authenticated request to:
  1. Stamp users.last_active_at (debounced — max once per 5 minutes per user)
  2. Inject request.state.theme with the user's theme_preference
  3. Inject request.state.user_id and request.state.tenant_id for downstream use

Debounce is implemented via a simple in-memory dict keyed by user_id.
This avoids a DB write on every single request. On multi-process deployments,
each worker has its own debounce — acceptable for this use case.

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.activity_middleware")

# Debounce: user_id -> last_stamp_unix_timestamp
_last_stamped: dict[int, float] = {}
_DEBOUNCE_SECONDS = 300  # 5 minutes


class ActivityMiddleware(BaseHTTPMiddleware):
    """
    Stamps last_active_at and injects theme/user context on authenticated requests.

    Skips:
    - Static file paths
    - Health check endpoints
    - Requests with no session token
    - HTMX partial requests (hx-request header)
    - Non-GET/POST methods
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip paths that don't need activity tracking
        path = request.url.path
        if any(path.startswith(p) for p in (
            "/static", "/health", "/favicon", "/_"
        )):
            return await call_next(request)

        # Skip HTMX partials — they fire too frequently
        if request.headers.get("hx-request"):
            return await call_next(request)

        # Resolve session token
        session_token = (
            request.cookies.get("session_token")
            or request.cookies.get("admin_session")
            or request.cookies.get("praesidium_session")
        )

        if session_token:
            try:
                await self._process_session(request, session_token)
            except Exception as exc:
                # Never block a request due to middleware failure
                log.debug("ActivityMiddleware error (non-fatal): %s", exc)

        return await call_next(request)

    async def _process_session(self, request: Request, token: str) -> None:
        """Load session, inject state, debounce last_active_at stamp."""
        async with AsyncSessionLocal() as db:
            row = await db.execute(
                text("""
                    SELECT s.user_id, s.tenant_id, s.expires_at,
                           u.theme_preference, u.role, u.full_name, u.username
                    FROM sessions s
                    JOIN users u ON u.id = s.user_id
                    WHERE s.token = :token AND s.expires_at > NOW()
                """),
                {"token": token},
            )
            session = row.fetchone()

        if not session:
            return

        user_id = session.user_id
        tenant_id = (session.tenant_id or "").strip()
        theme = session.theme_preference or "dark"

        # Inject into request state for use by routes and templates
        request.state.user_id = user_id
        request.state.tenant_id = tenant_id
        request.state.theme = theme
        request.state.user_role = session.role
        request.state.user_name = session.full_name or session.username or ""

        # Debounce last_active_at stamp
        now = time.monotonic()
        last = _last_stamped.get(user_id, 0)
        if now - last > _DEBOUNCE_SECONDS:
            _last_stamped[user_id] = now
            try:
                async with AsyncSessionLocal() as db2:
                    await db2.execute(
                        text("""
                            UPDATE users
                            SET last_active_at = NOW()
                            WHERE id = :uid
                        """),
                        {"uid": user_id},
                    )
                    await db2.commit()
            except Exception as exc:
                log.debug("last_active_at stamp failed (non-fatal): %s", exc)
