"""
M-DESK C1 — Desktop client authentication routes.

Two endpoints:

  POST /api/v1/desktop/auth/token
    Form-encoded body: username + password.
    Authenticates against the tenant's configured adapter (LDAP/Azure/local),
    same as the web login. On success, mints an access + refresh token pair
    and returns them as JSON.

  POST /api/v1/desktop/auth/refresh
    JSON body: refresh_token (string).
    Validates the refresh token, rotates it, and returns a fresh
    access + refresh pair. Atomic — the consumed refresh row is revoked
    in the same transaction that issues the new pair.

Reuses core/auth/routes.py for the actual credential check. M-DESK should
authenticate IDENTICALLY to the web login: one identity story per tenant.
The only behavioral difference is the issued token type (JWT vs session
cookie).

Future production-readiness items NOT included in C1:
  * Per-username rate limiting (recommend 10/min, 60/hour)
  * MFA challenge support (TOTP, WebAuthn)
  * Device binding (cert pin, machine GUID)
  * Audit log table for failed login attempts
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text as sa_text

from core.auth.routes import (
    resolve_tenant_auth,
    _authenticate_ldaps,
    _authenticate_azure,
    _authenticate_local,
)
from core.db.base import AsyncSessionLocal
from modules.desktop import jwt_service

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/v1/desktop/auth", tags=["m-desk-auth"])


# ═════════════════════════════════════════════════════════════════════════
# Request / response models
# ═════════════════════════════════════════════════════════════════════════

class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    access_expires_at: str
    refresh_expires_at: str


class AuthError(BaseModel):
    error: str
    reason: str
    detail: str = ""


# ═════════════════════════════════════════════════════════════════════════
# Helpers — adapter-resolution + user lookup
# ═════════════════════════════════════════════════════════════════════════

async def _get_user_adapter_override(
    tenant_id: str, username: str
) -> Optional[str]:
    """If the user's record has auth_provider='local', honor that.

    Mirrors the per-user override in core/auth/routes.py login flow. Lets
    break-glass local accounts authenticate to the desktop client even
    when the tenant has LDAP/Azure active.
    """
    tid = (tenant_id or "").strip()
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                sa_text("""
                    SELECT auth_provider FROM users
                    WHERE TRIM(tenant_id) = :tid
                      AND (username = :u OR email = :u)
                      AND is_active = TRUE
                    LIMIT 1
                """),
                {"tid": tid, "u": username},
            )
            row = result.first()
            if row and row[0] == "local":
                return "local"
    except Exception as exc:
        logger.warning(
            "[m-desk] per-user adapter check failed user=%s tenant=%s: %s",
            username, tid, exc,
        )
    return None


async def _load_user_record(
    tenant_id: str, username: str
) -> Optional[dict]:
    """Fetch the local users row for the given identity.

    Returns dict with id, email, full_name, role, or None if not found.
    Used after a successful adapter authenticate to fetch the canonical
    user_id (bigint) and role for the JWT claims.
    """
    tid = (tenant_id or "").strip()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa_text("""
                SELECT id, email, full_name, role::text AS role, is_active
                FROM users
                WHERE TRIM(tenant_id) = :tid
                  AND (username = :u OR email = :u)
                  AND is_active = TRUE
                LIMIT 1
            """),
            {"tid": tid, "u": username},
        )
        row = result.mappings().first()
        return dict(row) if row else None


# ═════════════════════════════════════════════════════════════════════════
# POST /token — exchange credentials for an access + refresh pair
# ═════════════════════════════════════════════════════════════════════════

@router.post("/token", response_model=AuthResponse)
async def issue_token(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> AuthResponse:
    """Authenticate username/password and issue a token pair.

    The desktop client posts username + password as application/x-www-form-
    urlencoded, the same content type the web login form uses. Successful
    auth returns a JSON token bundle. Bad credentials return 401.
    """
    tenant_id = (getattr(request.state, "tenant_id", None) or "").strip()
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to resolve tenant from request",
        )

    # ── Per-user override ───────────────────────────────────────────────
    user_override = await _get_user_adapter_override(tenant_id, username)
    if user_override == "local":
        adapter = "local"
        adapter_config: dict = {}
        adapter_credentials: dict = {}
        adapter_source = "user_record"
    else:
        # ── Tenant-level adapter ────────────────────────────────────────
        auth_info = await resolve_tenant_auth(tenant_id)
        adapter = auth_info["adapter"]
        adapter_config = auth_info.get("config") or {}
        adapter_credentials = auth_info.get("credentials") or {}
        adapter_source = auth_info.get("source") or "unknown"

    # ── Authenticate via the right adapter ──────────────────────────────
    if adapter in ("ldaps", "ldap"):
        result = await _authenticate_ldaps(
            username, password, tenant_id,
            config=adapter_config, credentials=adapter_credentials,
        )
    elif adapter in ("azure", "azure_ad"):
        result = await _authenticate_azure(
            username, password, tenant_id,
            config=adapter_config, credentials=adapter_credentials,
        )
    else:
        result = await _authenticate_local(username, password, tenant_id)

    if not result.get("success"):
        logger.info(
            "[m-desk] login failed username=%s tenant=%s adapter=%s reason=%s",
            username, tenant_id, adapter,
            result.get("error") or "invalid_credentials",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    # ── Look up canonical user_id (bigint) for JWT claims ──────────────
    user = await _load_user_record(tenant_id, username)
    if not user:
        # Authenticated by adapter but no local row? Web login auto-provisions
        # in this case (JIT user creation). For desktop we fail closed —
        # the JIT flow on the web path also writes the row to users; if the
        # adapter said yes but the row is missing, something is mid-state
        # and we don't want to issue a JWT against a half-formed identity.
        logger.warning(
            "[m-desk] adapter ok but no local user row username=%s tenant=%s",
            username, tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User record unavailable. Please complete a web login first.",
        )

    pair = await jwt_service.issue_token_pair(
        user_id=int(user["id"]),
        tenant_id=tenant_id,
        email=user["email"] or result.get("email") or "",
        role=user["role"] or "staff",
    )

    logger.info(
        "[m-desk] login ok username=%s user_id=%s tenant=%s adapter=%s "
        "source=%s jti=%s",
        username, user["id"], tenant_id, adapter, adapter_source, pair.jti,
    )
    return AuthResponse(**pair.to_response_dict())


# ═════════════════════════════════════════════════════════════════════════
# POST /refresh — rotate a refresh token for a new pair
# ═════════════════════════════════════════════════════════════════════════

@router.post("/refresh", response_model=AuthResponse)
async def refresh_token(
    request: Request,
    body: RefreshRequest,
) -> AuthResponse:
    """Exchange a refresh token for a new access + refresh pair.

    Rotating refresh: the presented row is revoked in the same transaction
    that inserts the new row. Replaying the same refresh token after rotation
    fails with 401 reason=revoked — that's the compromise-detection signal.
    """
    tenant_id = (getattr(request.state, "tenant_id", None) or "").strip()
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to resolve tenant from request",
        )

    try:
        pair = await jwt_service.refresh_token_pair(
            presented_refresh_token=body.refresh_token,
            expected_tenant_id=tenant_id,
        )
    except jwt_service.RefreshError as exc:
        # Discriminating reasons let the VSTO client decide what to do:
        #   'revoked'         — force re-login, possibly show compromise warning
        #   'expired'         — force re-login (no surprise)
        #   'unknown'         — force re-login
        #   'tenant_mismatch' — force re-login on the right tenant
        logger.info(
            "[m-desk] refresh rejected tenant=%s reason=%s",
            tenant_id, exc.reason,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "refresh_failed", "reason": exc.reason,
                    "message": str(exc)},
        )

    logger.info(
        "[m-desk] refresh ok tenant=%s new_jti=%s", tenant_id, pair.jti,
    )
    return AuthResponse(**pair.to_response_dict())
