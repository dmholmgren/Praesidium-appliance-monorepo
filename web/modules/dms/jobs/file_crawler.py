"""
COMP 2 — File Crawler
RQ jobs dispatched to PROC-01 (10.10.60.12).
Direct mount architecture — reads files from /mnt/* directly.
No CIFS bridge dependency.
Patent Pending — 64/020,027

dms_documents schema:
id, tenant_id, file_path, folder_root, file_hash, file_size_bytes,
modified_at, content_text, ocr_status, extraction_status, source,
agent_version, indexed_at, updated_at
"""

import os
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rq import Queue
from redis import Redis

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")

DEFAULT_SUPPORTED_EXTENSIONS = {
    ".docx", ".doc", ".pdf", ".xlsx", ".xls",
    ".msg", ".eml", ".jpg", ".jpeg", ".png",
    ".tiff", ".tif", ".txt", ".rtf", ".csv",
}


def _get_db_conn():
    """
    psycopg2 connection using rfind('@') to handle @ in password.
    Never use get_session_factory() — second definition returns sync factory.
    """
    import psycopg2
    raw = os.environ.get("DATABASE_URL", "")
    url = raw.replace("postgresql+asyncpg://", "postgresql://")
    at = url.rfind("@")
    rest = url[at + 1:]
    userinfo = url[len("postgresql://"):at]
    colon = userinfo.rfind(":")
    user = userinfo[:colon]
    password = userinfo[colon + 1:]
    slash = rest.find("/")
    hostport = rest[:slash]
    dbname = rest[slash + 1:]
    if ":" in hostport:
        host, port = hostport.rsplit(":", 1)
    else:
        host, port = hostport, "5432"
    return psycopg2.connect(
        host=host, port=int(port), dbname=dbname,
        user=user, password=password
    )


def get_rq_queue(name: str = "default") -> Queue:
    redis_conn = Redis.from_url(REDIS_URL)
    return Queue(name, connection=redis_conn)


def _walk_path(base_path: str):
    """
    Walk a local mount path recursively.
    Yields (abs_path, rel_path, name, size, modified_at) tuples.
    """
    base = Path(base_path)
    if not base.exists():
        logger.error(f"Base path does not exist: {base_path}")
        return

    for fpath in base.rglob("*"):
        if not fpath.is_file():
            continue
        try:
            stat = fpath.stat()
            rel = str(fpath.relative_to(base))
            yield (
                str(fpath),
                rel,
                fpath.name,
                stat.st_size,
                datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
        except Exception as e:
            logger.warning(f"Could not stat {fpath}: {e}")
            continue


def crawl_file_share(tenant_id: str, base_path: str = ""):
    """
    Full recursive crawl of a mounted path.
    Reads files directly from mount — no CIFS bridge.
    """
    from modules.dms.services.crawl_exclusion_rules import (
        load_exclusion_rules, should_skip_file,
    )

    logger.info(f"Starting crawl tenant={tenant_id} path={base_path}")

    ruleset = load_exclusion_rules(tenant_id)
    has_custom_rules = bool(
        ruleset.get("excluded_extensions")
        or ruleset.get("included_extensions")
        or ruleset.get("max_file_size") is not None
    )

    conn = _get_db_conn()
    conn.autocommit = False
    cur = conn.cursor()

    stats = {"new": 0, "modified": 0, "deleted": 0, "skipped": 0}
    seen_paths = set()
    batch = 0

    for abs_path, rel_path, name, file_size, modified_at in _walk_path(base_path):
        ext = os.path.splitext(name)[1].lower()

        # Apply exclusion rules
        skip, reason = should_skip_file(ruleset, name, file_size, rel_path)
        if skip:
            stats["skipped"] += 1
            continue

        # Default extension filter when no custom rules
        if not has_custom_rules and not ruleset.get("included_extensions"):
            if ext not in DEFAULT_SUPPORTED_EXTENSIONS:
                stats["skipped"] += 1
                continue

        seen_paths.add(abs_path)

        # Compute checksum
        try:
            sha256 = hashlib.sha256()
            with open(abs_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    sha256.update(chunk)
            checksum = sha256.hexdigest()
        except Exception as e:
            logger.error(f"Failed to hash {abs_path}: {e}")
            continue

        # Check existing record
        cur.execute(
            """SELECT id, file_hash
               FROM dms_documents
               WHERE TRIM(tenant_id) = %s AND file_path = %s""",
            (tenant_id.strip(), abs_path),
        )
        existing = cur.fetchone()

        if existing is None:
            import uuid
            doc_id = str(uuid.uuid4())
            if ext in {".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"}:
                ocr_status = "ocr_pending"
            elif ext in DEFAULT_SUPPORTED_EXTENSIONS:
                ocr_status = "text_native"
            else:
                ocr_status = "not_applicable"

            cur.execute(
                """INSERT INTO dms_documents
                   (id, tenant_id, file_path, folder_root, file_hash,
                    file_size_bytes, modified_at, ocr_status,
                    extraction_status, source, indexed_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                           'pending', 'file_crawler', NOW(), NOW())
                   ON CONFLICT (tenant_id, file_hash) DO UPDATE
                   SET file_path = EXCLUDED.file_path,
                       updated_at = NOW()
                   RETURNING id""",
                (doc_id, tenant_id.strip(), abs_path, base_path,
                 checksum, file_size, modified_at, ocr_status),
            )
            result = cur.fetchone()
            if result:
                doc_id = result[0]
            stats["new"] += 1

            if ocr_status == "ocr_pending":
                q = get_rq_queue("ocr")
                q.enqueue(
                    "modules.dms.jobs.ocr_pipeline.ocr_document",
                    tenant_id, str(doc_id),
                    job_timeout=3600,
                )

        elif existing[1] != checksum:
            cur.execute(
                """UPDATE dms_documents
                   SET file_hash = %s, file_size_bytes = %s,
                       modified_at = %s, extraction_status = 'pending',
                       updated_at = NOW()
                   WHERE id = %s AND TRIM(tenant_id) = %s""",
                (checksum, file_size, modified_at,
                 existing[0], tenant_id.strip()),
            )
            stats["modified"] += 1

        batch += 1
        if batch % 200 == 0:
            conn.commit()
            logger.info(
                f"Crawl progress tenant={tenant_id}: "
                f"new={stats['new']} modified={stats['modified']} "
                f"skipped={stats['skipped']}"
            )

    conn.commit()

    # Soft-delete files no longer on mount
    if seen_paths:
        cur.execute(
            """SELECT id, file_path FROM dms_documents
               WHERE TRIM(tenant_id) = %s
               AND file_path LIKE %s
               AND ocr_status != 'deleted'""",
            (tenant_id.strip(), f"{base_path}%"),
        )
        all_docs = cur.fetchall()
        for doc_id, doc_path in all_docs:
            if doc_path not in seen_paths:
                cur.execute(
                    """UPDATE dms_documents
                       SET ocr_status = 'deleted', updated_at = NOW()
                       WHERE id = %s AND TRIM(tenant_id) = %s""",
                    (doc_id, tenant_id.strip()),
                )
                stats["deleted"] += 1
        conn.commit()

    conn.close()
    logger.info(f"Crawl complete tenant={tenant_id} path={base_path}: {stats}")
    return stats


def crawl_single_file(tenant_id: str, path: str):
    """Process a single file change."""
    crawl_file_share(tenant_id, base_path=os.path.dirname(path))


def soft_delete_file(tenant_id: str, path: str):
    """Soft-delete a document removed from the file share."""
    conn = _get_db_conn()
    try:
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute(
            """UPDATE dms_documents
               SET ocr_status = 'deleted', updated_at = NOW()
               WHERE TRIM(tenant_id) = %s AND file_path = %s""",
            (tenant_id.strip(), path),
        )
        conn.commit()
    finally:
        conn.close()
