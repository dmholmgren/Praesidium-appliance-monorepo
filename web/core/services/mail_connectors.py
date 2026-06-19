"""
core/services/mail_connectors.py — User-scoped personal mailbox connectors.

Resolves a per-user "personal" Stalwart mailbox connector (a *different*
Stalwart server than the firm box) so the Comms viewer can render
firm / personal / combined views from one pipeline.

Storage (no schema migration; reuses existing connector + vault tables):
  tenant_connectors row:
      connector       = 'personal_mail'
      connector_type  = 'stalwart'
      config (jsonb)  = { user_email, jmap_url, account_address, color }
  credentials_vault rows (user-scoped via the provider field):
      provider   = 'personal_mail:<user_email>'
      key_type   in ('admin_user', 'admin_pass')   # server recovery-admin
      encrypted_key = Fernet(SECRET_KEY)            # 'gAAAAA...'

Read model mirrors the firm path: we authenticate to the personal server with
its *recovery-admin* basic-auth and resolve the user's accountId by address
(x:Account/get). We therefore never store the user's own mailbox password —
only the server's admin creds, encrypted in the user-scoped vault.

Author: Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger("praesidium.services.mail_connectors")

CONNECTOR_NAME = "personal_mail"
VAULT_PROVIDER_PREFIX = "personal_mail:"

_PRINCIPAL_USING = [
    "urn:ietf:params:jmap:core",
    "urn:ietf:params:jmap:principals",
    "urn:stalwart:jmap",
]

# Default offset color for personal mail in the combined view (frontend hint).
DEFAULT_PERSONAL_COLOR = "#7c4dff"


@dataclass
class MailboxConnector:
    """A resolved mailbox endpoint for one source within a user's view."""
    source: str                       # 'firm' | 'personal'
    account_id: Optional[str]         # JMAP accountId on its server
    account_address: str              # the mailbox email address
    jmap_url: Optional[str] = None    # None => firm default (STALWART_JMAP_URL)
    auth: Optional[tuple] = None      # None => firm admin creds
    color: Optional[str] = None       # frontend offset color (personal)

    @property
    def ok(self) -> bool:
        return bool(self.account_id)


# -- Fernet (matches email_send_connector._decrypt_creds) --

def _fernet():
    from cryptography.fernet import Fernet
    secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    key_bytes = (secret[:32]).encode().ljust(32, b"0")
    return Fernet(base64.urlsafe_b64encode(key_bytes))


def _decrypt(v: str) -> str:
    if v and v.startswith("gAAAAA"):
        try:
            return _fernet().decrypt(v.encode()).decode()
        except Exception:
            return v
    return v


def _encrypt(v: str) -> str:
    return _fernet().encrypt(v.encode()).decode()


# -- account resolution on an arbitrary Stalwart server --

async def _resolve_account_id(jmap_url: str, auth: tuple,
                              account_address: str) -> Optional[str]:
    """Resolve the JMAP accountId for an address on the given server."""
    name = account_address.split("@")[0].lower().strip()
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(jmap_url,
                             json={"using": _PRINCIPAL_USING,
                                   "methodCalls": [["x:Account/get", {}, "t"]]},
                             auth=auth)
            r.raise_for_status()
            data = r.json()
        for a in data["methodResponses"][0][1].get("list", []):
            if a.get("name", "").lower() == name:
                return a["id"]
    except Exception as e:
        log.warning("[mail_connectors] account resolve failed on %s: %s", jmap_url, e)
    return None


# -- DB access --

async def _load_personal_row(user_email: str) -> Optional[dict]:
    """Return {config, creds} for a user's personal connector, or None."""
    from sqlalchemy import text as sa_text
    from core.db.base import AsyncSessionLocal

    email = (user_email or "").strip().lower()
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT config FROM tenant_connectors
            WHERE connector = :cn AND is_active = true
              AND lower(config->>'user_email') = :em
            ORDER BY updated_at DESC
            LIMIT 1
        """), {"cn": CONNECTOR_NAME, "em": email})
        row = r.fetchone()
        if not row:
            return None
        config = row[0] or {}

        cr = await session.execute(sa_text("""
            SELECT key_type, encrypted_key FROM credentials_vault
            WHERE provider = :pv
        """), {"pv": VAULT_PROVIDER_PREFIX + email})
        creds = {kt: _decrypt(ek) for kt, ek in cr.fetchall()}
    return {"config": config, "creds": creds}


async def get_personal_connector(user_email: str) -> Optional[MailboxConnector]:
    """Build the user's personal MailboxConnector, or None if unconfigured."""
    data = await _load_personal_row(user_email)
    if not data:
        return None
    config, creds = data["config"], data["creds"]
    jmap_url = config.get("jmap_url")
    admin_user = creds.get("admin_user")
    admin_pass = creds.get("admin_pass")
    if not (jmap_url and admin_user and admin_pass):
        log.warning("[mail_connectors] personal connector incomplete for %s", user_email)
        return None
    address = config.get("account_address") or user_email
    auth = (admin_user, admin_pass)
    account_id = await _resolve_account_id(jmap_url, auth, address)
    return MailboxConnector(
        source="personal",
        account_id=account_id,
        account_address=address,
        jmap_url=jmap_url,
        auth=auth,
        color=config.get("color") or DEFAULT_PERSONAL_COLOR,
    )


async def get_firm_connector(user_email: str) -> MailboxConnector:
    """Build the firm MailboxConnector (admin read of the user's firm box)."""
    from core.services.matter_mail_folders import _account_id
    account_id = await _account_id(user_email)
    return MailboxConnector(
        source="firm",
        account_id=account_id,
        account_address=user_email,
        jmap_url=None,
        auth=None,
        color=None,
    )


async def resolve_connectors(user_email: str, source: str = "firm") -> list[MailboxConnector]:
    """Ordered connectors backing a requested view.

    source: 'firm' -> [firm]; 'personal' -> [personal?]; 'combined' -> [firm, personal?]
    Connectors that fail to resolve an accountId are dropped.
    """
    source = (source or "firm").lower()
    out: list[MailboxConnector] = []
    if source in ("firm", "combined"):
        fc = await get_firm_connector(user_email)
        if fc.ok:
            out.append(fc)
    if source in ("personal", "combined"):
        pc = await get_personal_connector(user_email)
        if pc and pc.ok:
            out.append(pc)
    return out


async def has_personal(user_email: str) -> bool:
    """Cheap check (no JMAP) whether the user has a personal connector row."""
    return (await _load_personal_row(user_email)) is not None


# -- provisioning (used to wire a user's box; safe to call repeatedly) --

async def set_personal_connector(user_email: str, *, jmap_url: str,
                                 admin_user: str, admin_pass: str,
                                 account_address: Optional[str] = None,
                                 color: Optional[str] = None,
                                 tenant_id: Optional[str] = None) -> dict:
    """Upsert a user's personal connector row + store admin creds in the vault."""
    from sqlalchemy import text as sa_text
    from core.db.base import AsyncSessionLocal
    import json as _json

    email = (user_email or "").strip().lower()
    address = account_address or email
    config = {
        "user_email": email,
        "jmap_url": jmap_url,
        "account_address": address,
        "color": color or DEFAULT_PERSONAL_COLOR,
    }
    provider = VAULT_PROVIDER_PREFIX + email

    async with AsyncSessionLocal() as session:
        # tenant_id: reuse an existing connector's tenant if not given.
        tid = tenant_id
        if not tid:
            r = await session.execute(sa_text(
                "SELECT tenant_id FROM tenant_connectors ORDER BY created_at LIMIT 1"))
            row = r.fetchone()
            tid = (row[0].strip() if row else "00000000-0000-0000-0000-000000000000")

        # remove any prior row for this user (one personal box per user here)
        await session.execute(sa_text("""
            DELETE FROM tenant_connectors
            WHERE connector = :cn AND lower(config->>'user_email') = :em
        """), {"cn": CONNECTOR_NAME, "em": email})
        await session.execute(sa_text("""
            INSERT INTO tenant_connectors
                (tenant_id, connector, connector_type, config, is_active, status)
            VALUES (:tid, :cn, 'stalwart', CAST(:cfg AS jsonb), true, 'active')
        """), {"tid": tid, "cn": CONNECTOR_NAME, "cfg": _json.dumps(config)})

        for kt, val in (("admin_user", admin_user), ("admin_pass", admin_pass)):
            await session.execute(sa_text("""
                DELETE FROM credentials_vault WHERE provider = :pv AND key_type = :kt
            """), {"pv": provider, "kt": kt})
            await session.execute(sa_text("""
                INSERT INTO credentials_vault
                    (tenant_id, provider, key_type, encrypted_key, key_hint)
                VALUES (:tid, :pv, :kt, :ek, :hint)
            """), {"tid": tid, "pv": provider, "kt": kt,
                   "ek": _encrypt(val),
                   "hint": (val[:2] + "***") if kt == "admin_user" else "***"})
        await session.commit()

    return {"ok": True, "user_email": email, "jmap_url": jmap_url,
            "account_address": address}
