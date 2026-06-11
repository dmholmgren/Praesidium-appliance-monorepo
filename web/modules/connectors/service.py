"""
modules/connectors/service.py

ConnectorService — manages connector CRUD, sync log, status, and RQ job dispatch.

ARCHITECTURE:
connector_registry  — platform-level catalog of connector types (global, no tenant_id)
tenant_connectors   — per-tenant configured instances of connector types

All UI-facing methods read metadata from connector_registry (DB).
get_connector_meta() from base.py is used only by background RQ jobs.
"""
from __future__ import annotations
import json
import logging
import secrets
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.connectors.base import ConnectorStatus, get_connector_meta

logger = logging.getLogger(__name__)



# ---------------------------------------------------------------------------
# credentials_vault crypto — Fernet keyed off SECRET_KEY
# ---------------------------------------------------------------------------

import base64 as _vault_base64
import os as _vault_os


def _vault_get_fernet():
    from cryptography.fernet import Fernet
    secret = _vault_os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    key_bytes = (secret[:32]).encode().ljust(32, b"0")
    return Fernet(_vault_base64.urlsafe_b64encode(key_bytes))


def _vault_encrypt(plaintext):
    if plaintext is None:
        return ""
    return _vault_get_fernet().encrypt(str(plaintext).encode()).decode()


def _vault_decrypt(stored):
    if not stored:
        return ""
    try:
        return _vault_get_fernet().decrypt(stored.encode()).decode()
    except Exception:
        logger.info("_vault_decrypt: row appears to be plaintext (pre-encryption fix)")
        return stored


class ConnectorService:
    """
    All connector operations go through this service.
    Reads connector_registry for type metadata.
    Reads/writes tenant_connectors, connector_sync_log, connector_csv_imports.
    """

    # ── Group display metadata ─────────────────────────────────────────────────
    GROUP_META: Dict[str, Dict[str, str]] = {
        "auth":       {"label": "Authentication",     "description": "How users log in to this tenant."},
        "data":       {"label": "Data Sources",       "description": "File systems, email servers, and document repositories that feed the platform."},
        "billing":    {"label": "Billing & Time",     "description": "Time capture, billing systems, and call records that feed time entries and invoices."},
        "research":   {"label": "Legal Research",     "description": "Court data, dockets, and legal research platforms."},
        "ediscovery": {"label": "eDiscovery Sources", "description": "Direct client custodian connections for matter-scoped ingestion. Configured per matter in the eDiscovery workspace."},
        "admin":      {"label": "Administrative",     "description": "Platform administration and infrastructure connectors."},
    }

    GROUP_ORDER: List[str] = ["auth", "data", "billing", "research", "ediscovery", "admin"]

    SYNC_LABELS: Dict[str, str] = {
        "agent_push":  "Agent Push",
        "api_pull":    "API Pull",
        "csv_import":  "CSV Import",
        "oauth2_pull": "OAuth2",
        "internal":    "Built-in",
        "webhook":     "Webhook",
        "manual":      "Manual",
    }

    # ── Registry (catalog) methods ─────────────────────────────────────────────

    @staticmethod
    async def get_registry_connector(connector_type: str) -> Optional[Dict[str, Any]]:
        """
        Fetch a single connector type definition from connector_registry.
        Returns full metadata including config_fields and credential_fields.
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT connector_type, display_name, description, icon,
                           sync_type, connector_group, display_order,
                           config_fields, credential_fields, schedule_options,
                           ingest_endpoint, is_active
                    FROM connector_registry
                    WHERE connector_type = :ctype
                    LIMIT 1
                """),
                {"ctype": connector_type}
            )
            row = result.mappings().first()
        return dict(row) if row else None

    @staticmethod
    async def list_registry_connectors(
        group: Optional[str] = None,
        active_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Fetch connector type definitions from connector_registry.
        Used to build the Add Connector catalog.
        """
        params: Dict[str, Any] = {}
        where_clauses = []
        if active_only:
            where_clauses.append("is_active = true")
        if group:
            where_clauses.append("connector_group = :group")
            params["group"] = group

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(f"""
                    SELECT connector_type, display_name, description, icon,
                           sync_type, connector_group, display_order,
                           config_fields, credential_fields, schedule_options,
                           ingest_endpoint, is_active
                    FROM connector_registry
                    {where_sql}
                    ORDER BY connector_group, display_order
                """),
                params
            )
            return [dict(r._mapping) for r in result.fetchall()]

    # ── Tenant connector instance methods ──────────────────────────────────────

    @staticmethod
    async def list_connectors(tenant_id: str) -> List[Dict[str, Any]]:
        """
        Return configured connector instances for this tenant,
        joined with registry metadata.
        Only returns connectors that have a tenant_connectors row.
        """
        tid = tenant_id.strip()

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT
                        tc.id,
                        tc.connector      AS connector_type,
                        tc.is_active      AS tenant_enabled,
                        tc.status,
                        tc.last_sync_at,
                        tc.last_error,
                        tc.sync_frequency,
                        tc.config,
                        r.display_name,
                        r.description,
                        r.sync_type,
                        r.connector_group,
                        r.display_order,
                        r.icon,
                        r.config_fields,
                        r.credential_fields
                    FROM tenant_connectors tc
                    JOIN connector_registry r ON r.connector_type = tc.connector
                    WHERE TRIM(tc.tenant_id) = :tid
                    ORDER BY r.connector_group, r.display_order
                """),
                {"tid": tid}
            )
            rows = [dict(r._mapping) for r in result.fetchall()]

        for row in rows:
            row["last_sync_at"] = row["last_sync_at"].isoformat() if row["last_sync_at"] else None

        return rows

    @classmethod
    async def list_connectors_grouped(cls, tenant_id: str) -> List[Dict[str, Any]]:
        """
        Return configured connector instances grouped for the tenant admin UI.
        Only shows connectors that have been configured (have a tenant_connectors row).
        eDiscovery group always appears (coming-soon state).
        """
        tid = tenant_id.strip()

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT
                        tc.id,
                        tc.connector      AS connector_type,
                        tc.is_active      AS tenant_enabled,
                        tc.status,
                        tc.last_sync_at,
                        tc.last_error,
                        tc.sync_frequency,
                        tc.config,
                        r.display_name,
                        r.description,
                        r.sync_type,
                        r.connector_group,
                        r.display_order,
                        r.icon,
                        r.config_fields,
                        r.credential_fields
                    FROM tenant_connectors tc
                    JOIN connector_registry r ON r.connector_type = tc.connector
                    WHERE TRIM(tc.tenant_id) = :tid
                    ORDER BY r.connector_group, r.display_order
                """),
                {"tid": tid}
            )
            rows = [dict(r._mapping) for r in result.fetchall()]

        for row in rows:
            row["sync_type_label"] = cls.SYNC_LABELS.get(row["sync_type"], row["sync_type"])
            row["last_sync_at"] = row["last_sync_at"].isoformat() if row["last_sync_at"] else None

        # Bucket by group
        buckets: Dict[str, List] = {}
        for row in rows:
            g = row.get("connector_group") or "data"
            buckets.setdefault(g, []).append(row)

        # eDiscovery always present
        buckets.setdefault("ediscovery", [])

        groups: List[Dict] = []
        seen: set = set()

        for key in cls.GROUP_ORDER:
            if key in buckets:
                meta = cls.GROUP_META.get(key, {"label": key.title(), "description": ""})
                groups.append({
                    "group_key":   key,
                    "label":       meta["label"],
                    "description": meta["description"],
                    "connectors":  buckets[key],
                })
                seen.add(key)

        for key, connectors in buckets.items():
            if key not in seen:
                meta = cls.GROUP_META.get(key, {"label": key.title(), "description": ""})
                groups.append({
                    "group_key":   key,
                    "label":       meta["label"],
                    "description": meta["description"],
                    "connectors":  connectors,
                })

        return groups

    @staticmethod
    async def get_connector(tenant_id: str, connector_type: str) -> Optional[Dict[str, Any]]:
        """
        Get a single configured connector instance joined with registry metadata.
        Returns None if not yet configured for this tenant.
        """
        tid = tenant_id.strip()

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT
                        tc.id,
                        tc.connector      AS connector_type,
                        tc.is_active      AS tenant_enabled,
                        tc.status,
                        tc.last_sync_at,
                        tc.last_error,
                        tc.sync_frequency,
                        tc.config,
                        r.display_name,
                        r.description,
                        r.sync_type,
                        r.connector_group,
                        r.config_fields,
                        r.credential_fields,
                        r.ingest_endpoint
                    FROM tenant_connectors tc
                    JOIN connector_registry r ON r.connector_type = tc.connector
                    WHERE TRIM(tc.tenant_id) = :tid AND tc.connector = :ctype
                    LIMIT 1
                """),
                {"tid": tid, "ctype": connector_type}
            )
            row = result.mappings().first()

        if not row:
            return None
        data = dict(row)
        data["last_sync_at"] = data["last_sync_at"].isoformat() if data["last_sync_at"] else None
        return data

    @staticmethod
    async def upsert_connector(
        tenant_id: str,
        connector_type: str,
        enabled: Optional[bool] = None,
        sync_frequency: Optional[str] = None,
        config: Optional[Dict] = None,
    ) -> str:
        """Create or update a tenant connector instance. Returns connector id."""
        tid = tenant_id.strip()

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT id FROM tenant_connectors WHERE TRIM(tenant_id) = :tid AND connector = :ctype"),
                {"tid": tid, "ctype": connector_type}
            )
            row = result.first()

            if row:
                cid = str(row[0])
                sets = ["updated_at = NOW()"]
                params: Dict[str, Any] = {"tid": tid, "ctype": connector_type}
                if enabled is not None:
                    sets.append("is_active = :enabled")
                    params["enabled"] = enabled
                if sync_frequency is not None:
                    sets.append("sync_frequency = :sync_frequency")
                    params["sync_frequency"] = sync_frequency
                if config is not None:
                    sets.append("config = CAST(:config AS jsonb)")
                    params["config"] = json.dumps(config)
                await session.execute(
                    text(f"UPDATE tenant_connectors SET {', '.join(sets)} WHERE TRIM(tenant_id) = :tid AND connector = :ctype"),
                    params
                )
            else:
                cid = str(uuid4())
                await session.execute(
                    text("""
                        INSERT INTO tenant_connectors
                            (id, tenant_id, connector, is_active, sync_frequency, config, status)
                        VALUES
                            (gen_random_uuid(), :tid, :ctype, :enabled, :freq, CAST(:config AS jsonb), 'unconfigured')
                    """),
                    {
                        "tid": tid,
                        "ctype": connector_type,
                        "enabled": enabled if enabled is not None else False,
                        "freq": sync_frequency or "hourly",
                        "config": json.dumps(config or {}),
                    }
                )
            await session.commit()
        return cid

    @staticmethod
    async def set_connector_status(
        tenant_id: str,
        connector_type: str,
        status: str,
        last_error: Optional[str] = None,
        update_last_sync: bool = False,
    ):
        """Update connector status after a sync run."""
        tid = tenant_id.strip()
        params: Dict[str, Any] = {"tid": tid, "ctype": connector_type, "status": status, "err": last_error}
        sets = ["status = :status", "last_error = :err"]
        if update_last_sync:
            sets.append("last_sync_at = NOW()")

        async with AsyncSessionLocal() as session:
            await session.execute(
                text(f"UPDATE tenant_connectors SET {', '.join(sets)} WHERE TRIM(tenant_id) = :tid AND connector = :ctype"),
                params
            )
            await session.commit()

    # ── Credential helpers ─────────────────────────────────────────────────────

    @staticmethod
    async def generate_ingest_api_key(tenant_id: str, provider: str) -> str:
        """Generate and store an ingest API key. Returns existing key if already set."""
        tid = tenant_id.strip()
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT encrypted_key FROM credentials_vault
                    WHERE TRIM(tenant_id) = :tid AND provider = :provider AND key_type = 'ingest_api_key'
                    LIMIT 1
                """),
                {"tid": tid, "provider": provider}
            )
            row = result.first()
            if row:
                return _vault_decrypt(row[0])

            api_key = secrets.token_urlsafe(32)
            encrypted = _vault_encrypt(api_key)
            await session.execute(
                text("""
                    INSERT INTO credentials_vault (id, tenant_id, provider, key_type, encrypted_key)
                    VALUES (gen_random_uuid(), :tid, :provider, 'ingest_api_key', :val)
                    ON CONFLICT (tenant_id, provider, key_type)
                    DO UPDATE SET encrypted_key = EXCLUDED.encrypted_key
                """),
                {"tid": tid, "provider": provider, "val": encrypted}
            )
            await session.commit()
            return api_key

    @staticmethod
    async def save_credential(tenant_id: str, provider: str, key_type: str, value: str):
        """Store a credential in credentials_vault, Fernet-encrypted."""
        tid = tenant_id.strip()
        encrypted = _vault_encrypt(value)
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO credentials_vault (id, tenant_id, provider, key_type, encrypted_key)
                    VALUES (gen_random_uuid(), :tid, :provider, :key_type, :val)
                    ON CONFLICT (tenant_id, provider, key_type)
                    DO UPDATE SET encrypted_key = EXCLUDED.encrypted_key
                """),
                {"tid": tid, "provider": provider, "key_type": key_type, "val": encrypted}
            )
            await session.commit()

    @staticmethod
    async def get_credential(tenant_id: str, provider: str, key_type: str) -> Optional[str]:
        """Retrieve a credential from credentials_vault. Decrypts via Fernet
        with plaintext fallback for legacy rows."""
        tid = tenant_id.strip()
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT encrypted_key FROM credentials_vault
                    WHERE TRIM(tenant_id) = :tid AND provider = :provider AND key_type = :key_type
                    LIMIT 1
                """),
                {"tid": tid, "provider": provider, "key_type": key_type}
            )
            row = result.first()
        if not row:
            return None
        return _vault_decrypt(row[0])

    # ── Sync log ───────────────────────────────────────────────────────────────

    @staticmethod
    async def start_sync_log(tenant_id: str, connector_type: str, triggered_by: str = "scheduler") -> str:
        tid = tenant_id.strip()
        log_id = str(uuid4())
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO connector_sync_log (id, tenant_id, connector_type, started_at, triggered_by)
                    VALUES (CAST(:id AS uuid), :tid, :ctype, NOW(), :triggered_by)
                """),
                {"id": log_id, "tid": tid, "ctype": connector_type, "triggered_by": triggered_by}
            )
            await session.commit()
        return log_id

    @staticmethod
    async def complete_sync_log(
        log_id: str,
        records_processed: int = 0,
        records_skipped: int = 0,
        error_count: int = 0,
        last_error: Optional[str] = None,
    ):
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    UPDATE connector_sync_log SET
                        completed_at = NOW(), records_processed = :rp,
                        records_skipped = :rs, error_count = :ec, last_error = :err
                    WHERE id = CAST(:id AS uuid)
                """),
                {"id": log_id, "rp": records_processed, "rs": records_skipped, "ec": error_count, "err": last_error}
            )
            await session.commit()

    @staticmethod
    async def get_sync_history(tenant_id: str, connector_type: str, limit: int = 50) -> List[Dict]:
        tid = tenant_id.strip()
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT id, started_at, completed_at, records_processed,
                           records_skipped, error_count, last_error, triggered_by
                    FROM connector_sync_log
                    WHERE TRIM(tenant_id) = :tid AND connector_type = :ctype
                    ORDER BY started_at DESC LIMIT :lim
                """),
                {"tid": tid, "ctype": connector_type, "lim": limit}
            )
            rows = result.mappings().all()
        return [
            {
                "id":                str(r["id"]),
                "started_at":        r["started_at"].isoformat() if r["started_at"] else None,
                "completed_at":      r["completed_at"].isoformat() if r["completed_at"] else None,
                "records_processed": r["records_processed"],
                "records_skipped":   r["records_skipped"],
                "error_count":       r["error_count"],
                "last_error":        r["last_error"],
                "triggered_by":      r["triggered_by"],
                "duration_seconds":  (
                    int((r["completed_at"] - r["started_at"]).total_seconds())
                    if r["completed_at"] and r["started_at"] else None
                ),
            }
            for r in rows
        ]

    # ── CSV import log ─────────────────────────────────────────────────────────

    @staticmethod
    async def log_csv_import(
        tenant_id: str, connector_type: str, filename: str, row_count: int,
        imported_by: int, status: str = "complete", error_detail: Optional[str] = None,
    ) -> str:
        tid = tenant_id.strip()
        import_id = str(uuid4())
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO connector_csv_imports
                        (id, tenant_id, connector_type, filename, row_count, imported_by, status, error_detail)
                    VALUES (CAST(:id AS uuid), :tid, :ctype, :fn, :rc, :uid, :status, :err)
                """),
                {"id": import_id, "tid": tid, "ctype": connector_type, "fn": filename,
                 "rc": row_count, "uid": imported_by, "status": status, "err": error_detail}
            )
            await session.commit()
        return import_id

    @staticmethod
    async def get_csv_import_history(tenant_id: str, connector_type: str, limit: int = 20) -> List[Dict]:
        tid = tenant_id.strip()
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT id, filename, row_count, imported_at, imported_by, status, error_detail
                    FROM connector_csv_imports
                    WHERE TRIM(tenant_id) = :tid AND connector_type = :ctype
                    ORDER BY imported_at DESC LIMIT :lim
                """),
                {"tid": tid, "ctype": connector_type, "lim": limit}
            )
            rows = result.mappings().all()
        return [
            {
                "id":           str(r["id"]),
                "filename":     r["filename"],
                "row_count":    r["row_count"],
                "imported_at":  r["imported_at"].isoformat() if r["imported_at"] else None,
                "imported_by":  r["imported_by"],
                "status":       r["status"],
                "error_detail": r["error_detail"],
            }
            for r in rows
        ]

    # ── Health summary ─────────────────────────────────────────────────────────

    @staticmethod
    async def get_health_summary(tenant_id: str) -> Dict[str, Any]:
        connectors = await ConnectorService.list_connectors(tenant_id)
        enabled = [c for c in connectors if c["tenant_enabled"]]
        healthy = [c for c in enabled if c["status"] == ConnectorStatus.HEALTHY]
        errored = [c for c in enabled if c["status"] == ConnectorStatus.ERROR]
        return {
            "total":   len(connectors),
            "enabled": len(enabled),
            "healthy": len(healthy),
            "errored": len(errored),
            "status": (
                ConnectorStatus.HEALTHY    if len(errored) == 0 and len(enabled) > 0
                else ConnectorStatus.ERROR if len(errored) > 0
                else ConnectorStatus.UNCONFIGURED
            ),
            "connectors": [
                {"type": c["connector_type"], "label": c["display_name"], "status": c["status"]}
                for c in enabled
            ],
        }
