"""
WebDAV LDAP Authenticator — v2.0 (Appliance)

Reads LDAP config from tenant_connectors + decrypts bind credentials
from credentials_vault. Authenticates WebDAV Basic auth requests against
the tenant's Active Directory.

Resolves tenant from request hostname (docs.{base_domain}).
JIT-provisions users into the users table on first successful bind.

⚖  PATENT NOTICE: Patent Pending — 64/020,027
"""

import base64
import logging
import os
import ssl
from typing import Optional, Tuple

import ldap3
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger("praesidium.webdav.auth")


# ── Fernet decrypt (same key derivation as connectors/service.py) ────────────

def _vault_get_fernet():
    from cryptography.fernet import Fernet
    secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    key_bytes = (secret[:32]).encode().ljust(32, b"0")
    return Fernet(base64.urlsafe_b64encode(key_bytes))


def _vault_decrypt(stored: str) -> str:
    if not stored:
        return ""
    try:
        return _vault_get_fernet().decrypt(stored.encode()).decode()
    except Exception:
        return stored  # plaintext fallback


# ── Tenant resolution ────────────────────────────────────────────────────────

async def resolve_tenant_from_host(hostname: str) -> Optional[dict]:
    """
    Given a hostname like 'docs.hjmmlegal.com', resolve the tenant.
    Looks up tenants where the hostname matches 'docs.' + base_domain
    or where the domain column contains the base domain.
    """
    # Strip 'docs.' prefix to get base domain
    if hostname.startswith("docs."):
        base_domain = hostname[5:]
    else:
        base_domain = hostname

    async with AsyncSessionLocal() as session:
        # Match on domain column containing the base domain
        result = await session.execute(
            text("""
                SELECT id, slug, domain
                FROM tenants
                WHERE domain LIKE :pattern
                LIMIT 1
            """),
            {"pattern": f"%{base_domain}"}
        )
        row = result.mappings().first()

    if not row:
        logger.warning(f"No tenant found for hostname {hostname} (base_domain={base_domain})")
        return None

    return {
        "tenant_id": str(row["id"]).strip(),
        "slug": row["slug"],
        "domain": row["domain"],
    }


# ── LDAP config loader ──────────────────────────────────────────────────────

async def load_ldap_config(tenant_id: str) -> Optional[dict]:
    """
    Load LDAP configuration from tenant_connectors + credentials_vault.
    Returns dict with all LDAP parameters, or None if not configured.
    """
    tid = tenant_id.strip()

    async with AsyncSessionLocal() as session:
        # Get connector config
        result = await session.execute(
            text("""
                SELECT config, is_active
                FROM tenant_connectors
                WHERE TRIM(tenant_id) = :tid
                  AND connector = 'auth_ldap'
                LIMIT 1
            """),
            {"tid": tid}
        )
        row = result.mappings().first()

        if not row or not row["is_active"]:
            return None

        config = row["config"] if isinstance(row["config"], dict) else {}

        # Get bind credentials from vault
        cred_result = await session.execute(
            text("""
                SELECT key_type, encrypted_key
                FROM credentials_vault
                WHERE TRIM(tenant_id) = :tid
                  AND provider = 'auth_ldap'
                  AND key_type IN ('bind_dn', 'bind_password')
            """),
            {"tid": tid}
        )
        creds = {r["key_type"]: _vault_decrypt(r["encrypted_key"]) for r in cred_result.mappings()}

    return {
        "ldap_url": config.get("ldap_url", ""),
        "base_dn": config.get("base_dn", ""),
        "bind_dn": creds.get("bind_dn", ""),
        "bind_password": creds.get("bind_password", ""),
        "user_search_base": config.get("user_search_base", ""),
        "user_filter": config.get("user_filter", "(&(objectClass=user)(sAMAccountName={username}))"),
        "group_search_base": config.get("group_search_base", ""),
        "group_role_map": config.get("group_role_map", "{}"),
        "verify_cert": config.get("verify_cert", "False").lower() != "true",
    }


# ── LDAP authentication ─────────────────────────────────────────────────────

async def authenticate_ldap(
    username: str, password: str, ldap_config: dict
) -> Optional[dict]:
    """
    Authenticate username/password against Active Directory.
    Returns user info dict on success, None on failure.
    """
    ldap_url = ldap_config["ldap_url"]
    bind_dn = ldap_config["bind_dn"]
    bind_password = ldap_config["bind_password"]
    user_search_base = ldap_config["user_search_base"]
    user_filter = ldap_config["user_filter"].replace("{username}", username)

    tls_config = None
    if ldap_url.startswith("ldaps://"):
        tls_config = ldap3.Tls(
            validate=ssl.CERT_NONE,
        )

    try:
        server = ldap3.Server(ldap_url, use_ssl=ldap_url.startswith("ldaps://"), tls=tls_config)

        # Service account bind to search for the user
        conn = ldap3.Connection(server, user=bind_dn, password=bind_password, auto_bind=True)
        conn.search(
            search_base=user_search_base,
            search_filter=user_filter,
            attributes=["sAMAccountName", "mail", "displayName", "memberOf"],
        )

        if not conn.entries:
            logger.info(f"LDAP search returned no results for {username}")
            conn.unbind()
            return None

        entry = conn.entries[0]
        user_dn = str(entry.entry_dn)
        conn.unbind()

        # User bind to verify password
        user_conn = ldap3.Connection(server, user=user_dn, password=password)
        if not user_conn.bind():
            logger.info(f"LDAP bind failed for {username}")
            return None
        user_conn.unbind()

        # Extract attributes
        display_name = str(entry.displayName) if hasattr(entry, "displayName") and entry.displayName else username
        email = str(entry.mail) if hasattr(entry, "mail") and entry.mail else ""
        member_of = [str(g) for g in entry.memberOf] if hasattr(entry, "memberOf") and entry.memberOf else []

        # Resolve role from group membership
        role = "staff"
        try:
            group_role_map = eval(ldap_config.get("group_role_map", "{}"))
            if isinstance(group_role_map, dict):
                for group_cn, mapped_role in group_role_map.items():
                    for member_dn in member_of:
                        if f"CN={group_cn}" in member_dn:
                            role = mapped_role
                            break
        except Exception:
            pass

        return {
            "username": username,
            "email": email,
            "full_name": display_name,
            "role": role,
            "dn": user_dn,
        }

    except ldap3.core.exceptions.LDAPException as e:
        logger.error(f"LDAP error authenticating {username}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in LDAP auth for {username}: {e}")
        return None


# ── JIT user provisioning ────────────────────────────────────────────────────

async def jit_provision_user(tenant_id: str, user_info: dict) -> int:
    """
    Create or update user in the users table. Returns user ID.
    """
    tid = tenant_id.strip()
    username = user_info["username"]

    async with AsyncSessionLocal() as session:
        # Check if user exists
        result = await session.execute(
            text("""
                SELECT id FROM users
                WHERE TRIM(tenant_id) = :tid AND username = :uname
                LIMIT 1
            """),
            {"tid": tid, "uname": username}
        )
        row = result.first()

        if row:
            user_id = row[0]
            # Update last login fields
            await session.execute(
                text("""
                    UPDATE users SET
                        full_name = :fname,
                        email = COALESCE(NULLIF(:email, ''), email),
                        updated_at = NOW()
                    WHERE id = :uid
                """),
                {"uid": user_id, "fname": user_info["full_name"], "email": user_info["email"]}
            )
        else:
            # Create new user
            result = await session.execute(
                text("""
                    INSERT INTO users (tenant_id, username, email, full_name, role, is_active, created_at, updated_at)
                    VALUES (:tid, :uname, :email, :fname, :role, true, NOW(), NOW())
                    RETURNING id
                """),
                {
                    "tid": tid,
                    "uname": username,
                    "email": user_info["email"],
                    "fname": user_info["full_name"],
                    "role": user_info["role"],
                }
            )
            user_id = result.scalar()
            logger.info(f"JIT provisioned user {username} (id={user_id}) for tenant {tid}")

        await session.commit()

    return user_id


# ── Main auth entry point (called by WebDAV middleware) ──────────────────────

async def webdav_authenticate(
    hostname: str, username: str, password: str
) -> Optional[Tuple[str, int, str]]:
    """
    Authenticate a WebDAV request.
    Returns (tenant_id, user_id, username) on success, None on failure.
    """
    tenant = await resolve_tenant_from_host(hostname)
    if not tenant:
        return None

    tenant_id = tenant["tenant_id"]

    ldap_config = await load_ldap_config(tenant_id)
    if not ldap_config:
        logger.warning(f"No LDAP config for tenant {tenant_id}")
        return None

    user_info = await authenticate_ldap(username, password, ldap_config)
    if not user_info:
        return None

    user_id = await jit_provision_user(tenant_id, user_info)

    return (tenant_id, user_id, username)
