"""
core/services/stalwart_service.py
Stalwart Mail Server — User Provisioning Service

Manages Stalwart user accounts via JMAP API. Called from user_mgmt_api.py
on user create/update/deactivate/reactivate events.

API Shape (Stalwart v0.16.5):
  - Endpoint: POST /jmap  (Basic Auth, recovery admin)
  - using: ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:principals", "urn:stalwart:jmap"]
  - x:Account/set  → create, update, destroy
  - x:Account/get  → retrieve by id
  - x:Account/query → list all account ids
  - Account name = username part (before @), email auto-derived as {name}@{domain}
  - domainId "b" = hjmmlegal.com
  - Credentials: {"0": {"@type": "Password", "secret": "..."}}

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import logging
import os
import secrets
import string
from typing import Optional

import httpx

log = logging.getLogger("praesidium.services.stalwart")

# ── Configuration ─────────────────────────────────────────────────────────────

STALWART_JMAP_URL = os.getenv("STALWART_JMAP_URL", "http://10.10.0.10:8080/jmap")
STALWART_ADMIN_USER = os.getenv("STALWART_ADMIN_USER", "admin")
STALWART_ADMIN_PASS = os.getenv("STALWART_ADMIN_PASS", "Praesidium2026!")
STALWART_DOMAIN_ID = os.getenv("STALWART_DOMAIN_ID", "b")   # hjmmlegal.com

JMAP_USING = [
    "urn:ietf:params:jmap:core",
    "urn:ietf:params:jmap:principals",
    "urn:stalwart:jmap",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _generate_temp_password(length: int = 24) -> str:
    """Generate a cryptographically random temporary password."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _username_from_email(email: str) -> str:
    """Extract username part from email address."""
    return email.split("@")[0].lower().strip()


async def _jmap_call(method_calls: list[list], timeout: float = 15.0) -> dict:
    """Execute a JMAP request against Stalwart."""
    payload = {
        "using": JMAP_USING,
        "methodCalls": method_calls,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            STALWART_JMAP_URL,
            json=payload,
            auth=(STALWART_ADMIN_USER, STALWART_ADMIN_PASS),
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


async def _get_stalwart_account_by_name(username: str) -> Optional[dict]:
    """Find a Stalwart account by username. Returns account dict or None."""
    try:
        # Query all account IDs, then get details to match by name
        result = await _jmap_call([
            ["x:Account/get", {}, "t1"],
        ])
        responses = result.get("methodResponses", [])
        if not responses:
            return None
        accounts = responses[0][1].get("list", [])
        for acct in accounts:
            if acct.get("name", "").lower() == username.lower():
                return acct
        return None
    except Exception as e:
        log.warning("Failed to query Stalwart accounts: %s", e)
        return None


# ── Public API ────────────────────────────────────────────────────────────────

async def provision_mail_account(
    email: str,
    full_name: str = "",
    description: str = "",
    password: Optional[str] = None,
) -> dict:
    """
    Create a Stalwart mail account for a Praesidium user.

    Args:
        email: User's email address (e.g. dennis@hjmmlegal.com)
        full_name: Display name
        description: Account description (role/title)
        password: If None, generates a random temporary password

    Returns:
        {"success": True, "stalwart_id": "c", "temp_password": "..."}
        or {"success": False, "error": "..."}
    """
    username = _username_from_email(email)
    temp_password = password or _generate_temp_password()

    # Check if account already exists
    existing = await _get_stalwart_account_by_name(username)
    if existing:
        log.info("Stalwart account already exists for %s (id=%s)", username, existing.get("id"))
        return {
            "success": True,
            "stalwart_id": existing["id"],
            "already_existed": True,
        }

    try:
        result = await _jmap_call([
            ["x:Account/set", {
                "create": {
                    "new_account": {
                        "name": username,
                        "domainId": STALWART_DOMAIN_ID,
                        "@type": "User",
                        "credentials": {
                            "0": {
                                "@type": "Password",
                                "secret": temp_password,
                            }
                        },
                        "roles": {"@type": "User"},
                        "description": description or f"{full_name}".strip(),
                    }
                }
            }, "t1"],
        ])

        responses = result.get("methodResponses", [])
        if not responses:
            return {"success": False, "error": "Empty JMAP response"}

        method_resp = responses[0]
        if method_resp[0] == "error":
            return {"success": False, "error": method_resp[1].get("description", "Unknown error")}

        data = method_resp[1]
        created = data.get("created", {})
        not_created = data.get("notCreated", {})

        if "new_account" in created:
            stalwart_id = created["new_account"]["id"]
            log.info("Created Stalwart account: %s@hjmmlegal.com (id=%s)", username, stalwart_id)
            return {
                "success": True,
                "stalwart_id": stalwart_id,
                "temp_password": temp_password,
            }
        elif "new_account" in not_created:
            err = not_created["new_account"]
            log.error("Failed to create Stalwart account for %s: %s", username, err)
            return {"success": False, "error": err.get("description", str(err))}
        else:
            return {"success": False, "error": f"Unexpected response: {data}"}

    except httpx.HTTPStatusError as e:
        log.error("Stalwart API HTTP error: %s", e)
        return {"success": False, "error": f"HTTP {e.response.status_code}"}
    except Exception as e:
        log.error("Stalwart provisioning error: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


async def disable_mail_account(email: str) -> dict:
    """
    Disable a Stalwart mail account (set credentials to empty).
    
    We don't delete accounts (preserves mailbox data). Instead we
    clear the password so the user can't authenticate.
    """
    username = _username_from_email(email)
    acct = await _get_stalwart_account_by_name(username)
    if not acct:
        log.info("No Stalwart account found for %s — nothing to disable", username)
        return {"success": True, "already_disabled": True}

    stalwart_id = acct["id"]
    try:
        result = await _jmap_call([
            ["x:Account/set", {
                "update": {
                    stalwart_id: {
                        "credentials": {},
                        "description": f"[DISABLED] {acct.get('description', '')}",
                    }
                }
            }, "t1"],
        ])

        responses = result.get("methodResponses", [])
        if responses and responses[0][0] != "error":
            data = responses[0][1]
            if stalwart_id in data.get("updated", {}):
                log.info("Disabled Stalwart account: %s (id=%s)", username, stalwart_id)
                return {"success": True, "stalwart_id": stalwart_id}
            not_updated = data.get("notUpdated", {})
            if stalwart_id in not_updated:
                return {"success": False, "error": str(not_updated[stalwart_id])}

        return {"success": False, "error": "Unexpected response"}
    except Exception as e:
        log.error("Failed to disable Stalwart account %s: %s", username, e)
        return {"success": False, "error": str(e)}


async def enable_mail_account(email: str, password: Optional[str] = None) -> dict:
    """
    Re-enable a disabled Stalwart mail account with a new password.
    """
    username = _username_from_email(email)
    acct = await _get_stalwart_account_by_name(username)
    if not acct:
        log.info("No Stalwart account for %s — provisioning new one", username)
        return await provision_mail_account(email)

    stalwart_id = acct["id"]
    new_password = password or _generate_temp_password()

    try:
        desc = acct.get("description", "").replace("[DISABLED] ", "")
        result = await _jmap_call([
            ["x:Account/set", {
                "update": {
                    stalwart_id: {
                        "credentials": {
                            "0": {
                                "@type": "Password",
                                "secret": new_password,
                            }
                        },
                        "description": desc,
                    }
                }
            }, "t1"],
        ])

        responses = result.get("methodResponses", [])
        if responses and responses[0][0] != "error":
            data = responses[0][1]
            if stalwart_id in data.get("updated", {}):
                log.info("Re-enabled Stalwart account: %s (id=%s)", username, stalwart_id)
                return {
                    "success": True,
                    "stalwart_id": stalwart_id,
                    "temp_password": new_password,
                }

        return {"success": False, "error": "Unexpected response"}
    except Exception as e:
        log.error("Failed to enable Stalwart account %s: %s", username, e)
        return {"success": False, "error": str(e)}


async def update_mail_password(email: str, new_password: str) -> dict:
    """Update the password for a Stalwart mail account."""
    username = _username_from_email(email)
    acct = await _get_stalwart_account_by_name(username)
    if not acct:
        return {"success": False, "error": f"No Stalwart account for {username}"}

    stalwart_id = acct["id"]
    try:
        result = await _jmap_call([
            ["x:Account/set", {
                "update": {
                    stalwart_id: {
                        "credentials": {
                            "0": {
                                "@type": "Password",
                                "secret": new_password,
                            }
                        },
                    }
                }
            }, "t1"],
        ])

        responses = result.get("methodResponses", [])
        if responses and responses[0][0] != "error":
            data = responses[0][1]
            if stalwart_id in data.get("updated", {}):
                log.info("Updated Stalwart password for %s", username)
                return {"success": True, "stalwart_id": stalwart_id}

        return {"success": False, "error": "Unexpected response"}
    except Exception as e:
        log.error("Failed to update Stalwart password for %s: %s", username, e)
        return {"success": False, "error": str(e)}


async def sync_all_users(tenant_id: str) -> dict:
    """
    Bulk sync: ensure every active Praesidium user has a Stalwart account.
    Run once during initial setup or as a reconciliation job.
    
    Returns: {"created": [...], "existing": [...], "errors": [...]}
    """
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text as sa_text

    created = []
    existing = []
    errors = []

    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            sa_text("""
                SELECT id, username, email, full_name, role, is_active
                FROM users
                WHERE tenant_id = :tid AND is_active = true
                  AND email IS NOT NULL AND email != ''
                ORDER BY id
            """),
            {"tid": tenant_id},
        )
        users = rows.fetchall()

    for user in users:
        if not user.email or "@" not in user.email:
            continue

        result = await provision_mail_account(
            email=user.email,
            full_name=user.full_name or user.username,
            description=f"{user.full_name or user.username} — {user.role}",
        )

        if result.get("success"):
            if result.get("already_existed"):
                existing.append(user.email)
            else:
                created.append(user.email)
        else:
            errors.append({"email": user.email, "error": result.get("error")})

    log.info(
        "Stalwart sync complete: %d created, %d existing, %d errors",
        len(created), len(existing), len(errors),
    )
    return {"created": created, "existing": existing, "errors": errors}


async def health_check() -> dict:
    """Check if Stalwart JMAP API is reachable and authenticated."""
    try:
        result = await _jmap_call([
            ["Core/echo", {"health": "check"}, "t0"],
        ])
        responses = result.get("methodResponses", [])
        if responses and responses[0][0] == "Core/echo":
            return {"status": "healthy", "url": STALWART_JMAP_URL}
        return {"status": "degraded", "error": "Unexpected response"}
    except Exception as e:
        return {"status": "unreachable", "error": str(e)}
