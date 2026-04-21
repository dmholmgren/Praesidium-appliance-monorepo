"""
jobs/crawl_job.py
Module 9 Component 4 — File Crawl RQ Job

Walks the CIFS bridge via /api/v1/files/list and writes index entries
into cifs_crawl_entries. Read-only against all legacy mounts.

Called by crawl_api.py after inserting a cifs_crawl_jobs row.

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
import psycopg2

log = logging.getLogger("praesidium.jobs.crawl_job")

CIFS_URL = os.environ.get("CIFS_URL", "http://10.10.60.13:8080")


def _get_db_conn():
    """Direct psycopg2 connection for the RQ worker (no asyncpg in sync context)."""
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "")
    at_idx = url.rfind("@")
    userinfo = url[:at_idx]
    hostinfo = url[at_idx + 1:]
    user, password = userinfo.split(":", 1)
    host_port, dbname = hostinfo.split("/", 1)
    host, port = host_port.rsplit(":", 1)
    return psycopg2.connect(
        host=host,
        port=int(port),
        dbname=dbname.split("?")[0],
        user=user,
        password=password,
    )


def _list_path(mount: str, path: str, tenant_id: str) -> list:
    """Synchronous CIFS bridge list call."""
    try:
        rel = f"{mount}/{path}".strip("/")
        resp = httpx.get(
            f"{CIFS_URL}/api/v1/files/list",
            params={"tenant_id": tenant_id or "platform", "path": rel},
            timeout=15.0,
        )
        if resp.status_code == 200:
            return resp.json().get("files", [])
    except Exception as exc:
        log.warning("CIFS list failed for %s/%s: %s", mount, path, exc)
    return []


def run(job_id: str) -> None:
    """
    Main entry point called by RQ.

    Walks the CIFS share tree breadth-first up to depth_limit.
    Writes discovered files into cifs_crawl_entries.
    Updates cifs_crawl_jobs with progress and final status.
    """
    conn = _get_db_conn()
    conn.autocommit = False
    cur = conn.cursor()

    try:
        # ── Load job record ───────────────────────────────────────────────────
        cur.execute(
            """
            SELECT id, tenant_id, mount, root_path, status, options
            FROM cifs_crawl_jobs
            WHERE id = %s
            """,
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            log.error("Crawl job %s not found", job_id)
            return

        jid, tenant_id, mount, root_path, status, options = row

        if status not in ("queued", "running"):
            log.info("Crawl job %s is %s — skipping", job_id, status)
            return

        depth_limit = (options or {}).get("depth_limit", 10) if isinstance(options, dict) else 10

        # ── Mark running ──────────────────────────────────────────────────────
        cur.execute(
            """
            UPDATE cifs_crawl_jobs
            SET status = 'running', started_at = NOW()
            WHERE id = %s
            """,
            (job_id,),
        )
        conn.commit()

        # ── BFS walk ──────────────────────────────────────────────────────────
        files_discovered = 0
        files_indexed = 0
        files_skipped = 0

        # Queue: (relative_path, depth)
        queue = [(root_path or "", 0)]
        visited = set()

        while queue:
            current_path, depth = queue.pop(0)

            if current_path in visited:
                continue
            visited.add(current_path)

            if depth > depth_limit:
                files_skipped += 1
                continue

            # ── Check for cancellation ────────────────────────────────────────
            cur.execute(
                "SELECT status FROM cifs_crawl_jobs WHERE id = %s", (job_id,)
            )
            check = cur.fetchone()
            if check and check[0] == "cancelled":
                log.info("Crawl job %s cancelled mid-run", job_id)
                conn.commit()
                return

            entries = _list_path(mount, current_path, tenant_id)

            for entry in entries:
                name = entry.get("name", "")
                is_dir = entry.get("is_directory", False)
                child_path = f"{current_path}/{name}".strip("/") if current_path else name

                files_discovered += 1

                if is_dir:
                    queue.append((child_path, depth + 1))
                    continue

                # ── Write entry ───────────────────────────────────────────────
                modified_str = entry.get("modified_at")
                modified_at = None
                if modified_str:
                    try:
                        modified_at = datetime.fromisoformat(modified_str)
                    except Exception:
                        pass

                entry_id = str(uuid.uuid4())
                try:
                    cur.execute(
                        """
                        INSERT INTO cifs_crawl_entries
                          (id, tenant_id, job_id, mount, file_path, file_name,
                           file_size_bytes, mime_type, modified_at, is_directory,
                           discovered_at)
                        VALUES
                          (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (job_id, file_path) DO NOTHING
                        """,
                        (
                            entry_id, tenant_id, job_id, mount,
                            child_path, name,
                            entry.get("size"),
                            entry.get("content_type"),
                            modified_at,
                            False,
                        ),
                    )
                    files_indexed += 1
                except Exception as exc:
                    log.warning("Failed to insert entry %s: %s", child_path, exc)
                    files_skipped += 1
                    conn.rollback()
                    continue

            # Update progress every 100 files
            if files_discovered % 100 == 0:
                cur.execute(
                    """
                    UPDATE cifs_crawl_jobs
                    SET files_discovered = %s,
                        files_indexed = %s,
                        files_skipped = %s
                    WHERE id = %s
                    """,
                    (files_discovered, files_indexed, files_skipped, job_id),
                )
                conn.commit()

        # ── Mark complete ─────────────────────────────────────────────────────
        cur.execute(
            """
            UPDATE cifs_crawl_jobs
            SET status = 'complete',
                completed_at = NOW(),
                files_discovered = %s,
                files_indexed = %s,
                files_skipped = %s
            WHERE id = %s
            """,
            (files_discovered, files_indexed, files_skipped, job_id),
        )
        conn.commit()
        log.info(
            "Crawl job %s complete — %d discovered, %d indexed, %d skipped",
            job_id, files_discovered, files_indexed, files_skipped,
        )

    except Exception as exc:
        log.exception("Crawl job %s failed: %s", job_id, exc)
        try:
            conn.rollback()
            cur.execute(
                """
                UPDATE cifs_crawl_jobs
                SET status = 'failed',
                    completed_at = NOW(),
                    error_message = %s
                WHERE id = %s
                """,
                (str(exc)[:500], job_id),
            )
            conn.commit()
        except Exception:
            pass
    finally:
        cur.close()
        conn.close()
