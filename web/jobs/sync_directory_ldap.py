"""
jobs/sync_directory_ldap.py

RQ background job — syncs Active Directory users to Praesidium.

Called by: POST /tenant-admin/users/sync-directory (when connector_type='auth_ldap')
Enqueued by: tenant_admin.py → tenant_users_sync_directory

For each AD user:
  1. Load tenant_connectors.config + credentials_vault for auth_ldap
  2. Pull full user list from LDAP (LDAPSAuthAdapter.list_users)
  3. Match against existing users by email or username
  4. Existing user → update full_name, auth_provider, external_id
                   → upsert user_auth_credentials row
  5. New user      → create users row (is_active=False, pending admin activation)
                   → create user_auth_credentials row
  6. Update connector_entity_sync_log with results

SYNC SEMANTICS:
  - Never deletes users — deactivation is manual
  - Never overwrites password_hash — LDAP users don't have local passwords
  - Disabled AD accounts (UAC flag) → is_active=False in platform
  - Existing active platform users are NOT deactivated even if missing from AD
    (safety: AD restructuring shouldn't lock people out)

CONFIG RESOLUTION (multi-tenant):
  - Reads tenant_connectors.config for non-secret config (URL, base DN, etc.)
  - Reads credentials_vault for bind_dn + bind_password (encrypted Fernet)
  - Falls back to env vars (legacy single-tenant path) if config rows missing
  - SECRET_KEY env var required to decrypt credentials_vault entries
"""
import base64
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def run(tenant_id: str, triggered_by_user_id: int = None, sync_log_id: str = None):
    """
    Synchronous entry point for RQ.
    RQ workers are sync — uses asyncio.run() to execute async logic.
    """
    import asyncio
    asyncio.run(_run_async(tenant_id, triggered_by_user_id, sync_log_id))


def _decrypt_vault_value(encrypted: str) -> str:
    """
    Decrypt a credentials_vault encrypted_key using Fernet.
    Diagnostic version — logs exception type, SECRET_KEY fingerprint, and
    a sample of the encrypted value when decrypt fails.
    """
    if not encrypted:
        return ""

    from cryptography.fernet import Fernet, InvalidToken
    secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    secret_status = (
        f"len={len(secret)} prefix={secret[:8]!r} "
        f"is_default={secret == 'changeme-32-bytes-exactly!!!!!!!'}"
    )
    enc_status = f"len={len(encrypted)} prefix={encrypted[:30]!r}"

    try:
        key_bytes = (secret[:32]).encode().ljust(32, b"0")
        fernet_key = base64.urlsafe_b64encode(key_bytes)
        f = Fernet(fernet_key)
        return f.decrypt(encrypted.encode()).decode()
    except InvalidToken:
        logger.error(
            f"[ldap_sync] Fernet decrypt InvalidToken — "
            f"SECRET_KEY[{secret_status}] CIPHERTEXT[{enc_status}]"
        )
        return ""
    except Exception as e:
        logger.error(
            f"[ldap_sync] Fernet decrypt {type(e).__name__}: {e!r} — "
            f"SECRET_KEY[{secret_status}] CIPHERTEXT[{enc_status}]"
        )
        return ""


async def _load_tenant_ldap_config(tid: str) -> tuple[dict, dict]:
    """
    Load tenant_connectors.config + credentials_vault for the LDAP connector.
    Returns (config_dict, credentials_dict). Either may be empty if rows
    are missing — adapter falls back to env vars in that case.
    """
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal

    config: dict = {}
    credentials: dict = {}

    async with AsyncSessionLocal() as session:
        # tenant_connectors.config — non-secret config (URL, DNs, filters)
        cfg_row = (
            await session.execute(
                text("""
                    SELECT config FROM tenant_connectors
                    WHERE TRIM(tenant_id) = :tid
                      AND connector_type = 'auth_ldap'
                    LIMIT 1
                """),
                {"tid": tid},
            )
        ).first()
        if cfg_row and cfg_row[0]:
            config = dict(cfg_row[0])

        # credentials_vault — decrypted secrets keyed by key_type
        # Match either provider='auth_ldap' (canonical) or provider='ldap'
        # (legacy/transitional naming we saw on appliance).
        cred_rows = (
            await session.execute(
                text("""
                    SELECT key_type, encrypted_key
                    FROM credentials_vault
                    WHERE TRIM(tenant_id) = :tid
                      AND provider IN ('auth_ldap', 'ldap')
                    ORDER BY provider DESC
                """),
                {"tid": tid},
            )
        ).fetchall()

        for key_type, encrypted in cred_rows:
            # Newer 'auth_ldap' rows win since they sort first DESC.
            # Don't overwrite if already populated by the canonical provider.
            if key_type in credentials and credentials[key_type]:
                continue
            decrypted = _decrypt_vault_value(encrypted)
            if decrypted:
                credentials[key_type] = decrypted

    return config, credentials


async def _run_async(tenant_id: str, triggered_by_user_id: int = None, sync_log_id: str = None):
    """Async implementation of the LDAP sync job."""
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal

    tid = tenant_id.strip()
    discovered = 0
    new_count = 0
    auto_matched = 0
    error_msg = None

    # Find the most recent sync log row for this tenant if not passed
    if not sync_log_id:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT id FROM connector_entity_sync_log
                    WHERE TRIM(tenant_id) = :tid AND connector_type = 'auth_ldap'
                    ORDER BY started_at DESC LIMIT 1
                """),
                {"tid": tid}
            )
            row = result.first()
            sync_log_id = str(row[0]) if row else None

    try:
        # ── Load tenant config + credentials ──────────────────────────────────
        config, credentials = await _load_tenant_ldap_config(tid)
        logger.info(
            f"[ldap_sync] tenant={tid} loaded config_keys={sorted(config.keys())} "
            f"cred_keys={sorted(credentials.keys())}"
        )

        # Sanity check before LDAP attempt — we know AD rejects anonymous binds
        if not credentials.get("bind_dn") or not credentials.get("bind_password"):
            error_msg = (
                "Missing service account credentials in credentials_vault. "
                "Bind DN and Bind Password must be configured for the LDAP "
                "connector before sync will work."
            )
            logger.warning(f"[ldap_sync] tenant={tid} {error_msg}")
            await _complete_log(sync_log_id, tid, 0, 0, 0, error_msg)
            return

        # ── Pull users from LDAP using DB-driven config ───────────────────────
        from modules.auth.adapters.auth_adapters import LDAPSAuthAdapter
        adapter = LDAPSAuthAdapter(config=config, credentials=credentials)
        ad_users = await adapter.list_users(tid)
        discovered = len(ad_users)
        logger.info(f"[ldap_sync] tenant={tid} discovered={discovered} AD users")

        if not ad_users:
            logger.warning(f"[ldap_sync] tenant={tid} — no users returned from LDAP")
            await _complete_log(
                sync_log_id, tid, 0, 0, 0,
                "LDAP search returned no users — check user_search_base "
                "and bind account permissions"
            )
            return

        # ── Process each AD user ──────────────────────────────────────────────
        async with AsyncSessionLocal() as session:
            for ad_user in ad_users:
                if not ad_user.email and not ad_user.username:
                    continue

                username = (ad_user.username or "").strip()
                email = (ad_user.email or "").strip().lower()
                full_name = (ad_user.display_name or username or email).strip()

                # ── Match existing user by email or username ───────────────────
                match_result = await session.execute(
                    text("""
                        SELECT id, email, username, full_name, is_active
                        FROM users
                        WHERE TRIM(tenant_id) = :tid
                          AND (
                              (email = :email AND :email != '')
                              OR (username = :username AND :username != '')
                          )
                        LIMIT 1
                    """),
                    {"tid": tid, "email": email, "username": username}
                )
                existing = match_result.mappings().first()

                now = datetime.now(timezone.utc).replace(tzinfo=None)

                if existing:
                    # ── Update existing user ───────────────────────────────────
                    auto_matched += 1
                    user_id = existing["id"]

                    await session.execute(
                        text("""
                            UPDATE users SET
                                full_name     = :full_name,
                                email         = :email,
                                auth_provider = 'ldap',
                                external_id   = :external_id,
                                is_active     = :is_active,
                                updated_at    = :now
                            WHERE id = :uid AND TRIM(tenant_id) = :tid
                        """),
                        {
                            "full_name":   full_name,
                            "email":       email or existing["email"],
                            "external_id": username,
                            "is_active":   ad_user.enabled,
                            "now":         now,
                            "uid":         user_id,
                            "tid":         tid,
                        }
                    )

                else:
                    # ── Create new user (inactive pending admin activation) ────
                    new_count += 1

                    safe_username = username or email.split("@")[0]

                    result = await session.execute(
                        text("""
                            INSERT INTO users
                                (tenant_id, username, email, full_name,
                                 role, is_active, is_timekeeper,
                                 auth_provider, external_id,
                                 created_at, updated_at)
                            VALUES
                                (:tid, :username, :email, :full_name,
                                 'staff', false, false,
                                 'ldap', :external_id,
                                 :now, :now)
                            ON CONFLICT (tenant_id, username) DO UPDATE SET
                                email         = EXCLUDED.email,
                                full_name     = EXCLUDED.full_name,
                                auth_provider = 'ldap',
                                external_id   = EXCLUDED.external_id,
                                updated_at    = EXCLUDED.updated_at
                            RETURNING id
                        """),
                        {
                            "tid":         tid,
                            "username":    safe_username,
                            "email":       email,
                            "full_name":   full_name,
                            "external_id": username,
                            "now":         now,
                        }
                    )
                    row = result.first()
                    user_id = row[0] if row else None

                if not user_id:
                    continue

                # ── Upsert user_auth_credentials ──────────────────────────────
                await session.execute(
                    text("""
                        INSERT INTO user_auth_credentials
                            (tenant_id, user_id, auth_source, external_id,
                             email, upn, display_name, is_primary,
                             meta, last_verified_at, created_at, updated_at)
                        VALUES
                            (:tid, :uid, 'ldap', :external_id,
                             :email, :upn, :display_name, true,
                             CAST(:meta AS jsonb), :now, :now, :now)
                        ON CONFLICT (tenant_id, auth_source, external_id)
                        DO UPDATE SET
                            email          = EXCLUDED.email,
                            upn            = EXCLUDED.upn,
                            display_name   = EXCLUDED.display_name,
                            last_verified_at = EXCLUDED.last_verified_at,
                            updated_at     = EXCLUDED.updated_at
                    """),
                    {
                        "tid":          tid,
                        "uid":          user_id,
                        "external_id":  username,
                        "email":        email,
                        "upn":          email,
                        "display_name": full_name,
                        "meta":         _build_meta(ad_user),
                        "now":          now,
                    }
                )

            await session.commit()

        # Update tenant_connectors.last_sync_at on success
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    UPDATE tenant_connectors SET
                        last_sync_at = NOW(),
                        last_error   = NULL,
                        updated_at   = NOW()
                    WHERE TRIM(tenant_id) = :tid
                      AND connector_type = 'auth_ldap'
                """),
                {"tid": tid},
            )
            await session.commit()

        logger.info(
            f"[ldap_sync] tenant={tid} complete — "
            f"discovered={discovered} new={new_count} matched={auto_matched}"
        )

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)[:400]}".replace("\x00", "")
        logger.exception(f"[ldap_sync] tenant={tid} error: {e}")

        # Also stash on tenant_connectors.last_error for the connector UI
        try:
            from sqlalchemy import text as sa_text
            from core.db.base import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                await session.execute(
                    sa_text("""
                        UPDATE tenant_connectors SET
                            last_error = :err,
                            updated_at = NOW()
                        WHERE TRIM(tenant_id) = :tid
                          AND connector_type = 'auth_ldap'
                    """),
                    {"tid": tid, "err": error_msg},
                )
                await session.commit()
        except Exception as inner:
            logger.warning(f"[ldap_sync] could not record last_error: {inner}")

    finally:
        await _complete_log(sync_log_id, tid, discovered, new_count, auto_matched, error_msg)


async def _complete_log(
    sync_log_id: str,
    tenant_id: str,
    discovered: int,
    new_count: int,
    auto_matched: int,
    error: str = None,
):
    """Update the sync log row with results."""
    if not sync_log_id:
        return
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    UPDATE connector_entity_sync_log SET
                        completed_at        = NOW(),
                        discovered_count    = :discovered,
                        new_count           = :new_count,
                        auto_matched_count  = :auto_matched,
                        error               = :error
                    WHERE id = CAST(:id AS uuid)
                """),
                {
                    "id":           sync_log_id,
                    "discovered":   discovered,
                    "new_count":    new_count,
                    "auto_matched": auto_matched,
                    "error":        (error or "").replace("\x00", "") if error else None,
                }
            )
            await session.commit()
    except Exception as e:
        logger.warning(f"[ldap_sync] failed to update sync log: {e}")


def _build_meta(ad_user) -> str:
    """Build JSON meta from AD user attributes."""
    import json
    meta = {}
    if hasattr(ad_user, "department") and ad_user.department:
        meta["department"] = ad_user.department
    if hasattr(ad_user, "title") and ad_user.title:
        meta["title"] = ad_user.title
    if hasattr(ad_user, "phone") and ad_user.phone:
        meta["phone"] = ad_user.phone
    return json.dumps(meta)
