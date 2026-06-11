"""
jobs/run_connector_sync.py

Central RQ job dispatcher for all connector sync jobs.
Called by:
  - POST /tenant-admin/connectors/{type}/trigger   (manual trigger)
  - jobs.connector_scheduler                        (scheduled sweep)

Every run is recorded in connector_sync_log and stamps
tenant_connectors.last_sync_at / last_error so the scheduler can
determine what is due. Add new connectors to SYNC_JOBS as built.
"""
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, text as sa_text

logger = logging.getLogger(__name__)

# Map connector_type -> sync job module.function
SYNC_JOBS = {
    "exchange":      "jobs.exchange_sync.run",
    "manictime":     "jobs.manictime_sync.run",      # future
    "pbx_cdr":       "jobs.pbx_sync.run",            # future
    "courtlistener": "jobs.courtlistener_sync.run",  # future
}

# Dedicated lightweight sync engine for log writes — decoupled from the
# app's async engine / init state inside the worker process.
_log_engine = None


def _engine():
    global _log_engine
    if _log_engine is None:
        url = os.environ.get("DATABASE_URL", "")
        if "+asyncpg" in url:
            url = url.replace("+asyncpg", "+psycopg2")
        elif url.startswith("postgresql://") and "+psycopg" not in url:
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        _log_engine = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2)
    return _log_engine


def _record(tenant_id, connector_type, started_at, completed_at,
            records_processed, error_count, last_error, triggered_by):
    """Write a connector_sync_log row and update the connector's
    last_sync_at / last_error. Best-effort — never raises."""
    try:
        with _engine().begin() as conn:
            conn.execute(sa_text("""
                INSERT INTO connector_sync_log
                    (tenant_id, connector_type, started_at, completed_at,
                     records_processed, records_skipped, error_count,
                     last_error, triggered_by)
                VALUES
                    (:tid, :ct, :started, :completed,
                     :rp, 0, :ec, :err, :tb)
            """), {
                "tid": tenant_id, "ct": connector_type,
                "started": started_at, "completed": completed_at,
                "rp": records_processed, "ec": error_count,
                "err": last_error, "tb": triggered_by,
            })
            if error_count:
                conn.execute(sa_text("""
                    UPDATE tenant_connectors
                       SET last_error = :err, updated_at = NOW()
                     WHERE TRIM(tenant_id) = :tid AND connector_type = :ct
                """), {"err": last_error, "tid": tenant_id, "ct": connector_type})
            else:
                conn.execute(sa_text("""
                    UPDATE tenant_connectors
                       SET last_sync_at = :completed, last_error = NULL,
                           updated_at = NOW()
                     WHERE TRIM(tenant_id) = :tid AND connector_type = :ct
                """), {"completed": completed_at, "tid": tenant_id, "ct": connector_type})
    except Exception as exc:
        logger.exception("[run_connector_sync] sync-log write failed: %s", exc)


def run_connector_sync(tenant_id: str, connector_type: str, trigger_type: str = "manual"):
    """Dispatcher — imports and calls the correct sync job for connector_type.
    Runs on a proc worker via RQ. Records the run in connector_sync_log."""
    tenant_id = (tenant_id or "").strip()
    logger.info("[run_connector_sync] tenant=%s connector=%s trigger=%s",
                tenant_id, connector_type, trigger_type)

    job_path = SYNC_JOBS.get(connector_type)
    if not job_path:
        logger.warning("[run_connector_sync] no sync job for connector_type=%s", connector_type)
        return

    started_at = datetime.now(timezone.utc)
    records = 0
    try:
        module_path, fn_name = job_path.rsplit(".", 1)
        import importlib
        mod = importlib.import_module(module_path)
        fn = getattr(mod, fn_name)
        result = fn(tenant_id)

        # Best-effort record count if the sync job returns one
        if isinstance(result, int):
            records = result
        elif isinstance(result, dict):
            records = result.get("records_processed") or result.get("processed") or 0

        completed_at = datetime.now(timezone.utc)
        _record(tenant_id, connector_type, started_at, completed_at,
                records, 0, None, trigger_type)
        logger.info("[run_connector_sync] complete connector=%s tenant=%s records=%s",
                    connector_type, tenant_id, records)
    except Exception as exc:
        completed_at = datetime.now(timezone.utc)
        _record(tenant_id, connector_type, started_at, completed_at,
                0, 1, str(exc)[:2000], trigger_type)
        logger.exception("[run_connector_sync] failed connector=%s: %s",
                         connector_type, exc)
        raise
