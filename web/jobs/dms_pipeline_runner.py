"""
jobs/dms_pipeline_runner.py
RQ job: Full DMS indexing pipeline — scan → extract_fast → extract_ocr → parse.

Runs the scan phase inline, then chains the extract phases
(which themselves auto-chain through to parse).

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import logging
import os
import uuid as _uuid

log = logging.getLogger("praesidium.jobs.dms_pipeline_runner")


def _get_rq_queue():
    from redis import Redis
    from rq import Queue
    redis_url = os.environ.get("REDIS_URL", "redis://praesidium-redis:6379/0")
    parts = redis_url.replace("redis://", "").split("/")
    host_port = parts[0].split(":")
    host = host_port[0]
    port = int(host_port[1]) if len(host_port) > 1 else 6379
    db = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    conn = Redis(host=host, port=port, db=db)
    return Queue("default", connection=conn)


def _get_db_conn():
    import psycopg2
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "")
    at_idx = url.rfind("@")
    userinfo = url[:at_idx]
    hostinfo = url[at_idx + 1:]
    user, password = userinfo.split(":", 1)
    host_port, dbname = hostinfo.split("/", 1)
    host, port = host_port.rsplit(":", 1)
    return psycopg2.connect(
        host=host, port=int(port),
        dbname=dbname.split("?")[0],
        user=user, password=password,
    )


def run_pipeline(scan_job_id: str, tenant_id: str):
    """Run the full pipeline: execute scan inline, then chain extraction.
    
    The extract_fast job auto-chains extract_ocr, which auto-chains parse.
    So we only need to enqueue scan (inline) then extract_fast.
    """
    tenant_id = tenant_id.strip()
    log.info("Pipeline: starting scan phase for tenant %s", tenant_id[:8])

    # ── Phase 1: Run scan ────────────────────────────────────────────
    try:
        from jobs.dms_scan_job import run_scan
        run_scan(scan_job_id)
    except Exception as e:
        log.error("Pipeline: scan failed: %s", e)
        return

    # Check scan completed successfully
    import psycopg2.extras
    conn = _get_db_conn()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT status FROM dms_scan_jobs WHERE id = %s", (scan_job_id,))
        row = cur.fetchone()
        if not row or row["status"] != "complete":
            log.warning("Pipeline: scan did not complete (status=%s) — stopping",
                        row["status"] if row else "missing")
            return

        # ── Phase 2: Enqueue extract_fast (chains to OCR → parse) ────
        # Check no extract already running
        cur.execute(
            "SELECT id FROM dms_scan_jobs "
            "WHERE TRIM(tenant_id)=%s AND job_type IN ('extract', 'extract_fast', 'extract_ocr') "
            "AND status IN ('queued','running') LIMIT 1",
            (tenant_id,)
        )
        if cur.fetchone():
            log.info("Pipeline: extract already running — skipping chain")
            return

        extract_job_id = str(_uuid.uuid4())
        cur.execute(
            "INSERT INTO dms_scan_jobs (id, tenant_id, job_type, status, queued_at) "
            "VALUES (CAST(%s AS uuid), %s, 'extract_fast', 'queued', NOW())",
            (extract_job_id, tenant_id)
        )

        q = _get_rq_queue()
        q.enqueue("jobs.dms_extract_job.run_extract_fast", extract_job_id,
                   job_timeout=7200, result_ttl=86400)
        log.info("Pipeline: chained extract_fast job %s", extract_job_id)

    except Exception as e:
        log.error("Pipeline: chain failed: %s", e)
    finally:
        cur.close()
        conn.close()
