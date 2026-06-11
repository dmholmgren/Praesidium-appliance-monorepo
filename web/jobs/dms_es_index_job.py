"""
jobs/dms_es_index_job.py
RQ background job: push documents.extracted_text into Elasticsearch.

Creates index 'praesidium_documents' with tenant-aware mappings.
Follows the same dms_scan_jobs tracking pattern as run_scan/run_parse.

Enqueued on 'default' queue. Progress updates every 200 docs.

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

log = logging.getLogger("praesidium.jobs.dms_es_index_job")

ES_INDEX = "praesidium_documents"
BATCH_SIZE = 200

INDEX_SETTINGS = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "analysis": {
            "analyzer": {
                "legal_text": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": ["lowercase", "stop", "snowball"]
                }
            }
        }
    },
    "mappings": {
        "properties": {
            "tenant_id":       {"type": "keyword"},
            "matter_id":       {"type": "keyword"},
            "document_id":     {"type": "keyword"},
            "filename":        {"type": "text", "fields": {"raw": {"type": "keyword"}}},
            "title":           {"type": "text", "analyzer": "legal_text"},
            "mime_type":       {"type": "keyword"},
            "document_type":   {"type": "keyword"},
            "file_size":       {"type": "long"},
            "storage_path":    {"type": "keyword"},
            "extracted_text":  {"type": "text", "analyzer": "legal_text"},
            "checksum":        {"type": "keyword"},
            "status":          {"type": "keyword"},
            "created_at":      {"type": "date"},
            "updated_at":      {"type": "date"},
            "indexed_at":      {"type": "date"},
        }
    }
}


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
    if not es.indices.exists(index=ES_INDEX):
        es.indices.create(index=ES_INDEX, body=INDEX_SETTINGS)
        log.info("Created ES index: %s", ES_INDEX)
    else:
        log.info("ES index %s already exists", ES_INDEX)


def run_index(job_id: str) -> None:
    conn = _get_db_conn()
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute("SELECT tenant_id, status FROM dms_scan_jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        if not row:
            log.error("Index job %s not found", job_id)
            return
        tenant_id = row["tenant_id"].strip()
        status = row["status"]
        if status not in ("queued", "running"):
            log.info("Index job %s is %s — skipping", job_id, status)
            return

        cur.execute("UPDATE dms_scan_jobs SET status='running', started_at=NOW() WHERE id=%s", (job_id,))
        conn.commit()

        es = _get_es()
        _ensure_index(es)

        cur.execute("""
            SELECT COUNT(*) as cnt FROM documents
            WHERE TRIM(tenant_id) = %s
              AND storage_path LIKE '/mnt/praesidium%%'
              AND extracted_text IS NOT NULL
              AND extracted_text != ''
        """, (tenant_id,))
        total = cur.fetchone()["cnt"]

        cur.execute("UPDATE dms_scan_jobs SET files_discovered=%s WHERE id=%s", (total, job_id))
        conn.commit()

        if total == 0:
            cur.execute("""
                UPDATE dms_scan_jobs
                SET status='complete', completed_at=NOW(),
                    files_discovered=0, files_indexed=0, files_skipped=0
                WHERE id=%s
            """, (job_id,))
            conn.commit()
            log.info("Index job %s — no documents with extracted text to index", job_id)
            return

        indexed = 0
        skipped = 0
        offset = 0

        while offset < total:
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
            """, (tenant_id, BATCH_SIZE, offset))
            rows = cur.fetchall()

            if not rows:
                break

            now_iso = datetime.now(timezone.utc).isoformat()
            bulk_body = []
            for doc in rows:
                doc_id = doc["doc_id"]
                text = (doc["extracted_text"] or "")[:100000]

                bulk_body.append(json.dumps({"index": {"_index": ES_INDEX, "_id": doc_id}}))
                bulk_body.append(json.dumps({
                    "tenant_id": tenant_id,
                    "matter_id": doc["matter_id"],
                    "document_id": doc_id,
                    "filename": doc["filename"],
                    "title": doc["title"],
                    "mime_type": doc["mime_type"],
                    "document_type": doc["document_type"],
                    "file_size": doc["file_size"],
                    "storage_path": doc["storage_path"],
                    "extracted_text": text,
                    "checksum": doc["checksum"],
                    "status": doc["status"],
                    "created_at": doc["created_at"].isoformat() if doc["created_at"] else None,
                    "updated_at": doc["updated_at"].isoformat() if doc["updated_at"] else None,
                    "indexed_at": now_iso,
                }))

            try:
                bulk_str = "\n".join(bulk_body) + "\n"
                resp = es.bulk(body=bulk_str, request_timeout=120)

                if resp.get("errors"):
                    for item in resp.get("items", []):
                        action = item.get("index", {})
                        if action.get("error"):
                            skipped += 1
                            log.warning("ES index error for %s: %s",
                                        action.get("_id"), action["error"])
                        else:
                            indexed += 1
                else:
                    indexed += len(rows)

            except Exception as e:
                log.warning("Bulk index batch failed at offset %d: %s", offset, e)
                skipped += len(rows)

            offset += len(rows)

            cur.execute(
                "UPDATE dms_scan_jobs SET files_indexed=%s, files_skipped=%s WHERE id=%s",
                (indexed, skipped, job_id)
            )
            conn.commit()

            if _is_cancelled(cur, job_id):
                log.info("Index job %s cancelled after %d docs", job_id, indexed)
                return

        try:
            es.indices.refresh(index=ES_INDEX)
        except Exception:
            pass

        cur.execute("""
            UPDATE dms_scan_jobs
            SET status='complete', completed_at=NOW(),
                files_discovered=%s, files_indexed=%s, files_skipped=%s
            WHERE id=%s
        """, (total, indexed, skipped, job_id))
        conn.commit()
        log.info("Index job %s complete — %d indexed, %d skipped of %d total",
                 job_id, indexed, skipped, total)

    except Exception as exc:
        log.exception("Index job %s failed: %s", job_id, exc)
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
