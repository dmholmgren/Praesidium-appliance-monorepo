"""
jobs/dms_sweep_job.py
Periodic sweep: check for pending extractions and kick off pipeline if needed.

Designed to run every 15 minutes via RQ-scheduler or cron.
Checks dms_documents for pending extractions, and if any exist with no
active extract/parse job running, enqueues extract_fast (which chains
through parse → extract_ocr → parse).

Also checks for unparsed documents (extracted but not in ES) and kicks
off a parse job if needed.

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import logging
import os
import uuid as _uuid

import psycopg2
import psycopg2.extras

log = logging.getLogger("praesidium.jobs.dms_sweep_job")


def _get_db_conn():
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


def run_sweep():
    """Check all tenants for pending work and enqueue jobs as needed.
    Safe to call frequently — skips if jobs already running."""
    conn = _get_db_conn()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        # Get all active tenants
        cur.execute("SELECT DISTINCT TRIM(tenant_id) AS tid FROM dms_documents")
        tenants = [r["tid"] for r in cur.fetchall()]

        for tid in tenants:
            _sweep_tenant(cur, conn, tid)

    except Exception as e:
        log.exception("Sweep failed: %s", e)
    finally:
        cur.close()
        conn.close()


def _sweep_tenant(cur, conn, tenant_id):
    """Check a single tenant for pending work."""

    # Check if ANY pipeline job is already running/queued
    cur.execute(
        "SELECT id, job_type FROM dms_scan_jobs "
        "WHERE TRIM(tenant_id) = %s "
        "AND status IN ('queued', 'running') "
        "LIMIT 1",
        (tenant_id,)
    )
    active = cur.fetchone()
    if active:
        # Something is already running — don't interfere
        return

    # ── Check 1: Pending fast extractions ─────────────────────────────
    cur.execute(
        "SELECT COUNT(*) AS cnt FROM dms_documents "
        "WHERE TRIM(tenant_id) = %s "
        "AND extraction_status = 'pending' "
        "AND (content_text IS NULL OR content_text = '') "
        "AND lower(reverse(split_part(reverse(file_path), '.', 1))) "
        "    = ANY(ARRAY['pdf','txt','csv','log','docx','xlsx','xls','msg','eml','rtf'])",
        (tenant_id,)
    )
    fast_pending = cur.fetchone()["cnt"]

    if fast_pending > 0:
        _enqueue_job(cur, tenant_id, "extract_fast",
                     "jobs.dms_extract_job.run_extract_fast", 7200)
        log.info("Sweep: tenant %s — %d fast files pending, enqueued extract_fast",
                 tenant_id[:8], fast_pending)
        return  # extract_fast will chain parse → ocr → parse

    # ── Check 2: Pending OCR extractions (no fast pending) ────────────
    cur.execute(
        "SELECT COUNT(*) AS cnt FROM dms_documents "
        "WHERE TRIM(tenant_id) = %s "
        "AND extraction_status = 'pending' "
        "AND (content_text IS NULL OR content_text = '') "
        "AND lower(reverse(split_part(reverse(file_path), '.', 1))) "
        "    = ANY(ARRAY['jpg','jpeg','png','tif','tiff'])",
        (tenant_id,)
    )
    ocr_pending = cur.fetchone()["cnt"]

    if ocr_pending > 0:
        _enqueue_job(cur, tenant_id, "extract_ocr",
                     "jobs.dms_extract_job.run_extract_ocr", 86400)
        log.info("Sweep: tenant %s — %d OCR files pending, enqueued extract_ocr",
                 tenant_id[:8], ocr_pending)
        return  # extract_ocr will chain parse

    # ── Check 3: Unparsed documents (extracted but not in documents table) ─
    cur.execute("""
        SELECT COUNT(*) AS cnt
        FROM documents d
        JOIN dms_documents dd
          ON dd.file_path = d.storage_path
          AND TRIM(dd.tenant_id) = TRIM(d.tenant_id)
        WHERE TRIM(d.tenant_id) = %s
          AND d.storage_path LIKE '/mnt/praesidium%%'
          AND (d.extracted_text IS NULL OR d.extracted_text = '')
          AND dd.content_text IS NOT NULL
          AND dd.content_text != ''
    """, (tenant_id,))
    unparsed = cur.fetchone()["cnt"]

    if unparsed > 0:
        _enqueue_job(cur, tenant_id, "parse",
                     "jobs.dms_parse_analyze_job.run_parse_analyze", 14400)
        log.info("Sweep: tenant %s — %d unparsed docs, enqueued parse",
                 tenant_id[:8], unparsed)
        return


def _enqueue_job(cur, tenant_id, job_type, func_path, timeout):
    """Create dms_scan_jobs row and enqueue RQ job."""
    job_id = str(_uuid.uuid4())
    cur.execute(
        "INSERT INTO dms_scan_jobs (id, tenant_id, job_type, status, queued_at) "
        "VALUES (CAST(%s AS uuid), %s, %s, 'queued', NOW())",
        (job_id, tenant_id, job_type)
    )
    q = _get_rq_queue()
    q.enqueue(func_path, job_id, job_timeout=timeout, result_ttl=86400)


# ── Self-scheduling: re-enqueue itself in 15 minutes ─────────────────────────

def run_sweep_loop():
    """Run sweep, then re-enqueue itself in 15 minutes.
    Bootstrap with: rq enqueue jobs.dms_sweep_job.run_sweep_loop"""
    run_sweep()
    try:
        from redis import Redis
        from rq import Queue
        from datetime import timedelta
        redis_url = os.environ.get("REDIS_URL", "redis://praesidium-redis:6379/0")
        parts = redis_url.replace("redis://", "").split("/")
        host_port = parts[0].split(":")
        host = host_port[0]
        port = int(host_port[1]) if len(host_port) > 1 else 6379
        db = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        rconn = Redis(host=host, port=port, db=db)
        q = Queue("default", connection=rconn)
        q.enqueue_in(
            timedelta(minutes=15),
            "jobs.dms_sweep_job.run_sweep_loop",
            job_timeout=300,
            result_ttl=3600,
        )
        log.info("Sweep: re-scheduled in 15 minutes")
    except Exception as e:
        log.warning("Sweep: failed to re-schedule: %s", e)
