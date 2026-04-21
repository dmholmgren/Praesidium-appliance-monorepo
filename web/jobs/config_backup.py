"""
jobs/config_backup.py
Module 8 Component 0e — PROC-01 Jobs

Config backup RQ job.

Creates a .tar.gz config backup archive containing:
  db-export/         — pg_dump of platform config tables (NOT tenant data)
  env-template/      — .env.bootstrap template (secrets blanked)
  scripts/           — current provision scripts from /opt/praesidium/scripts/
  MANIFEST.txt       — SHA-256 checksums + metadata

Records the backup in config_backups with:
  config_archive_path  — path to .tar.gz on disk
  config_hash          — SHA-256 of archive
  alembic_head         — Alembic revision at time of backup
  tenant_count         — number of active tenants

Two entry points:
  create_config_backup(label, triggered_by, pre_event_type)
    — called on-demand by admin panel
  auto_backup_before_event(event_type, triggered_by)
    — called automatically by bank swap / firmware ops

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("praesidium.jobs.config_backup")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/opt/praesidium/backups")

# Config tables to export — platform infrastructure only, NOT tenant data
CONFIG_TABLES = [
    "middleware_servers",
    "firmware_versions",
    "firmware_banks",
    "firmware_events",
    "platform_manifest",
    "config_generated_scripts",
    "praesidium_connect_sites",
    "storage_health_snapshots",
    "tenants",
    "tenant_licenses",
    "feature_overrides",
]


# ══════════════════════════════════════════════════════════════════════════════
# Public job functions
# ══════════════════════════════════════════════════════════════════════════════

def create_config_backup(
    label: Optional[str] = None,
    triggered_by: str = "admin-panel",
    pre_event_type: Optional[str] = None,
    is_auto: bool = False,
) -> dict:
    """
    RQ job: Create a full config backup archive.

    Steps:
      1. pg_dump config tables (schema + data, no tenant document data)
      2. Collect current provision scripts
      3. Build .env.bootstrap template (secrets blanked)
      4. Write MANIFEST.txt with checksums
      5. Assemble .tar.gz
      6. Update config_backups record with archive path + hash
      7. Record firmware_event
    """
    log.info(f"create_config_backup: label={label} by={triggered_by} auto={is_auto}")

    timestamp = datetime.now(timezone.utc)
    ts_str = timestamp.strftime("%Y%m%d-%H%M%S")
    label = label or f"backup-{ts_str}"

    os.makedirs(BACKUP_DIR, exist_ok=True)
    archive_name = f"praesidium-config-{ts_str}.tar.gz"
    archive_path = os.path.join(BACKUP_DIR, archive_name)

    conn = _db_connect()
    if not conn:
        return _err("DB connection failed")

    try:
        # 1. Alembic head
        alembic_head = _get_alembic_head()

        # 2. Tenant count
        tenant_count = _get_tenant_count(conn)

        # 3. DB export (config tables only)
        db_export = _pg_dump_config_tables()

        # 4. Provision scripts
        scripts = _collect_scripts()

        # 5. .env.bootstrap template (blanked)
        env_template = _get_env_template()

        # 6. Build MANIFEST
        manifest_data = {
            "label": label,
            "triggered_by": triggered_by,
            "is_auto": is_auto,
            "pre_event_type": pre_event_type,
            "alembic_head": alembic_head,
            "tenant_count": tenant_count,
            "timestamp": timestamp.isoformat(),
            "files": {},
        }

        # 7. Assemble archive
        with tarfile.open(archive_path, "w:gz") as tar:
            # DB export
            if db_export:
                _add_bytes_to_tar(tar, "db-export/config.sql", db_export.encode())
                manifest_data["files"]["db-export/config.sql"] = _sha256_str(db_export)

            # Provision scripts
            for filename, content in scripts.items():
                arcname = f"scripts/{filename}"
                _add_bytes_to_tar(tar, arcname, content.encode(), mode=0o750)
                manifest_data["files"][arcname] = _sha256_str(content)

            # .env.bootstrap template
            if env_template:
                _add_bytes_to_tar(tar, "env-template/.env.bootstrap", env_template.encode(), mode=0o600)
                manifest_data["files"]["env-template/.env.bootstrap"] = _sha256_str(env_template)

            # MANIFEST last (after all other files so hashes are complete)
            manifest_str = _build_manifest_text(manifest_data)
            _add_bytes_to_tar(tar, "MANIFEST.txt", manifest_str.encode())

        archive_sha256 = _sha256_file(archive_path)
        archive_size = os.path.getsize(archive_path)

        # 8. Insert/update config_backups record
        backup_id = _record_backup(
            conn=conn,
            label=label,
            triggered_by=triggered_by,
            is_auto=is_auto,
            pre_event_type=pre_event_type,
            archive_path=archive_path,
            archive_sha256=archive_sha256,
            alembic_head=alembic_head,
            tenant_count=tenant_count,
        )

        # 9. Record firmware event
        with conn.cursor() as cur:
            _insert_event(cur, event_type="config_save", vm_name="MAIN-PRD-WEB-01",
                          triggered_by=triggered_by,
                          detail={
                              "backup_id": backup_id,
                              "label": label,
                              "archive_path": archive_path,
                              "archive_sha256": archive_sha256[:16] + "...",
                              "tenant_count": tenant_count,
                          })
        conn.commit()

        log.info(f"create_config_backup OK: {archive_path} ({archive_size} bytes)")
        return {
            "status": "ok",
            "backup_id": backup_id,
            "label": label,
            "archive_path": archive_path,
            "archive_sha256": archive_sha256,
            "archive_size": archive_size,
            "alembic_head": alembic_head,
            "tenant_count": tenant_count,
        }

    except Exception as exc:
        log.error(f"create_config_backup failed: {exc}")
        conn.rollback()
        return _err(str(exc))
    finally:
        conn.close()


def auto_backup_before_event(
    event_type: str,
    triggered_by: str = "system",
) -> dict:
    """
    Convenience wrapper: create an auto config backup before a firmware event.
    Sets is_auto=True and pre_event_type=event_type.
    """
    return create_config_backup(
        label=f"auto-pre-{event_type}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        triggered_by=triggered_by,
        pre_event_type=event_type,
        is_auto=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _err(msg: str) -> dict:
    log.error(f"config_backup error: {msg}")
    return {"status": "error", "error": msg}


def _db_connect():
    try:
        import psycopg2
        db_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        return psycopg2.connect(db_url, connect_timeout=10)
    except Exception as exc:
        log.error(f"DB connect failed: {exc}")
        return None


def _insert_event(cur, event_type: str, vm_name: str, triggered_by: str, detail: dict) -> None:
    cur.execute(
        """INSERT INTO firmware_events
             (id, event_type, vm_name, triggered_by, detail, created_at)
           VALUES (gen_random_uuid()::text, %s, %s, %s, %s::jsonb, NOW())""",
        (event_type, vm_name, triggered_by, json.dumps(detail))
    )


def _get_alembic_head() -> str:
    try:
        result = subprocess.run(
            ["alembic", "current"], capture_output=True, text=True, timeout=10,
            cwd=os.environ.get("APP_ROOT", "/app"),
        )
        parts = result.stdout.strip().split()
        return parts[0][:12] if parts else "unknown"
    except Exception:
        return "unknown"


def _get_tenant_count(conn) -> int:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM tenants WHERE provisioning_status = 'active'")
            row = cur.fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


def _pg_dump_config_tables() -> Optional[str]:
    """
    Dump config tables using pg_dump --table flags.
    Returns SQL string or None if pg_dump unavailable.
    Secrets (passwords) are not in these tables — safe to export.
    """
    try:
        db_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        table_args = []
        for t in CONFIG_TABLES:
            table_args.extend(["--table", t])

        result = subprocess.run(
            ["pg_dump", db_url, "--no-owner", "--no-privileges",
             "--format=plain"] + table_args,
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            return result.stdout
        log.warning(f"pg_dump failed (non-fatal): {result.stderr[:200]}")
        return None
    except FileNotFoundError:
        log.warning("pg_dump not found — skipping DB export in backup")
        return None
    except Exception as exc:
        log.warning(f"pg_dump exception: {exc}")
        return None


def _collect_scripts() -> dict:
    """Collect v3.0 provision scripts from /opt/praesidium/scripts/."""
    scripts = {}
    scripts_dir = "/opt/praesidium/scripts"
    if not os.path.isdir(scripts_dir):
        return scripts
    for filename in os.listdir(scripts_dir):
        if filename.endswith(".sh"):
            path = os.path.join(scripts_dir, filename)
            try:
                with open(path) as fh:
                    scripts[filename] = fh.read()
            except Exception as exc:
                log.warning(f"Could not read script {path}: {exc}")
    return scripts


def _get_env_template() -> Optional[str]:
    """
    Read /etc/praesidium/.env.bootstrap if present.
    This is already a template with secrets blanked — safe to include.
    """
    path = "/etc/praesidium/.env.bootstrap"
    if os.path.isfile(path):
        try:
            with open(path) as fh:
                return fh.read()
        except Exception:
            pass
    return None


def _build_manifest_text(data: dict) -> str:
    lines = [
        "═" * 70,
        "  PRAESIDIUM CONFIG BACKUP — MANIFEST",
        "═" * 70,
        "",
        f"  Label:          {data['label']}",
        f"  Triggered by:   {data['triggered_by']}",
        f"  Auto backup:    {data['is_auto']}",
        f"  Pre-event type: {data.get('pre_event_type', 'n/a')}",
        f"  Timestamp:      {data['timestamp']}",
        f"  Alembic head:   {data['alembic_head']}",
        f"  Tenant count:   {data['tenant_count']}",
        "",
        "  File checksums (SHA-256):",
    ]
    for fname, sha in data.get("files", {}).items():
        lines.append(f"  {sha}  {fname}")
    lines += [
        "",
        "  Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168",
        "═" * 70,
    ]
    return "\n".join(lines) + "\n"


def _add_bytes_to_tar(tar: tarfile.TarFile, arcname: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mode = mode
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    tar.addfile(info, io.BytesIO(data))


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _record_backup(
    conn,
    label: str,
    triggered_by: str,
    is_auto: bool,
    pre_event_type: Optional[str],
    archive_path: str,
    archive_sha256: str,
    alembic_head: str,
    tenant_count: int,
) -> Optional[str]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO config_backups
                     (id, label, triggered_by, is_auto, pre_event_type,
                      config_archive_path, config_hash, alembic_head, tenant_count)
                   VALUES
                     (gen_random_uuid()::text, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (label, triggered_by, is_auto, pre_event_type,
                 archive_path, archive_sha256, alembic_head, tenant_count)
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.warning(f"_record_backup failed: {exc}")
        return None
