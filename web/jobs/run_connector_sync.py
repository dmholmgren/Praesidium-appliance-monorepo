"""
jobs/run_connector_sync.py

Central RQ job dispatcher for all connector sync jobs.
Called by POST /tenant-admin/connectors/{type}/trigger.

Add new connectors here as they are built.
"""
import logging

logger = logging.getLogger(__name__)

# Map connector_type → sync job module.function
SYNC_JOBS = {
    "exchange":    "jobs.exchange_sync.run",
    "manictime":   "jobs.manictime_sync.run",   # future
    "pbx_cdr":     "jobs.pbx_sync.run",          # future
    "courtlistener": "jobs.courtlistener_sync.run",  # future
}


def run_connector_sync(tenant_id: str, connector_type: str, trigger_type: str = "manual"):
    """
    Dispatcher — imports and calls the correct sync job for connector_type.
    Runs on PROC-01 via RQ.
    """
    logger.info("[run_connector_sync] tenant=%s connector=%s trigger=%s",
                tenant_id, connector_type, trigger_type)

    job_path = SYNC_JOBS.get(connector_type)
    if not job_path:
        logger.warning("[run_connector_sync] no sync job for connector_type=%s", connector_type)
        return

    try:
        module_path, fn_name = job_path.rsplit(".", 1)
        import importlib
        mod = importlib.import_module(module_path)
        fn  = getattr(mod, fn_name)
        fn(tenant_id)
        logger.info("[run_connector_sync] complete connector=%s tenant=%s",
                    connector_type, tenant_id)
    except Exception as exc:
        logger.exception("[run_connector_sync] failed connector=%s: %s",
                         connector_type, exc)
        raise
