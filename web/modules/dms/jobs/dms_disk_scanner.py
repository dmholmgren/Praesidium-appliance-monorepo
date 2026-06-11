"""
modules/dms/jobs/dms_disk_scanner.py
DMS Disk Scanner — Walks Praesidium matter folders, finds files not yet
registered in the documents table, and creates DB records for them.

Two tables exist:
  - documents — matter-linked DMS registry (MCP tools, UI, matter context)
  - dms_documents — OCR/text extraction cache (file_path keyed, no matter_id)

This scanner populates documents. OCR pipeline populates dms_documents.
They join on storage_path = file_path when both are needed.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
from pathlib import Path

log = logging.getLogger("praesidium.dms_disk_scanner")

PRAESIDIUM_ROOT = "/mnt/praesidium"
SKIP_DIRS = {".git", "__pycache__", "@eaDir", ".Spotlight-V100", ".Trash"}
SKIP_PREFIXES = (".", "~$", "Thumbs.db")


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


async def scan_tenant(tenant_id: str, db_session) -> dict:
    from sqlalchemy import text

    rows = (await db_session.execute(text("""
        SELECT mf.matter_id::text, mf.disk_root, m.matter_name, c.client_name
        FROM matter_folders mf
        JOIN matters m ON m.id = mf.matter_id
        LEFT JOIN clients c ON m.client_id = c.id
        WHERE mf.disk_root LIKE '/mnt/praesidium%'
          AND TRIM(mf.tenant_id) = :tid
    """), {"tid": tenant_id.strip()})).fetchall()

    stats = {
        "matters_scanned": 0, "files_found": 0,
        "files_already_indexed": 0, "files_registered": 0,
        "errors": 0, "matters_with_new_files": [],
    }

    for row in rows:
        matter_id = str(row.matter_id)
        disk_root = row.disk_root

        if not os.path.isdir(disk_root):
            continue

        stats["matters_scanned"] += 1
        matter_new = 0

        for dirpath, dirnames, filenames in os.walk(disk_root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            rel_dir = os.path.relpath(dirpath, disk_root)
            if rel_dir == ".":
                rel_dir = ""

            for fname in filenames:
                if _should_skip(fname):
                    continue
                full_path = os.path.join(dirpath, fname)
                if not os.path.isfile(full_path):
                    continue

                stats["files_found"] += 1

                existing = (await db_session.execute(text(
                    "SELECT id FROM documents WHERE storage_path = :sp AND TRIM(tenant_id) = :tid"
                ), {"sp": full_path, "tid": tenant_id.strip()})).fetchone()

                if existing:
                    stats["files_already_indexed"] += 1
                    continue

                try:
                    st = os.stat(full_path)
                    mime = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
                    ext = Path(fname).suffix.lstrip(".").lower()
                    checksum = _checksum(full_path)

                    await db_session.execute(text("""
                        INSERT INTO documents
                            (id, tenant_id, matter_id, filename, original_filename,
                             title, mime_type, file_size, storage_path,
                             document_type, status, checksum, ocr_status,
                             created_at, updated_at)
                        VALUES
                            (gen_random_uuid(), :tid, CAST(:mid AS uuid), :fname, :fname,
                             :title, :mime, :fsize, :spath,
                             :dtype, 'active', :checksum, 'pending',
                             NOW(), NOW())
                        ON CONFLICT DO NOTHING
                    """), {
                        "tid": tenant_id.strip(), "mid": matter_id,
                        "fname": fname, "title": fname,
                        "mime": mime, "fsize": st.st_size,
                        "spath": full_path, "dtype": ext or "unknown",
                        "checksum": checksum,
                    })
                    stats["files_registered"] += 1
                    matter_new += 1
                except Exception as e:
                    log.error(f"Error registering {full_path}: {e}")
                    stats["errors"] += 1

        if matter_new > 0:
            stats["matters_with_new_files"].append({
                "matter_id": matter_id,
                "matter_name": row.matter_name or "",
                "new_files": matter_new,
            })

    await db_session.commit()
    return stats


async def get_unindexed_count(tenant_id: str, db_session) -> dict:
    from sqlalchemy import text

    rows = (await db_session.execute(text("""
        SELECT mf.disk_root FROM matter_folders mf
        WHERE mf.disk_root LIKE '/mnt/praesidium%'
          AND TRIM(mf.tenant_id) = :tid
    """), {"tid": tenant_id.strip()})).fetchall()

    disk_count = 0
    for row in rows:
        root = row.disk_root
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fname in filenames:
                if not _should_skip(fname):
                    disk_count += 1

    indexed = (await db_session.execute(text("""
        SELECT COUNT(*) FROM documents
        WHERE TRIM(tenant_id) = :tid AND storage_path LIKE '/mnt/praesidium%'
    """), {"tid": tenant_id.strip()})).scalar() or 0

    return {
        "disk_files": disk_count,
        "indexed_files": indexed,
        "unindexed_files": max(0, disk_count - indexed),
    }
