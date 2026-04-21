# ── PATCH: Replace the timeslips/ingest stub in router.py ────────────────────
#
# Find this block in modules/connectors/router.py (~line 220):
#
#   @router.post("/api/connectors/timeslips/ingest")
#   async def timeslips_ingest(request: Request):
#       ...
#       # Stub response — M10 C2 implements actual processing
#       return JSONResponse({
#           "accepted": record_count,
#           "processed": 0,
#           "message": "Timeslips ingest endpoint ready — full processing implemented in M10 C2"
#       })
#
# Replace the ENTIRE function with the version below.
# Also add the import at the top of router.py with the other imports:
#
#   from modules.connectors.timeslips_ingest_service import (
#       validate_timeslips_key,
#       process_timeslips_ingest,
#   )
#
# ─────────────────────────────────────────────────────────────────────────────


# ADD THIS IMPORT near the top of router.py (with other imports):
# from modules.connectors.timeslips_ingest_service import validate_timeslips_key, process_timeslips_ingest


# REPLACE the existing timeslips_ingest function with this:

@router.post("/api/connectors/timeslips/ingest")
async def timeslips_ingest(request: Request):
    """
    Called by the Timeslips Windows agent (ts_sync_agent.py v2.0).
    Authenticated by X-Connector-Key header.
    Processes batched slip/client/invoice/payment payloads.
    Writes to ts_slips, ts_clients, ts_timekeepers, ts_invoices, ts_payments.
    Logs to billing_import_log.
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

    # Validate key
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
        "status":         "ok",
        "accepted":       len(slips),
        "slips_new":      result["slips_new"],
        "slips_deduped":  result["slips_deduped"],
        "slips_errors":   result["slips_errors"],
        "clients":        result["clients"],
        "timekeepers":    result["timekeepers"],
        "invoices":       result["invoices"],
        "payments":       result["payments"],
        "batch_index":    result["batch_index"],
        "batch_count":    result["batch_count"],
    }, status_code=202)
