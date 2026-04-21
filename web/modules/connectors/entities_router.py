"""
modules/connectors/entities_router.py

Connector entity discovery and user mapping routes.
Mounted at /tenant-admin/connectors/{type}/entities

PATTERN — works for any connector that has enumerable external entities:
  Exchange    → mailboxes       → maps to Praesidium users
  Office 365  → mailboxes/users → maps to Praesidium users
  FreePBX     → extensions      → maps to Praesidium users
  ManicTime   → workstations    → maps to Praesidium users
  Clio        → timekeepers     → maps to Praesidium users

Discovery jobs are connector-specific (implemented per connector).
The UI, data model, and mapping flow are generic.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.connectors.service import ConnectorService
from modules.dashboard.services.nav_context import get_nav_context
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)


def _templates(request):
    from app import templates
    return templates


router = APIRouter()


# ── Entity map page ────────────────────────────────────────────────────────────

@router.get("/tenant-admin/connectors/{connector_type}/entities", response_class=HTMLResponse)
async def connector_entities(
    request: Request,
    connector_type: str,
    user=Depends(get_current_user),
):
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()

    registry = await ConnectorService.get_registry_connector(connector_type)
    if not registry:
        raise HTTPException(status_code=404, detail=f"Unknown connector type: {connector_type}")

    # Discovered entities for this connector
    async with AsyncSessionLocal() as session:
        entities_result = await session.execute(
            text("""
                SELECT
                    cem.id,
                    cem.entity_id,
                    cem.entity_display,
                    cem.entity_email,
                    cem.entity_meta,
                    cem.mapped_user_id,
                    cem.is_active,
                    cem.auto_matched,
                    cem.discovered_at,
                    cem.mapped_at,
                    u.full_name  AS mapped_user_name,
                    u.email      AS mapped_user_email
                FROM connector_entity_map cem
                LEFT JOIN users u ON u.id = cem.mapped_user_id
                WHERE TRIM(cem.tenant_id) = :tid
                  AND cem.connector_type = :ctype
                ORDER BY cem.entity_display
            """),
            {"tid": tenant_id, "ctype": connector_type}
        )
        entities = [dict(r._mapping) for r in entities_result.fetchall()]

        # All platform users for the mapping dropdown
        users_result = await session.execute(
            text("""
                SELECT id, full_name, email
                FROM users
                WHERE TRIM(tenant_id) = :tid
                  AND is_active = true
                ORDER BY full_name
            """),
            {"tid": tenant_id}
        )
        platform_users = [dict(r._mapping) for r in users_result.fetchall()]

        # Last discovery run
        sync_result = await session.execute(
            text("""
                SELECT started_at, completed_at, discovered_count,
                       new_count, auto_matched_count, error
                FROM connector_entity_sync_log
                WHERE TRIM(tenant_id) = :tid AND connector_type = :ctype
                ORDER BY started_at DESC
                LIMIT 1
            """),
            {"tid": tenant_id, "ctype": connector_type}
        )
        last_sync = sync_result.mappings().first()

    # Summary counts
    total      = len(entities)
    mapped     = sum(1 for e in entities if e["mapped_user_id"])
    unmapped   = total - mapped
    auto       = sum(1 for e in entities if e["auto_matched"])

    nav = await get_nav_context(request, page="connectors")
    return _templates(request).TemplateResponse(request, "connectors/entities.html", {
        "registry":       registry,
        "connector_type": connector_type,
        "entities":       entities,
        "platform_users": platform_users,
        "last_sync":      dict(last_sync) if last_sync else None,
        "summary": {
            "total":    total,
            "mapped":   mapped,
            "unmapped": unmapped,
            "auto":     auto,
        },
        "branding":  getattr(request.state, "branding", None),
        "user":      user,
        **nav,
    })


# ── Trigger discovery ──────────────────────────────────────────────────────────

@router.post("/tenant-admin/connectors/{connector_type}/entities/discover")
async def connector_entities_discover(
    request: Request,
    connector_type: str,
    user=Depends(get_current_user),
):
    """
    Trigger entity discovery for this connector.
    Enqueues a background job — connector-specific discovery logic
    runs on PROC-01 and populates connector_entity_map.

    Discovery jobs per connector (implement as needed):
      exchange    → jobs.discover_exchange_mailboxes
      office365   → jobs.discover_o365_users
      pbx_cdr     → jobs.discover_pbx_extensions
      manictime   → jobs.discover_manictime_users
    """
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", 0)

    registry = await ConnectorService.get_registry_connector(connector_type)
    if not registry:
        raise HTTPException(status_code=404, detail=f"Unknown connector type: {connector_type}")

    # Log discovery attempt
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO connector_entity_sync_log
                    (tenant_id, connector_type, triggered_by)
                VALUES (:tid, :ctype, :uid)
            """),
            {"tid": tenant_id, "ctype": connector_type, "uid": user_id}
        )
        await session.commit()

    # Connector-specific discovery job map
    DISCOVERY_JOBS = {
        "exchange":  "jobs.discover_exchange_mailboxes.run",
        "office365": "jobs.discover_o365_users.run",
        "pbx_cdr":   "jobs.discover_pbx_extensions.run",
        "manictime": "jobs.discover_manictime_users.run",
    }

    job_func = DISCOVERY_JOBS.get(connector_type)
    if job_func:
        try:
            import os
            from redis import Redis
            from rq import Queue
            REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
            q = Queue(connection=Redis.from_url(REDIS_URL))
            job = q.enqueue(job_func, tenant_id, user_id, job_timeout=300)
            logger.info(
                f"[entities/discover] connector={connector_type} "
                f"tenant={tenant_id} job_id={job.id}"
            )
        except Exception as e:
            logger.exception(f"[entities/discover] enqueue failed: {e}")
    else:
        logger.info(f"[entities/discover] connector={connector_type} — no discovery job registered")

    return RedirectResponse(
        f"/tenant-admin/connectors/{connector_type}/entities?discovering=1",
        status_code=303
    )


# ── Save entity→user mapping ───────────────────────────────────────────────────

@router.post("/tenant-admin/connectors/{connector_type}/entities/map")
async def connector_entities_map(
    request: Request,
    connector_type: str,
    user=Depends(get_current_user),
):
    """
    Save admin-assigned entity→user mappings.
    Form posts entity_id → user_id pairs.
    Clears mapping if user_id is empty.
    """
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", 0)

    form = await request.form()

    # Form fields are named: map_{entity_id} = user_id
    mappings = {
        k[4:]: v.strip()
        for k, v in form.items()
        if k.startswith("map_")
    }

    async with AsyncSessionLocal() as session:
        for entity_id, mapped_user_id in mappings.items():
            if mapped_user_id:
                await session.execute(
                    text("""
                        UPDATE connector_entity_map
                        SET mapped_user_id = :uid,
                            mapped_at      = NOW(),
                            mapped_by      = :admin_id,
                            auto_matched   = false
                        WHERE TRIM(tenant_id) = :tid
                          AND connector_type  = :ctype
                          AND entity_id       = :eid
                    """),
                    {
                        "tid":      tenant_id,
                        "ctype":    connector_type,
                        "eid":      entity_id,
                        "uid":      int(mapped_user_id),
                        "admin_id": user_id,
                    }
                )
            else:
                # Clear mapping
                await session.execute(
                    text("""
                        UPDATE connector_entity_map
                        SET mapped_user_id = NULL,
                            mapped_at      = NULL,
                            mapped_by      = NULL,
                            auto_matched   = false
                        WHERE TRIM(tenant_id) = :tid
                          AND connector_type  = :ctype
                          AND entity_id       = :eid
                    """),
                    {"tid": tenant_id, "ctype": connector_type, "eid": entity_id}
                )
        await session.commit()

    return RedirectResponse(
        f"/tenant-admin/connectors/{connector_type}/entities?saved=1",
        status_code=303
    )


# ── Save entity type classifications ──────────────────────────────────────────

@router.post("/tenant-admin/connectors/{connector_type}/entities/type")
async def connector_entities_set_type(
    request: Request,
    connector_type: str,
    user=Depends(get_current_user),
):
    """
    Save entity type classifications (mailbox / shared_mailbox / calendar).
    Form fields named: type_{entity_id} = mailbox|shared_mailbox|calendar
    """
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()

    form = await request.form()
    type_map = {
        k[5:]: v.strip()
        for k, v in form.items()
        if k.startswith("type_")
    }

    VALID_TYPES = {"mailbox", "shared_mailbox", "calendar"}

    async with AsyncSessionLocal() as session:
        for entity_id, entity_type in type_map.items():
            if entity_type not in VALID_TYPES:
                continue
            await session.execute(
                text("""
                    UPDATE connector_entity_map
                    SET entity_type = :etype
                    WHERE TRIM(tenant_id) = :tid
                      AND connector_type  = :ctype
                      AND entity_id       = :eid
                """),
                {
                    "tid":   tenant_id,
                    "ctype": connector_type,
                    "eid":   entity_id,
                    "etype": entity_type,
                }
            )
        await session.commit()

    return RedirectResponse(
        f"/tenant-admin/connectors/{connector_type}/entities?types_saved=1",
        status_code=303
    )
