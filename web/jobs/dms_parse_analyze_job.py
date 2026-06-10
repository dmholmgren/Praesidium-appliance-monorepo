"""
jobs/dms_parse_analyze_job.py
RQ background job: Parse and Analyze

Two-phase pipeline run as a single job:
  Phase 1 — Parse: Copy content_text from dms_documents into documents.extracted_text
            where storage_path = file_path.
  Phase 2 — Index: Push all documents with extracted_text into Elasticsearch
            for full-text search.

Job type: 'parse' in dms_scan_jobs (replaces the old parse-only job).
Progress updates every 500 rows.

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

log = logging.getLogger("praesidium.jobs.dms_parse_analyze_job")

ES_INDEX = "praesidium_documents"
BATCH_SIZE = 500
ES_BULK_SIZE = 200


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


def _get_es():
    from elasticsearch import Elasticsearch
    es_url = os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch:9200")
    return Elasticsearch([es_url], request_timeout=60)


def _is_cancelled(cur, job_id: str) -> bool:
    cur.execute("SELECT status FROM dms_scan_jobs WHERE id = %s", (job_id,))
    row = cur.fetchone()
    return row and row["status"] == "cancelled"


def _ensure_index(es):
    from jobs.dms_es_index_job import INDEX_SETTINGS, ES_INDEX
    if not es.indices.exists(index=ES_INDEX):
        es.indices.create(index=ES_INDEX, body=INDEX_SETTINGS)
        log.info("Created ES index: %s", ES_INDEX)


def _es_bulk_index(es, docs, tenant_id):
    """Push a batch of document dicts into ES. Returns (success_count, error_count)."""
    if not docs:
        return 0, 0
    now_iso = datetime.now(timezone.utc).isoformat()
    bulk_body = []
    for doc in docs:
        doc_id = doc["doc_id"]
        text = (doc.get("extracted_text") or "")[:100000]
        bulk_body.append(json.dumps({"index": {"_index": ES_INDEX, "_id": doc_id}}))
        bulk_body.append(json.dumps({
            "tenant_id": tenant_id,
            "matter_id": doc.get("matter_id"),
            "document_id": doc_id,
            "filename": doc.get("filename"),
            "title": doc.get("title"),
            "mime_type": doc.get("mime_type"),
            "document_type": doc.get("document_type"),
            "file_size": doc.get("file_size"),
            "storage_path": doc.get("storage_path"),
            "extracted_text": text,
            "checksum": doc.get("checksum"),
            "status": doc.get("status"),
            "created_at": doc["created_at"].isoformat() if doc.get("created_at") else None,
            "updated_at": doc["updated_at"].isoformat() if doc.get("updated_at") else None,
            "indexed_at": now_iso,
        }))
    try:
        bulk_str = "\n".join(bulk_body) + "\n"
        resp = es.bulk(body=bulk_str, request_timeout=120)
        ok = 0
        err = 0
        if resp.get("errors"):
            for item in resp.get("items", []):
                action = item.get("index", {})
                if action.get("error"):
                    err += 1
                else:
                    ok += 1
        else:
            ok = len(docs)
        return ok, err
    except Exception as e:
        log.warning("ES bulk failed: %s", e)
        return 0, len(docs)


def run_parse_analyze(job_id: str) -> None:
    """Phase 1: Copy text from dms_documents to documents.
    Phase 2: Index all documents with text into Elasticsearch."""
    conn = _get_db_conn()
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute("SELECT tenant_id, status FROM dms_scan_jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        if not row:
            log.error("Parse-analyze job %s not found", job_id)
            return
        tenant_id = row["tenant_id"].strip()
        status = row["status"]
        if status not in ("queued", "running"):
            return

        cur.execute("UPDATE dms_scan_jobs SET status='running', started_at=NOW() WHERE id=%s", (job_id,))
        conn.commit()

        # ── Phase 1: Parse — copy content_text to extracted_text ─────────
        log.info("Parse-analyze job %s Phase 1: Parse", job_id)
        total_parsed = 0

        while True:
            cur.execute("""
                SELECT d.id::text AS doc_id, dd.content_text
                FROM documents d
                JOIN dms_documents dd
                  ON dd.file_path = d.storage_path
                  AND TRIM(dd.tenant_id) = TRIM(d.tenant_id)
                WHERE TRIM(d.tenant_id) = %s
                  AND d.storage_path LIKE '/mnt/praesidium%%'
                  AND (d.extracted_text IS NULL OR d.extracted_text = '')
                  AND dd.content_text IS NOT NULL
                  AND dd.content_text != ''
                LIMIT %s
            """, (tenant_id, BATCH_SIZE))
            rows = cur.fetchall()

            if not rows:
                break

            for r in rows:
                cur.execute("""
                    UPDATE documents
                    SET extracted_text = %s, ocr_status = 'complete', updated_at = NOW()
                    WHERE id = CAST(%s AS uuid) AND TRIM(tenant_id) = %s
                """, (r["content_text"][:200000], r["doc_id"], tenant_id))
                total_parsed += 1

            conn.commit()
            cur.execute(
                "UPDATE dms_scan_jobs SET files_indexed=%s WHERE id=%s",
                (total_parsed, job_id)
            )
            conn.commit()

            if _is_cancelled(cur, job_id):
                log.info("Parse-analyze job %s cancelled during parse phase", job_id)
                return

        log.info("Parse-analyze job %s Phase 1 complete: %d rows parsed", job_id, total_parsed)

        # ── Phase 2: Index — push all documents with text to ES ──────────
        log.info("Parse-analyze job %s Phase 2: ES Index", job_id)
        es = _get_es()
        _ensure_index(es)

        cur.execute("""
            SELECT COUNT(*) as cnt FROM documents
            WHERE TRIM(tenant_id) = %s
              AND storage_path LIKE '/mnt/praesidium%%'
              AND extracted_text IS NOT NULL
              AND extracted_text != ''
        """, (tenant_id,))
        total_to_index = cur.fetchone()["cnt"]

        cur.execute(
            "UPDATE dms_scan_jobs SET files_discovered=%s WHERE id=%s",
            (total_to_index, job_id)
        )
        conn.commit()

        es_indexed = 0
        es_errors = 0
        offset = 0

        while offset < total_to_index:
            cur.execute("""
                SELECT id::text AS doc_id, tenant_id, matter_id::text,
                       filename, title, mime_type, document_type,
                       file_size, storage_path, extracted_text,
                       checksum, status, created_at, updated_at
                FROM documents
                WHERE TRIM(tenant_id) = %s
                  AND storage_path LIKE '/mnt/praesidium%%'
                  AND extracted_text IS NOT NULL
                  AND extracted_text != ''
                ORDER BY created_at
                LIMIT %s OFFSET %s
            """, (tenant_id, ES_BULK_SIZE, offset))
            rows = cur.fetchall()
            if not rows:
                break

            ok, err = _es_bulk_index(es, rows, tenant_id)
            es_indexed += ok
            es_errors += err
            offset += len(rows)

            cur.execute(
                "UPDATE dms_scan_jobs SET files_indexed=%s, files_skipped=%s WHERE id=%s",
                (total_parsed + es_indexed, es_errors, job_id)
            )
            conn.commit()

            if _is_cancelled(cur, job_id):
                log.info("Parse-analyze job %s cancelled during index phase", job_id)
                return

        try:
            es.indices.refresh(index=ES_INDEX)
        except Exception:
            pass

        # Complete
        cur.execute("""
            UPDATE dms_scan_jobs
            SET status='complete', completed_at=NOW(),
                files_discovered=%s, files_indexed=%s, files_skipped=%s
            WHERE id=%s
        """, (total_to_index, total_parsed + es_indexed, es_errors, job_id))
        conn.commit()
        log.info("Parse-analyze job %s complete — %d parsed, %d ES indexed, %d ES errors",
                 job_id, total_parsed, es_indexed, es_errors)

        # ── Chain: if pending OCR files exist, chain extract_ocr ─────
        try:
            cur.execute(
                "SELECT COUNT(*) as cnt FROM dms_documents "
                "WHERE TRIM(tenant_id) = %s "
                "AND (content_text IS NULL OR content_text = '') "
                "AND extraction_status = 'pending' "
                "AND lower(reverse(split_part(reverse(file_path), '.', 1))) "
                "    = ANY(ARRAY['jpg','jpeg','png','tif','tiff'])",
                (tenant_id,)
            )
            ocr_pending = cur.fetchone()["cnt"]
            if ocr_pending > 0:
                log.info("Parse complete — %d OCR files pending, chaining extract_ocr", ocr_pending)
                # Check no OCR job already running
                cur.execute(
                    "SELECT id FROM dms_scan_jobs "
                    "WHERE TRIM(tenant_id)=%s AND job_type='extract_ocr' "
                    "AND status IN ('queued','running') LIMIT 1",
                    (tenant_id,)
                )
                if not cur.fetchone():
                    import uuid as _uuid
                    ocr_job_id = str(_uuid.uuid4())
                    cur.execute(
                        "INSERT INTO dms_scan_jobs (id, tenant_id, job_type, status, queued_at) "
                        "VALUES (CAST(%s AS uuid), %s, 'extract_ocr', 'queued', NOW())",
                        (ocr_job_id, tenant_id)
                    )
                    conn.commit()
                    from redis import Redis
                    from rq import Queue
                    redis_url = os.environ.get("REDIS_URL", "redis://praesidium-redis:6379/0")
                    parts = redis_url.replace("redis://", "").split("/")
                    host_port = parts[0].split(":")
                    rhost = host_port[0]
                    rport = int(host_port[1]) if len(host_port) > 1 else 6379
                    rdb = int(parts[1]) if len(parts) > 1 and parts[1] else 0
                    rconn = Redis(host=rhost, port=rport, db=rdb)
                    q = Queue("default", connection=rconn)
                    q.enqueue("jobs.dms_extract_job.run_extract_ocr", ocr_job_id,
                              job_timeout=86400, result_ttl=86400)
                    log.info("Chained extract_ocr job %s", ocr_job_id)
                else:
                    log.info("extract_ocr already running — skipping chain")
            else:
                log.info("No pending OCR files — text pipeline complete; chaining geometry_segment")
                cur.execute(
                    "SELECT id FROM dms_scan_jobs "
                    "WHERE TRIM(tenant_id)=%s AND job_type='geometry_segment' "
                    "AND status IN ('queued','running') LIMIT 1",
                    (tenant_id,)
                )
                if not cur.fetchone():
                    import uuid as _uuid
                    gs_job_id = str(_uuid.uuid4())
                    cur.execute(
                        "INSERT INTO dms_scan_jobs (id, tenant_id, job_type, status, queued_at) "
                        "VALUES (CAST(%s AS uuid), %s, 'geometry_segment', 'queued', NOW())",
                        (gs_job_id, tenant_id)
                    )
                    conn.commit()
                    from redis import Redis
                    from rq import Queue
                    redis_url = os.environ.get("REDIS_URL", "redis://praesidium-redis:6379/0")
                    parts = redis_url.replace("redis://", "").split("/")
                    host_port = parts[0].split(":")
                    rhost = host_port[0]
                    rport = int(host_port[1]) if len(host_port) > 1 else 6379
                    rdb = int(parts[1]) if len(parts) > 1 and parts[1] else 0
                    rconn = Redis(host=rhost, port=rport, db=rdb)
                    q = Queue("default", connection=rconn)
                    q.enqueue("jobs.dms_geometry_segment_job.run_geometry_segment",
                              gs_job_id, tenant_id, job_timeout=86400, result_ttl=86400)
                    log.info("Chained geometry_segment job %s", gs_job_id)
                else:
                    log.info("geometry_segment already running — skipping chain")
        except Exception as chain_err:
            log.warning("OCR/geometry chain check failed (non-fatal): %s", chain_err)

    except Exception as exc:
        log.exception("Parse-analyze job %s failed: %s", job_id, exc)
        try:
            conn.rollback()
            cur.execute(
                "UPDATE dms_scan_jobs SET status='failed', completed_at=NOW(), error_message=%s WHERE id=%s",
                (str(exc)[:500], job_id)
            )
            conn.commit()
        except Exception:
            pass
    finally:
        cur.close()
        conn.close()
