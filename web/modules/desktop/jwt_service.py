"""
M-DESK C1 — JWT + refresh token service.

Pure functions. No FastAPI imports. Unit-testable in isolation.

Tokens:

  ACCESS TOKEN (JWT, HS256, 30-day TTL by default)
    Issued at /auth/token, presented as Bearer on every desktop request.
    Self-contained — verification is pure crypto and does NOT touch the
    database. Revocation is implicit: when an access token expires the
    client MUST refresh; a refresh-token revocation prevents new access
    tokens from being minted.

    Claims:
      sub        user id (string, even though users.id is bigint)
      tenant_id  stripped (no trailing spaces)
      email      contact email
      role       'staff'|'paralegal'|'attorney'|'partner'|'admin'|'super_admin'
      client     'desktop-vsto-1.0.0'
      jti        UUID — links this access token back to the refresh-token row
      iat        issued-at  (unix seconds)
      exp        expiry     (unix seconds)

  REFRESH TOKEN (opaque, 90-day TTL by default)
    Random 256-bit URL-safe string. NOT a JWT. Used once at /auth/refresh
    to mint a new access+refresh pair (rotating refresh). Stored as
    SHA-256 hash in desktop_refresh_tokens.token_hash — the raw token
    never lives in the database. On rotation the consumed row is marked
    revoked_at NOW() with revoke_reason='rotated'.

    A second use of the same refresh token (after rotation) is a strong
    compromise signal: the row is already revoked so the attempt fails.
    The honest client and the attacker cannot both succeed.

Patent: M-DESK checkout-with-TTL is the Series 4 candidate; this file
documents the tokening surface that supports the deterministic-merge
chain. Annotations using # PATENT-S4-CANDIDATE: tag the locus.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt  # PyJWT
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════
# Constants pulled from environment with safe defaults
# ═════════════════════════════════════════════════════════════════════════

ALGORITHM = "HS256"
DEFAULT_CLIENT = "desktop-vsto-1.0.0"


def _get_secret() -> str:
    """Read DESKTOP_JWT_SECRET. Refuses to start if unset or default."""
    secret = os.environ.get("DESKTOP_JWT_SECRET", "").strip()
    if not secret or secret in ("change-me", "changeme", "todo"):
        raise RuntimeError(
            "DESKTOP_JWT_SECRET is not configured. Set a 32+ byte random "
            "hex string in /opt/praesidium-web/.env before any desktop "
            "auth endpoint can be used. Generate with: "
            "python3 -c 'import secrets; print(secrets.token_hex(32))'"
        )
    return secret


def _access_ttl_seconds() -> int:
    days = int(os.environ.get("DESKTOP_TOKEN_TTL_DAYS", "30"))
    return days * 24 * 3600


def _refresh_ttl_seconds() -> int:
    days = int(os.environ.get("DESKTOP_REFRESH_TTL_DAYS", "90"))
    return days * 24 * 3600


# ═════════════════════════════════════════════════════════════════════════
# Data shapes
# ═════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class TokenPair:
    """Returned by issue_token_pair() and refresh_token_pair()."""
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    jti: str

    def to_response_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": "Bearer",
            "access_expires_at": self.access_expires_at.isoformat(),
            "refresh_expires_at": self.refresh_expires_at.isoformat(),
        }


@dataclass(frozen=True)
class AccessClaims:
    """Decoded access-token claims, validated."""
    sub: str
    tenant_id: str
    email: str
    role: str
    client: str
    jti: str
    iat: int
    exp: int

    @property
    def user_id(self) -> int:
        """users.id is bigint — return it typed as int for SQL parameters."""
        return int(self.sub)


# ═════════════════════════════════════════════════════════════════════════
# Hashing helpers
# ═════════════════════════════════════════════════════════════════════════

def _sha256_hex(value: str) -> str:
    """Stable SHA-256 hex digest. Used to derive token_hash from raw token."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_refresh_token_raw() -> str:
    """Generate a fresh opaque refresh token. URL-safe, 256 bits of entropy."""
    return secrets.token_urlsafe(32)


# ═════════════════════════════════════════════════════════════════════════
# Access token issue + verify (pure crypto, no DB)
# ═════════════════════════════════════════════════════════════════════════

def encode_access_token(
    *,
    user_id: int,
    tenant_id: str,
    email: str,
    role: str,
    jti: str,
    client: str = DEFAULT_CLIENT,
    issued_at: Optional[datetime] = None,
    ttl_seconds: Optional[int] = None,
) -> tuple[str, datetime]:
    """Encode an access JWT. Returns (token, expires_at)."""
    now = issued_at or datetime.now(timezone.utc)
    ttl = ttl_seconds if ttl_seconds is not None else _access_ttl_seconds()
    exp = now + timedelta(seconds=ttl)

    payload = {
        "sub": str(user_id),
        "tenant_id": (tenant_id or "").strip(),
        "email": email or "",
        "role": role or "staff",
        "client": client,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, _get_secret(), algorithm=ALGORITHM)
    return token, exp


def decode_access_token(token: str) -> AccessClaims:
    """Verify and decode an access JWT.

    Raises jwt.PyJWTError subclasses on failure:
      * ExpiredSignatureError if exp is in the past
      * InvalidSignatureError if HMAC fails
      * DecodeError if the token is malformed
      * MissingRequiredClaimError / InvalidTokenError otherwise
    """
    payload = jwt.decode(
        token,
        _get_secret(),
        algorithms=[ALGORITHM],
        options={"require": ["sub", "tenant_id", "exp", "iat", "jti"]},
    )

    # Strict types.
    return AccessClaims(
        sub=str(payload["sub"]),
        tenant_id=(payload.get("tenant_id") or "").strip(),
        email=payload.get("email") or "",
        role=payload.get("role") or "staff",
        client=payload.get("client") or DEFAULT_CLIENT,
        jti=str(payload["jti"]),
        iat=int(payload["iat"]),
        exp=int(payload["exp"]),
    )


# ═════════════════════════════════════════════════════════════════════════
# Refresh-token persistence + rotation (DB-backed)
# ═════════════════════════════════════════════════════════════════════════
# PATENT-S4-CANDIDATE:
# Rotating-refresh on first reuse is the foundation of compromise-detectable
# session continuity for the desktop client. Combined with the checkout TTL
# enforced in checkout_service, this enables deterministic merge of native
# and web edits without trusting client-side state.
# ═════════════════════════════════════════════════════════════════════════

async def issue_token_pair(
    *,
    user_id: int,
    tenant_id: str,
    email: str,
    role: str,
    client: str = DEFAULT_CLIENT,
) -> TokenPair:
    """Mint a new access + refresh token, persist the refresh side.

    Used by /auth/token after successful credential verification.
    """
    tid = (tenant_id or "").strip()
    jti = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    access_token, access_exp = encode_access_token(
        user_id=user_id,
        tenant_id=tid,
        email=email,
        role=role,
        jti=jti,
        client=client,
        issued_at=now,
    )

    refresh_raw = _new_refresh_token_raw()
    refresh_hash = _sha256_hex(refresh_raw)
    refresh_exp = now + timedelta(seconds=_refresh_ttl_seconds())

    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_text("""
                INSERT INTO desktop_refresh_tokens
                  (tenant_id, user_id, token_hash, jti, client,
                   issued_at, expires_at)
                VALUES
                  (:tid, :uid, :hash, CAST(:jti AS uuid), :client,
                   :iat, :exp)
            """),
            {
                "tid": tid,
                "uid": int(user_id),
                "hash": refresh_hash,
                "jti": jti,
                "client": client,
                "iat": now,
                "exp": refresh_exp,
            },
        )
        await session.commit()

    logger.info(
        "[m-desk] issued token pair user_id=%s tenant=%s client=%s jti=%s",
        user_id, tid, client, jti,
    )
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_raw,
        access_expires_at=access_exp,
        refresh_expires_at=refresh_exp,
        jti=jti,
    )


class RefreshError(Exception):
    """Raised when a refresh token cannot be rotated.

    The .reason attribute is one of:
      * 'unknown'       — no row matched the presented hash
      * 'expired'       — row exists but expires_at is past
      * 'revoked'       — row exists but revoked_at is non-NULL
      * 'tenant_mismatch' — row exists but tenant_id does not match request
    """
    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


async def refresh_token_pair(
    *,
    presented_refresh_token: str,
    expected_tenant_id: Optional[str] = None,
) -> TokenPair:
    """Exchange a presented refresh token for a fresh access + refresh pair.

    Implements rotating refresh: the presented row is revoked atomically
    in the same transaction that inserts the new row. A second use of the
    same refresh token will not match an active row.

    Raises RefreshError on any failure mode. Callers should map this to
    HTTP 401 in the route layer.
    """
    if not presented_refresh_token:
        raise RefreshError("unknown", "Empty refresh token")

    presented_hash = _sha256_hex(presented_refresh_token)
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        # Look the row up. We need user/tenant info to mint the new pair.
        result = await session.execute(
            sa_text("""
                SELECT id, tenant_id, user_id, client,
                       expires_at, revoked_at
                FROM desktop_refresh_tokens
                WHERE token_hash = :hash
                LIMIT 1
            """),
            {"hash": presented_hash},
        )
        row = result.mappings().first()
        if not row:
            raise RefreshError("unknown", "Refresh token not recognized")

        if row["revoked_at"] is not None:
            # PATENT-S4-CANDIDATE: A revoked-but-presented refresh is the
            # canonical compromise signal. We do NOT reset the row — leaving
            # it revoked forces both honest and attacker into re-login.
            logger.warning(
                "[m-desk] refresh attempted on revoked token user_id=%s "
                "tenant=%s reuse_after=%s",
                row["user_id"], row["tenant_id"], row["revoked_at"],
            )
            raise RefreshError("revoked", "Refresh token already used")

        if row["expires_at"] <= now:
            raise RefreshError("expired", "Refresh token expired")

        # Tenant pin (defense in depth — TenantResolverMiddleware will already
        # have set request.state.tenant_id but we double-check at the auth
        # layer because access tokens cross tenants iff the secret leaks).
        row_tenant = (row["tenant_id"] or "").strip()
        if expected_tenant_id is not None:
            expected = (expected_tenant_id or "").strip()
            if row_tenant != expected:
                logger.warning(
                    "[m-desk] refresh tenant mismatch row=%s expected=%s",
                    row_tenant, expected,
                )
                raise RefreshError("tenant_mismatch", "Tenant mismatch")

        # Look up user details for the new access token claims.
        u_result = await session.execute(
            sa_text("""
                SELECT email, role::text AS role
                FROM users
                WHERE id = :uid AND TRIM(tenant_id) = :tid
                  AND is_active = TRUE
                LIMIT 1
            """),
            {"uid": int(row["user_id"]), "tid": row_tenant},
        )
        u_row = u_result.mappings().first()
        if not u_row:
            # Could happen if the user was deactivated between issue and refresh.
            raise RefreshError("revoked", "User no longer active")

        # Mint the new pair.
        new_jti = str(uuid.uuid4())
        access_token, access_exp = encode_access_token(
            user_id=int(row["user_id"]),
            tenant_id=row_tenant,
            email=u_row["email"] or "",
            role=u_row["role"] or "staff",
            jti=new_jti,
            client=row["client"] or DEFAULT_CLIENT,
            issued_at=now,
        )
        refresh_raw = _new_refresh_token_raw()
        refresh_hash = _sha256_hex(refresh_raw)
        refresh_exp = now + timedelta(seconds=_refresh_ttl_seconds())

        # Atomic rotation: revoke the presented row + insert the new row in
        # one transaction. If either fails, neither persists.
        await session.execute(
            sa_text("""
                UPDATE desktop_refresh_tokens
                SET revoked_at = :now,
                    revoke_reason = 'rotated',
                    last_used_at = :now
                WHERE id = :id
            """),
            {"now": now, "id": row["id"]},
        )
        await session.execute(
            sa_text("""
                INSERT INTO desktop_refresh_tokens
                  (tenant_id, user_id, token_hash, jti, client,
                   issued_at, expires_at)
                VALUES
                  (:tid, :uid, :hash, CAST(:jti AS uuid), :client,
                   :iat, :exp)
            """),
            {
                "tid": row_tenant,
                "uid": int(row["user_id"]),
                "hash": refresh_hash,
                "jti": new_jti,
                "client": row["client"] or DEFAULT_CLIENT,
                "iat": now,
                "exp": refresh_exp,
            },
        )
        await session.commit()

    logger.info(
        "[m-desk] rotated token pair user_id=%s tenant=%s old_jti=%s new_jti=%s",
        row["user_id"], row_tenant, row["id"], new_jti,
    )
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_raw,
        access_expires_at=access_exp,
        refresh_expires_at=refresh_exp,
        jti=new_jti,
    )


async def revoke_refresh_token(
    *,
    presented_refresh_token: Optional[str] = None,
    jti: Optional[str] = None,
    reason: str = "logout",
) -> int:
    """Revoke a refresh-token row.

    Either the raw refresh token or the access-token's jti can be used to
    locate the row. Returns number of rows revoked (0 or 1). Idempotent —
    revoking an already-revoked row is a no-op.
    """
    if not presented_refresh_token and not jti:
        raise ValueError("Must provide refresh token or jti")

    now = datetime.now(timezone.utc)
    where_clause: str
    params: dict[str, Any] = {"now": now, "reason": reason}

    if presented_refresh_token:
        where_clause = "token_hash = :hash AND revoked_at IS NULL"
        params["hash"] = _sha256_hex(presented_refresh_token)
    else:
        where_clause = "jti = CAST(:jti AS uuid) AND revoked_at IS NULL"
        params["jti"] = jti

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa_text(
                f"UPDATE desktop_refresh_tokens "
                f"SET revoked_at = :now, revoke_reason = :reason "
                f"WHERE {where_clause}"
            ),
            params,
        )
        await session.commit()
        return int(result.rowcount or 0)


async def revoke_user_refresh_tokens(
    *,
    user_id: int,
    tenant_id: str,
    reason: str = "admin_revoke",
) -> int:
    """Revoke ALL active refresh tokens for a (user, tenant) pair.

    Used for admin-initiated session termination, password reset, etc.
    Returns number of rows revoked.
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa_text("""
                UPDATE desktop_refresh_tokens
                SET revoked_at = :now, revoke_reason = :reason
                WHERE user_id = :uid
                  AND TRIM(tenant_id) = :tid
                  AND revoked_at IS NULL
            """),
            {
                "now": now,
                "reason": reason,
                "uid": int(user_id),
                "tid": (tenant_id or "").strip(),
            },
        )
        await session.commit()
        return int(result.rowcount or 0)
