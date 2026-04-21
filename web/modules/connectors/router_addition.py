
# ── API: Windows file agent ingest endpoint ───────────────────────────────────
# Append this block to the END of modules/connectors/router.py
# (after the timeslips_configure_post route)

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

    Accepts a batch of indexed file records and:
      - Upserts into dms_documents
      - Routes OCR-flagged files to dms_ocr_queue
      - Records production-excluded paths to dms_excluded_paths
      - Pushes extracted text to Elasticsearch
      - Logs run to connector_sync_log

    The agent never sends production (Bates-stamped) files — those are
    excluded at the agent layer and recorded in excluded_paths for
    eDiscovery routing via Module 5.
    """
    api_key   = request.headers.get("X-Connector-Key", "").strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="X-Connector-Key header required")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    tenant_id = (body.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id required in payload")

    # Validate key against credentials_vault
    valid = await validate_connector_key(tenant_id, api_key)
    if not valid:
        logger.warning(f"[files/ingest] Invalid connector key for tenant {tenant_id}")
        raise HTTPException(status_code=401, detail="Invalid connector key")

    # Validate payload has files array
    files = body.get("files", [])
    if not isinstance(files, list):
        raise HTTPException(status_code=400, detail="files must be an array")

    # Cap batch size — agent sends 250 per batch, hard cap at 1000 for safety
    if len(files) > 1000:
        raise HTTPException(
            status_code=400,
            detail=f"Batch too large: {len(files)} files. Maximum 1000 per request."
        )

    # Register unique folder roots as connector sources
    agent_version = body.get("agent_version", "unknown")
    folder_roots  = {f.get("folder_root") for f in files if f.get("folder_root")}
    for fr in folder_roots:
        await ensure_connector_source(tenant_id, fr, agent_version)

    # Process the batch
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
    """
    Returns DMS indexing status for the tenant.
    Used by the connector admin UI to show progress.
    """
    tenant_id = (getattr(user, "tenant_id", None) or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=401, detail="No tenant context")

    try:
        async with AsyncSessionLocal() as session:
            # Document counts by ocr_status
            docs_result = await session.execute(
                text("""
                    SELECT ocr_status, COUNT(*) as n
                    FROM dms_documents
                    WHERE TRIM(tenant_id) = :tid
                    GROUP BY ocr_status
                """),
                {"tid": tenant_id}
            )
            docs_by_status = {r[0]: r[1] for r in docs_result}

            # OCR queue counts
            ocr_result = await session.execute(
                text("""
                    SELECT status, COUNT(*) as n
                    FROM dms_ocr_queue
                    WHERE TRIM(tenant_id) = :tid
                    GROUP BY status
                """),
                {"tid": tenant_id}
            )
            ocr_by_status = {r[0]: r[1] for r in ocr_result}

            # Excluded paths count
            excl_result = await session.execute(
                text("SELECT COUNT(*) FROM dms_excluded_paths WHERE TRIM(tenant_id) = :tid"),
                {"tid": tenant_id}
            )
            excluded_count = excl_result.scalar()

            # Last sync
            sync_result = await session.execute(
                text("""
                    SELECT completed_at, records_processed, records_skipped, error_count
                    FROM connector_sync_log
                    WHERE TRIM(tenant_id) = :tid AND connector_type = 'windows_agent'
                    ORDER BY started_at DESC
                    LIMIT 1
                """),
                {"tid": tenant_id}
            )
            last_sync = sync_result.first()

        return JSONResponse({
            "documents":           docs_by_status,
            "ocr_queue":           ocr_by_status,
            "excluded_paths":      excluded_count,
            "total_documents":     sum(docs_by_status.values()),
            "last_sync": {
                "completed_at":    last_sync[0].isoformat() if last_sync and last_sync[0] else None,
                "records_processed": last_sync[1] if last_sync else 0,
                "records_skipped":   last_sync[2] if last_sync else 0,
                "error_count":       last_sync[3] if last_sync else 0,
            } if last_sync else None,
        })
    except Exception as exc:
        logger.exception(f"[files/status] Error for tenant {tenant_id}: {exc}")
        raise HTTPException(status_code=500, detail="Status query error")
