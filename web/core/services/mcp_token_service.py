"""MCP token service — issue / list / revoke bearer credentials for the
Praesidium MCP resource servers, and mint short-lived tokens for the
in-app AI chat.

A credential is a signed JWT (HS256, per-scope secret) carrying a `jti`.
The raw JWT is returned to the caller ONCE at issue time; only its
SHA-256 hash + jti are stored. Revocation flips mcp_credentials.revoked_at
and the resource-server verifier rejects the jti before the JWT's exp.

Per-scope secrets (read from files, no env churn):
  mcp:user  -> /app/certs/mcp_user_jwt.secret
  mcp:admin -> /app/certs/mcp_admin_jwt.secret

Multi-token by design: every call to issue_credential() writes a new row;
there is no per-user uniqueness, so a user may hold many live credentials,
each revocable on its own.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

ALGORITHM = "HS256"
ISSUER = os.environ.get("MCP_TOKEN_ISSUER", "https://login.hjmmlegal.com")

# scope -> (secret file, audience URL)
SCOPES: dict[str, dict[str, str]] = {
    "mcp:user": {
        "secret_file": "/app/certs/mcp_user_jwt.secret",
        "audience": "https://mcp-user.praesidium-legal.com/mcp",
    },
    "mcp:admin": {
        "secret_file": "/app/certs/mcp_admin_jwt.secret",
        "audience": "https://mcp-admin.praesidium-legal.com/mcp",
    },
}


def _secret_for(scope: str) -> str:
    cfg = SCOPES.get(scope)
    if not cfg:
        raise ValueError(f"Unknown MCP scope: {scope!r}")
    with open(cfg["secret_file"], "r") as fh:
        s = fh.read().strip()
    if not s:
        raise RuntimeError(f"Empty signing secret for scope {scope}")
    return s


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_jwt(*, scope: str, user_id: int, tenant_id: str, email: str,
               role: str, jti: str, ttl_days: Optional[int]) -> tuple[str, Optional[datetime]]:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tenant_id": (tenant_id or "").strip(),
        "email": email or "",
        "role": role or "staff",
        "scope": scope,
        "aud": SCOPES[scope]["audience"],
        "iss": ISSUER,
        "jti": jti,
        "iat": int(now.timestamp()),
    }
    exp_dt: Optional[datetime] = None
    if ttl_days and ttl_days > 0:
        exp_dt = now + timedelta(days=ttl_days)
        payload["exp"] = int(exp_dt.timestamp())
    token = jwt.encode(payload, _secret_for(scope), algorithm=ALGORITHM)
    return token, exp_dt


async def issue_credential(*, user_id: int, tenant_id: str, email: str, role: str,
                           scope: str, label: str, created_by: Optional[int] = None,
                           ttl_days: Optional[int] = None) -> dict[str, Any]:
    """Issue a new MCP credential. Returns metadata + the raw token (shown ONCE)."""
    if scope not in SCOPES:
        raise ValueError(f"Unknown MCP scope: {scope!r}")
    label = (label or "").strip() or "unnamed"
    jti = str(uuid.uuid4())
    token, exp_dt = _build_jwt(scope=scope, user_id=user_id, tenant_id=tenant_id,
                               email=email, role=role, jti=jti, ttl_days=ttl_days)
    token_hash = _sha256_hex(token)
    async with AsyncSessionLocal() as session:
        row = (await session.execute(sa_text("""
            INSERT INTO mcp_credentials
                (tenant_id, user_id, label, scope, kind, token_hash, jti,
                 created_by, expires_at)
            VALUES
                (:tenant_id, :user_id, :label, :scope, 'api_key', :token_hash,
                 CAST(:jti AS uuid), :created_by, :expires_at)
            RETURNING id, created_at
        """), {
            "tenant_id": (tenant_id or "").strip(),
            "user_id": user_id, "label": label, "scope": scope,
            "token_hash": token_hash, "jti": jti, "created_by": created_by,
            "expires_at": exp_dt,
        })).mappings().one()
        await session.commit()
    return {
        "id": str(row["id"]),
        "label": label,
        "scope": scope,
        "jti": jti,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "expires_at": exp_dt.isoformat() if exp_dt else None,
        "token": token,  # raw — surfaced once, never persisted
    }


async def list_credentials(*, tenant_id: str, include_revoked: bool = True) -> list[dict[str, Any]]:
    where = "WHERE TRIM(tenant_id) = :tenant_id"
    if not include_revoked:
        where += " AND revoked_at IS NULL"
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sa_text(f"""
            SELECT id, user_id, label, scope, kind, jti, created_by, created_at,
                   expires_at, last_used_at, revoked_at, revoke_reason
            FROM mcp_credentials
            {where}
            ORDER BY revoked_at IS NOT NULL, created_at DESC
        """), {"tenant_id": (tenant_id or "").strip()})).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("created_at", "expires_at", "last_used_at", "revoked_at"):
            d[k] = d[k].isoformat() if d.get(k) else None
        d["id"] = str(d["id"]); d["jti"] = str(d["jti"])
        d["active"] = d["revoked_at"] is None and (
            d["expires_at"] is None or d["expires_at"] > datetime.now(timezone.utc).isoformat()
        )
        out.append(d)
    return out


async def revoke_credential(*, cred_id: str, tenant_id: str,
                            reason: str = "manual") -> bool:
    async with AsyncSessionLocal() as session:
        res = await session.execute(sa_text("""
            UPDATE mcp_credentials
               SET revoked_at = NOW(), revoke_reason = :reason
             WHERE id = CAST(:cred_id AS uuid)
               AND TRIM(tenant_id) = :tenant_id
               AND revoked_at IS NULL
        """), {"cred_id": cred_id, "tenant_id": (tenant_id or "").strip(),
               "reason": (reason or "manual")[:64]})
        await session.commit()
        return res.rowcount > 0


async def mint_chat_token(*, user_id: int, tenant_id: str, email: str, role: str,
                          scope: str, ttl_minutes: int = 30) -> str:
    """Short-lived, NON-persisted token for the in-app AI chat. Not revocable
    individually (it expires in minutes); carries a random jti that is simply
    never written to mcp_credentials, so the verifier's revocation check treats
    absence-with-valid-signature as allowed for chat-scoped tokens."""
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=max(1, ttl_minutes))
    payload = {
        "sub": str(user_id), "tenant_id": (tenant_id or "").strip(),
        "email": email or "", "role": role or "staff", "scope": scope,
        "aud": SCOPES[scope]["audience"], "iss": ISSUER,
        "jti": "chat-" + uuid.uuid4().hex, "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, _secret_for(scope), algorithm=ALGORITHM)
