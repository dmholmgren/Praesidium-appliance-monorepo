"""
jobs/dms_scan_job.py
RQ background jobs for DMS disk scanning and text-parse backfill.

Two entry points:
  run_scan(job_id)  — Walk matter_folders, register unindexed files into documents.
  run_parse(job_id) — Copy content_text from dms_documents into documents.extracted_text.

Both follow the crawl_job.py pattern: psycopg2 direct, status tracking via
dms_scan_jobs table, progress updates every 500 files, cancellation checks.

Enqueued on the 'default' queue (workers already listen on it).

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
from pathlib import Path

import psycopg2

log = logging.getLogger("praesidium.jobs.dms_scan_job")

SKIP_DIRS = {".git", "__pycache__", "@eaDir", ".Spotlight-V100", ".Trash"}
SKIP_PREFIXES = (".", "~$", "Thumbs.db")


def _get_db_conn():
    """Direct psycopg2 connection for sync RQ worker context."""
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


def _checksum(filepath: str) -> str:
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except (OSError, PermissionError):
        return ""
    return h.hexdigest()


def _should_skip(name: str) -> bool:
    return any(name.startswith(p) for p in SKIP_PREFIXES)


def _is_cancelled(cur, job_id: str) -> bool:
    cur.execute("SELECT status FROM dms_scan_jobs WHERE id = %s", (job_id,))
    row = cur.fetchone()
    return row and row[0] == "cancelled"


# ── Scan Job ─────────────────────────────────────────────────────────────────

def run_scan(job_id: str) -> None:
    """Walk matter_folders with disk_root under /mnt/praesidium/, register
    unindexed files into the documents table. Progress tracked in dms_scan_jobs."""
    conn = _get_db_conn()
    conn.autocommit = False
    cur = conn.cursor()

    try:
        # Load job record
        cur.execute(
            "SELECT tenant_id, status FROM dms_scan_jobs WHERE id = %s",
            (job_id,)
        )
        row = cur.fetchone()
        if not row:
            log.error("Scan job %s not found", job_id)
            return
        tenant_id, status = row[0].strip(), row[1]
        if status not in ("queued", "running"):
            log.info("Scan job %s is %s — skipping", job_id, status)
            return

        # Mark running
        cur.execute(
            "UPDATE dms_scan_jobs SET status='running', started_at=NOW() WHERE id=%s",
            (job_id,)
        )
        conn.commit()

        # Get matter folders
        cur.execute("""
            SELECT mf.matter_id::text, mf.disk_root
            FROM matter_folders mf
            WHERE mf.disk_root LIKE '/mnt/praesidium%%'
              AND TRIM(mf.tenant_id) = %s
        """, (tenant_id,))
        folders = cur.fetchall()

        discovered = 0
        indexed = 0
        skipped = 0

        for matter_id, disk_root in folders:
            if not os.path.isdir(disk_root):
                continue

            for dirpath, dirnames, filenames in os.walk(disk_root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]

                for fname in filenames:
                    if _should_skip(fname):
                        continue
                    full_path = os.path.join(dirpath, fname)
                    if not os.path.isfile(full_path):
                        continue

                    discovered += 1

                    # Check if already registered
                    cur.execute(
                        "SELECT 1 FROM documents WHERE storage_path=%s AND TRIM(tenant_id)=%s",
                        (full_path, tenant_id)
                    )
                    if cur.fetchone():
                        skipped += 1
                        # Progress update every 500
                        if discovered % 500 == 0:
                            cur.execute(
                                "UPDATE dms_scan_jobs SET files_discovered=%s, files_indexed=%s, files_skipped=%s WHERE id=%s",
                                (discovered, indexed, skipped, job_id)
                            )
                            conn.commit()
                            if _is_cancelled(cur, job_id):
                                log.info("Scan job %s cancelled", job_id)
                                return
                        continue

                    try:
                        st = os.stat(full_path)
                        mime = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
                        ext = Path(fname).suffix.lstrip(".").lower()
                        checksum = _checksum(full_path)

                        cur.execute("""
                            INSERT INTO documents
                                (id, tenant_id, matter_id, filename, original_filename,
                                 title, mime_type, file_size, storage_path,
                                 document_type, status, checksum, ocr_status,
                                 created_at, updated_at)
                            VALUES
                                (gen_random_uuid(), %s, CAST(%s AS uuid), %s, %s,
                                 %s, %s, %s, %s,
                                 %s, 'active', %s, 'pending',
                                 NOW(), NOW())
                            ON CONFLICT DO NOTHING
                        """, (
                            tenant_id, matter_id, fname, fname,
                            fname, mime, st.st_size, full_path,
                            ext or "unknown", checksum,
                        ))
                        indexed += 1
                    except Exception as e:
                        log.warning("Error registering %s: %s", full_path, e)
                        skipped += 1
                        conn.rollback()

                    # Progress update every 500
                    if discovered % 500 == 0:
                        cur.execute(
                            "UPDATE dms_scan_jobs SET files_discovered=%s, files_indexed=%s, files_skipped=%s WHERE id=%s",
                            (discovered, indexed, skipped, job_id)
                        )
                        conn.commit()
                        if _is_cancelled(cur, job_id):
                            log.info("Scan job %s cancelled", job_id)
                            return

        # Mark complete
        cur.execute("""
            UPDATE dms_scan_jobs
            SET status='complete', completed_at=NOW(),
                files_discovered=%s, files_indexed=%s, files_skipped=%s
            WHERE id=%s
        """, (discovered, indexed, skipped, job_id))
        conn.commit()
        log.info("Scan job %s complete — %d discovered, %d indexed, %d skipped",
                 job_id, discovered, indexed, skipped)

    except Exception as exc:
        log.exception("Scan job %s failed: %s", job_id, exc)
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


# ── Parse Job ────────────────────────────────────────────────────────────────

def run_parse(job_id: str) -> None:
    """Copy content_text from dms_documents into documents.extracted_text
    where storage_path = file_path. Processes ALL matching rows in batches of 500."""
    conn = _get_db_conn()
    conn.autocommit = False
    cur = conn.cursor()

    try:
        cur.execute(
            "SELECT tenant_id, status FROM dms_scan_jobs WHERE id = %s",
            (job_id,)
        )
        row = cur.fetchone()
        if not row:
            log.error("Parse job %s not found", job_id)
            return
        tenant_id, status = row[0].strip(), row[1]
        if status not in ("queued", "running"):
            log.info("Parse job %s is %s — skipping", job_id, status)
            return

        cur.execute(
            "UPDATE dms_scan_jobs SET status='running', started_at=NOW() WHERE id=%s",
            (job_id,)
        )
        conn.commit()

        total_parsed = 0
        batch_size = 500

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
            """, (tenant_id, batch_size))
            rows = cur.fetchall()

            if not rows:
                break

            for doc_id, content_text in rows:
                cur.execute("""
                    UPDATE documents
                    SET extracted_text = %s, ocr_status = 'complete', updated_at = NOW()
                    WHERE id = CAST(%s AS uuid) AND TRIM(tenant_id) = %s
                """, (content_text[:50000], doc_id, tenant_id))
                total_parsed += 1

            conn.commit()

            # Update progress
            cur.execute(
                "UPDATE dms_scan_jobs SET files_indexed=%s WHERE id=%s",
                (total_parsed, job_id)
            )
            conn.commit()

            if _is_cancelled(cur, job_id):
                log.info("Parse job %s cancelled after %d rows", job_id, total_parsed)
                return

        # Mark complete
        cur.execute("""
            UPDATE dms_scan_jobs
            SET status='complete', completed_at=NOW(), files_indexed=%s
            WHERE id=%s
        """, (total_parsed, job_id))
        conn.commit()
        log.info("Parse job %s complete — %d rows backfilled", job_id, total_parsed)

    except Exception as exc:
        log.exception("Parse job %s failed: %s", job_id, exc)
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
