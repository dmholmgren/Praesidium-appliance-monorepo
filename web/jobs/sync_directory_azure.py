"""
jobs/sync_directory_azure.py

RQ background job — syncs Azure AD / Entra ID users to Praesidium.
Parallel structure to jobs/sync_directory_ldap.py.

Loads tenant_connectors.config (graph endpoint, scopes) and
credentials_vault (tenant_id, client_id, client_secret) — passes both
to AzureADAuthAdapter constructor.
"""
import base64
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def run(tenant_id: str, triggered_by_user_id: int = None, sync_log_id: str = None):
    """Synchronous entry point for RQ."""
    import asyncio
    asyncio.run(_run_async(tenant_id, triggered_by_user_id, sync_log_id))


def _decrypt_vault_value(encrypted: str) -> str:
    """Decrypt credentials_vault encrypted_key using Fernet."""
    if not encrypted:
        return ""
    try:
        from cryptography.fernet import Fernet
        secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
        key_bytes = (secret[:32]).encode().ljust(32, b"0")
        fernet_key = base64.urlsafe_b64encode(key_bytes)
        f = Fernet(fernet_key)
        return f.decrypt(encrypted.encode()).decode()
    except Exception as e:
        logger.error(f"[azure_sync] Fernet decrypt failed: {e}")
        return ""


async def _load_tenant_azure_config(tid: str) -> tuple[dict, dict]:
    """Load tenant_connectors.config + credentials_vault for Azure."""
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal

    config: dict = {}
    credentials: dict = {}

    async with AsyncSessionLocal() as session:
        cfg_row = (
            await session.execute(
                text("""
                    SELECT config FROM tenant_connectors
                    WHERE TRIM(tenant_id) = :tid
                      AND connector_type = 'auth_azure'
                    LIMIT 1
                """),
                {"tid": tid},
            )
        ).first()
        if cfg_row and cfg_row[0]:
            config = dict(cfg_row[0])

        cred_rows = (
            await session.execute(
                text("""
                    SELECT key_type, encrypted_key
                    FROM credentials_vault
                    WHERE TRIM(tenant_id) = :tid
                      AND provider IN ('auth_azure', 'azure_ad', 'azure')
                    ORDER BY provider DESC
                """),
                {"tid": tid},
            )
        ).fetchall()

        for key_type, encrypted in cred_rows:
            if key_type in credentials and credentials[key_type]:
                continue
            decrypted = _decrypt_vault_value(encrypted)
            if decrypted:
                credentials[key_type] = decrypted

    return config, credentials


async def _run_async(tenant_id: str, triggered_by_user_id: int = None, sync_log_id: str = None):
    """Async implementation of the Azure AD sync job."""
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal

    tid = tenant_id.strip()
    discovered = 0
    new_count = 0
    auto_matched = 0
    error_msg = None

    if not sync_log_id:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT id FROM connector_entity_sync_log
                    WHERE TRIM(tenant_id) = :tid AND connector_type = 'auth_azure'
                    ORDER BY started_at DESC LIMIT 1
                """),
                {"tid": tid}
            )
            row = result.first()
            sync_log_id = str(row[0]) if row else None

    try:
        config, credentials = await _load_tenant_azure_config(tid)
        logger.info(
            f"[azure_sync] tenant={tid} loaded config_keys={sorted(config.keys())} "
            f"cred_keys={sorted(credentials.keys())}"
        )

        if not credentials.get("client_id") or not credentials.get("client_secret"):
            error_msg = (
                "Missing Azure AD application credentials in credentials_vault. "
                "Client ID and Client Secret must be configured."
            )
            logger.warning(f"[azure_sync] tenant={tid} {error_msg}")
            await _complete_log(sync_log_id, tid, 0, 0, 0, error_msg)
            return

        from modules.auth.adapters.auth_adapters import AzureADAuthAdapter
        adapter = AzureADAuthAdapter(config=config, credentials=credentials)
        az_users = await adapter.list_users(tid)
        discovered = len(az_users)
        logger.info(f"[azure_sync] tenant={tid} discovered={discovered} Azure AD users")

        if not az_users:
            await _complete_log(
                sync_log_id, tid, 0, 0, 0,
                "Azure Graph returned no users — check application permissions"
            )
            return

        async with AsyncSessionLocal() as session:
            for az_user in az_users:
                if not az_user.email and not az_user.username:
                    continue

                username = (az_user.username or "").strip()
                email = (az_user.email or "").strip().lower()
                full_name = (az_user.display_name or username or email).strip()

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
                now = datetime.now(timezone.utc)

                if existing:
                    auto_matched += 1
                    user_id = existing["id"]
                    await session.execute(
                        text("""
                            UPDATE users SET
                                full_name     = :full_name,
                                email         = :email,
                                auth_provider = 'azure_ad',
                                external_id   = :external_id,
                                is_active     = :is_active,
                                updated_at    = :now
                            WHERE id = :uid AND TRIM(tenant_id) = :tid
                        """),
                        {
                            "full_name":   full_name,
                            "email":       email or existing["email"],
                            "external_id": username,
                            "is_active":   az_user.enabled,
                            "now":         now,
                            "uid":         user_id,
                            "tid":         tid,
                        }
                    )
                else:
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
                                 'azure_ad', :external_id,
                                 :now, :now)
                            ON CONFLICT (tenant_id, username) DO UPDATE SET
                                email         = EXCLUDED.email,
                                full_name     = EXCLUDED.full_name,
                                auth_provider = 'azure_ad',
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

                await session.execute(
                    text("""
                        INSERT INTO user_auth_credentials
                            (tenant_id, user_id, auth_source, external_id,
                             email, upn, display_name, is_primary,
                             meta, last_verified_at, created_at, updated_at)
                        VALUES
                            (:tid, :uid, 'azure_ad', :external_id,
                             :email, :upn, :display_name, true,
                             :meta::jsonb, :now, :now, :now)
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
                        "upn":          username,
                        "display_name": full_name,
                        "meta":         _build_meta(az_user),
                        "now":          now,
                    }
                )

            await session.commit()

        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    UPDATE tenant_connectors SET
                        last_sync_at = NOW(),
                        last_error   = NULL,
                        updated_at   = NOW()
                    WHERE TRIM(tenant_id) = :tid
                      AND connector_type = 'auth_azure'
                """),
                {"tid": tid},
            )
            await session.commit()

        logger.info(
            f"[azure_sync] tenant={tid} complete — "
            f"discovered={discovered} new={new_count} matched={auto_matched}"
        )

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)[:400]}"
        logger.exception(f"[azure_sync] tenant={tid} error: {e}")

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
                          AND connector_type = 'auth_azure'
                    """),
                    {"tid": tid, "err": error_msg},
                )
                await session.commit()
        except Exception as inner:
            logger.warning(f"[azure_sync] could not record last_error: {inner}")

    finally:
        await _complete_log(sync_log_id, tid, discovered, new_count, auto_matched, error_msg)


async def _complete_log(sync_log_id, tenant_id, discovered, new_count, auto_matched, error=None):
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
                    WHERE id = :id::uuid
                """),
                {
                    "id":           sync_log_id,
                    "discovered":   discovered,
                    "new_count":    new_count,
                    "auto_matched": auto_matched,
                    "error":        error,
                }
            )
            await session.commit()
    except Exception as e:
        logger.warning(f"[azure_sync] failed to update sync log: {e}")


def _build_meta(az_user) -> str:
    import json
    meta = {}
    if hasattr(az_user, "department") and az_user.department:
        meta["department"] = az_user.department
    if hasattr(az_user, "title") and az_user.title:
        meta["title"] = az_user.title
    if hasattr(az_user, "phone") and az_user.phone:
        meta["phone"] = az_user.phone
    return json.dumps(meta)
