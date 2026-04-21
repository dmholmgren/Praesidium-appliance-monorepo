"""
modules/connectors/router.py

Connector admin UI routes and API endpoints.
All routes under /tenant-admin/connectors/* and /api/connectors/*.
Tenant session required — resolves from session token, not subdomain.

ARCHITECTURE:
- GET  /tenant-admin/connectors              — configured instances, grouped
- GET  /tenant-admin/connectors/catalog      — available types not yet configured
- GET  /tenant-admin/connectors/{type}/configure  — configure form (generic, DB-driven)
- POST /tenant-admin/connectors/{type}/configure  — save config + credentials
- POST /tenant-admin/connectors/{type}/toggle     — enable/disable
- POST /tenant-admin/connectors/{type}/trigger    — manual sync
- POST /tenant-admin/connectors/{type}/import     — CSV upload

All configure forms are driven by connector_registry.config_fields and
credential_fields JSONB. No connector-specific routes.
"""
import json
import logging
import os
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from redis import Redis
from rq import Queue

from modules.connectors.service import ConnectorService
from sqlalchemy import text
from core.db.base import AsyncSessionLocal
from modules.dashboard.services.nav_context import get_nav_context
from modules.connectors.base import CONNECTOR_TYPES, get_connector_meta, ConnectorStatus
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)


def _templates(request):
    from app import templates
    return templates


router = APIRouter()

REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")


# ── Connector list — configured instances only ─────────────────────────────────

@router.get("/tenant-admin/connectors", response_class=HTMLResponse)
async def connector_list(request: Request, user=Depends(get_current_user)):
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=401, detail="No tenant context")

    connector_groups = await ConnectorService.list_connectors_grouped(tenant_id)
    branding = getattr(request.state, "branding", None)
    nav = await get_nav_context(request, page="connectors")

    return _templates(request).TemplateResponse(request, "connectors/list.html", {
        "connector_groups": connector_groups,
        "branding": branding,
        "user": user,
        **nav,
    })


# ── Connector catalog — available types not yet configured ─────────────────────

@router.get("/tenant-admin/connectors/catalog", response_class=HTMLResponse)
async def connector_catalog(request: Request, user=Depends(get_current_user)):
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=401, detail="No tenant context")

    # All active registry types
    all_types = await ConnectorService.list_registry_connectors(active_only=True)

    # Already configured for this tenant
    configured = await ConnectorService.list_connectors(tenant_id)
    configured_types = {c["connector_type"] for c in configured}

    # Available = in registry but not yet configured, excluding stubs
    stub_types = {"auth_local", "auth_ldap", "auth_azure", "auth_okta", "ediscovery_client_pull"}
    available = [
        t for t in all_types
        if t["connector_type"] not in configured_types
        and t["connector_type"] not in stub_types
    ]

    # Group available types using same GROUP_META
    buckets: dict = {}
    for t in available:
        g = t.get("connector_group") or "data"
        buckets.setdefault(g, []).append(t)

    groups = []
    for key in ConnectorService.GROUP_ORDER:
        if key in buckets:
            meta = ConnectorService.GROUP_META.get(key, {"label": key.title(), "description": ""})
            # Resolve sync label
            for t in buckets[key]:
                t["sync_type_label"] = ConnectorService.SYNC_LABELS.get(t["sync_type"], t["sync_type"])
            groups.append({
                "group_key":   key,
                "label":       meta["label"],
                "description": meta["description"],
                "connectors":  buckets[key],
            })

    branding = getattr(request.state, "branding", None)
    nav = await get_nav_context(request, page="connectors")

    return _templates(request).TemplateResponse(request, "connectors/catalog.html", {
        "catalog_groups": groups,
        "branding": branding,
        "user": user,
        **nav,
    })


# ── Configure GET — generic, DB-driven form ────────────────────────────────────
# Must be declared before the toggle/trigger/import POST routes to avoid
# FastAPI matching "configure" as a connector_type.

@router.get("/tenant-admin/connectors/{connector_type}/configure", response_class=HTMLResponse)
async def connector_configure_get(
    request: Request,
    connector_type: str,
    user=Depends(get_current_user),
):
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()

    # Get registry definition — this is the source of truth for the form
    registry = await ConnectorService.get_registry_connector(connector_type)
    if not registry:
        raise HTTPException(status_code=404, detail=f"Unknown connector type: {connector_type}")

    # Get existing tenant instance if configured
    instance = await ConnectorService.get_connector(tenant_id, connector_type)
    existing_config = instance.get("config", {}) if instance else {}

    # Load existing credential hints (masked) from credentials_vault
    credential_hints: dict = {}
    if registry.get("credential_fields"):
        for field in registry["credential_fields"]:
            existing = await ConnectorService.get_credential(
                tenant_id, connector_type, field["name"]
            )
            if existing:
                credential_hints[field["name"]] = True  # signal to template: value exists

    # For agent-push connectors, surface the ingest API key
    ingest_api_key = None
    if registry.get("sync_type") == "agent_push":
        ingest_api_key = await ConnectorService.get_credential(
            tenant_id, connector_type, "ingest_api_key"
        )

    base_url = f"{request.url.scheme}://{request.url.hostname}"
    ingest_url = f"{base_url}/api/connectors/{connector_type}/ingest" if ingest_api_key else None

    nav = await get_nav_context(request, page="connectors")
    return _templates(request).TemplateResponse(request, "connectors/configure.html", {
        "registry":         registry,
        "instance":         instance,
        "existing_config":  existing_config,
        "credential_hints": credential_hints,
        "ingest_api_key":   ingest_api_key,
        "ingest_url":       ingest_url,
        "is_active":        instance.get("tenant_enabled", False) if instance else False,
        "last_sync_at":     instance.get("last_sync_at") if instance else None,
        "saved":            request.query_params.get("saved"),
        "error":            request.query_params.get("error"),
        "branding":         getattr(request.state, "branding", None),
        "user":             user,
        **nav,
    })


# ── Configure POST — generic save ──────────────────────────────────────────────

@router.post("/tenant-admin/connectors/{connector_type}/configure")
async def connector_configure_post(
    request: Request,
    connector_type: str,
    user=Depends(get_current_user),
):
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()

    registry = await ConnectorService.get_registry_connector(connector_type)
    if not registry:
        raise HTTPException(status_code=404, detail=f"Unknown connector type: {connector_type}")

    form = await request.form()

    # ── Extract config fields (non-secret) ────────────────────────────────────
    config: dict = {}
    for field in (registry.get("config_fields") or []):
        name = field["name"]
        val = form.get(name, "").strip()
        if val:
            config[name] = val

    # ── Extract and store credential fields ───────────────────────────────────
    for field in (registry.get("credential_fields") or []):
        name = field["name"]
        val = form.get(name, "").strip()
        if val:
            await ConnectorService.save_credential(tenant_id, connector_type, name, val)

    # ── Auto-generate ingest API key for agent-push connectors ────────────────
    if registry.get("sync_type") == "agent_push":
        await ConnectorService.generate_ingest_api_key(tenant_id, connector_type)

    # ── Enable toggle ─────────────────────────────────────────────────────────
    is_active = form.get("is_active") == "on"

    # ── Sync frequency ────────────────────────────────────────────────────────
    sync_frequency = form.get("sync_frequency", "hourly").strip() or "hourly"

    try:
        await ConnectorService.upsert_connector(
            tenant_id, connector_type,
            enabled=is_active,
            sync_frequency=sync_frequency,
            config=config,
        )
    except Exception as e:
        logger.exception(f"[configure_post] Error saving {connector_type} for {tenant_id}: {e}")
        return RedirectResponse(
            f"/tenant-admin/connectors/{connector_type}/configure?error={str(e)[:80]}",
            status_code=303
        )

    return RedirectResponse(
        f"/tenant-admin/connectors/{connector_type}/configure?saved=1",
        status_code=303
    )


# ── Enable / disable toggle ────────────────────────────────────────────────────

@router.post("/tenant-admin/connectors/{connector_type}/toggle")
async def connector_toggle(request: Request, connector_type: str, user=Depends(get_current_user)):
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()
    instance = await ConnectorService.get_connector(tenant_id, connector_type)
    current_enabled = instance.get("tenant_enabled", False) if instance else False
    await ConnectorService.upsert_connector(tenant_id, connector_type, enabled=not current_enabled)
    return RedirectResponse("/tenant-admin/connectors", status_code=303)


# ── Manual sync trigger ────────────────────────────────────────────────────────

@router.post("/tenant-admin/connectors/{connector_type}/trigger")
async def connector_trigger(request: Request, connector_type: str, user=Depends(get_current_user)):
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()

    registry = await ConnectorService.get_registry_connector(connector_type)
    if not registry:
        raise HTTPException(status_code=404, detail="Unknown connector type")
    if registry.get("sync_type") == "csv_import":
        raise HTTPException(status_code=400, detail="CSV connectors use the import endpoint")

    try:
        redis = Redis.from_url(REDIS_URL)
        q = Queue(connection=redis)
        job = q.enqueue(
            "jobs.run_connector_sync.run_connector_sync",
            tenant_id, connector_type, "manual",
            job_timeout=3600,
        )
        return JSONResponse({"status": "queued", "job_id": job.id})
    except Exception as exc:
        logger.exception(f"Failed to enqueue connector sync: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ── CSV import upload ──────────────────────────────────────────────────────────

@router.post("/tenant-admin/connectors/{connector_type}/import")
async def connector_csv_import(
    request: Request,
    connector_type: str,
    user=Depends(get_current_user),
    file: UploadFile = File(...),
):
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()

    registry = await ConnectorService.get_registry_connector(connector_type)
    if not registry or registry.get("sync_type") != "csv_import":
        raise HTTPException(status_code=400, detail="This connector does not support CSV import")
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted")

    content = await file.read()
    lines = content.decode("utf-8", errors="replace").splitlines()
    row_count = max(0, len(lines) - 1)

    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", prefix=f"{connector_type}_")
    tmp.write(content)
    tmp.close()

    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", 0)
    import_id = await ConnectorService.log_csv_import(
        tenant_id, connector_type, file.filename, row_count,
        imported_by=user_id, status="pending"
    )

    try:
        redis = Redis.from_url(REDIS_URL)
        q = Queue(connection=redis)
        q.enqueue(
            "jobs.run_connector_sync.process_csv_import",
            tenant_id, connector_type, tmp.name, file.filename, import_id,
            job_timeout=1800,
        )
    except Exception as exc:
        logger.warning(f"Could not enqueue CSV processing: {exc}")

    return RedirectResponse(
        f"/tenant-admin/connectors/{connector_type}/configure?imported={row_count}",
        status_code=303
    )


# ── API: connector health (for health dashboard) ──────────────────────────────

@router.get("/api/connectors/health")
async def connector_health_api(request: Request, user=Depends(get_current_user)):
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()
    summary = await ConnectorService.get_health_summary(tenant_id)
    return JSONResponse(summary)


# ── API: Timeslips ingest (called by Windows agent) ───────────────────────────

@router.post("/api/connectors/timeslips/ingest")
async def timeslips_ingest(request: Request):
    """
    Called by the Timeslips Windows agent (ts_sync_agent.py v2.0).
    Authenticated by X-Connector-Key header.
    """
    from modules.connectors.timeslips_ingest_service import (
        validate_timeslips_key,
        process_timeslips_ingest,
    )

    api_key = request.headers.get("X-Connector-Key", "").strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="X-Connector-Key required")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    tenant_id = (body.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id required")

    valid = await validate_timeslips_key(tenant_id, api_key)
    if not valid:
        logger.warning(f"[timeslips/ingest] Invalid key for tenant {tenant_id}")
        raise HTTPException(status_code=401, detail="Invalid connector key")

    slips = body.get("slips", [])
    logger.info(
        f"[timeslips/ingest] tenant={tenant_id} "
        f"batch={body.get('batch_index', 0)+1}/{body.get('batch_count', 1)} "
        f"slips={len(slips)}"
    )

    try:
        result = await process_timeslips_ingest(tenant_id, body)
    except Exception as exc:
        logger.exception(f"[timeslips/ingest] Processing error: {exc}")
        raise HTTPException(status_code=500, detail=f"Ingest error: {str(exc)[:200]}")

    return JSONResponse({
        "status":        "ok",
        "accepted":      len(slips),
        "slips_new":     result["slips_new"],
        "slips_deduped": result["slips_deduped"],
        "slips_errors":  result["slips_errors"],
        "clients":       result["clients"],
        "timekeepers":   result["timekeepers"],
        "invoices":      result["invoices"],
        "payments":      result["payments"],
        "batch_index":   result["batch_index"],
        "batch_count":   result["batch_count"],
    }, status_code=202)


# ── API: Windows file agent ingest ────────────────────────────────────────────

from modules.connectors.file_ingest_service import (
    validate_connector_key,
    process_file_batch,
    ensure_connector_source,
)


@router.post("/api/connectors/files/ingest")
async def files_ingest(request: Request):
    """
    Called by praesidium_agent.pyw (Windows file sync agent).
    Authenticated by X-Connector-Key header matching credentials_vault.
    """
    api_key = request.headers.get("X-Connector-Key", "").strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="X-Connector-Key header required")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    tenant_id = (body.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id required in payload")

    valid = await validate_connector_key(tenant_id, api_key)
    if not valid:
        logger.warning(f"[files/ingest] Invalid connector key for tenant {tenant_id}")
        raise HTTPException(status_code=401, detail="Invalid connector key")

    files = body.get("files", [])
    if not isinstance(files, list):
        raise HTTPException(status_code=400, detail="files must be an array")
    if len(files) > 1000:
        raise HTTPException(status_code=400, detail=f"Batch too large: {len(files)}. Max 1000.")

    agent_version = body.get("agent_version", "unknown")
    folder_roots = {f.get("folder_root") for f in files if f.get("folder_root")}
    for fr in folder_roots:
        await ensure_connector_source(tenant_id, fr, agent_version)

    try:
        result = await process_file_batch(tenant_id, body)
    except Exception as exc:
        logger.exception(f"[files/ingest] Processing error for tenant {tenant_id}: {exc}")
        raise HTTPException(status_code=500, detail="Ingest processing error")

    return JSONResponse({
        "status":            "ok",
        "accepted":          result["accepted"],
        "queued_for_ocr":    result["queued_for_ocr"],
        "excluded_recorded": result["excluded_recorded"],
        "errors":            result["errors"],
        "skipped":           result["skipped"],
        "tenant_id":         tenant_id,
        "agent_version":     agent_version,
    }, status_code=202)


@router.get("/api/connectors/files/status")
async def files_ingest_status(request: Request, user=Depends(get_current_user)):
    """Returns DMS indexing status for the tenant."""
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=401, detail="No tenant context")

    try:
        async with AsyncSessionLocal() as session:
            docs_result = await session.execute(
                text("SELECT ocr_status, COUNT(*) FROM dms_documents WHERE TRIM(tenant_id) = :tid GROUP BY ocr_status"),
                {"tid": tenant_id}
            )
            docs_by_status = {r[0]: r[1] for r in docs_result}

            ocr_result = await session.execute(
                text("SELECT status, COUNT(*) FROM dms_ocr_queue WHERE TRIM(tenant_id) = :tid GROUP BY status"),
                {"tid": tenant_id}
            )
            ocr_by_status = {r[0]: r[1] for r in ocr_result}

            excl_result = await session.execute(
                text("SELECT COUNT(*) FROM dms_excluded_paths WHERE TRIM(tenant_id) = :tid"),
                {"tid": tenant_id}
            )
            excluded_count = excl_result.scalar()

            sync_result = await session.execute(
                text("""
                    SELECT completed_at, records_processed, records_skipped, error_count
                    FROM connector_sync_log
                    WHERE TRIM(tenant_id) = :tid AND connector_type = 'windows_agent'
                    ORDER BY started_at DESC LIMIT 1
                """),
                {"tid": tenant_id}
            )
            last_sync = sync_result.first()

        return JSONResponse({
            "documents":       docs_by_status,
            "ocr_queue":       ocr_by_status,
            "excluded_paths":  excluded_count,
            "total_documents": sum(docs_by_status.values()),
            "last_sync": {
                "completed_at":      last_sync[0].isoformat() if last_sync and last_sync[0] else None,
                "records_processed": last_sync[1] if last_sync else 0,
                "records_skipped":   last_sync[2] if last_sync else 0,
                "error_count":       last_sync[3] if last_sync else 0,
            } if last_sync else None,
        })
    except Exception as exc:
        logger.exception(f"[files/status] Error for tenant {tenant_id}: {exc}")
        raise HTTPException(status_code=500, detail="Status query error")
