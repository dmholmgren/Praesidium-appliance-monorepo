"""
jobs/config_bundle.py
Module 8 Component 0d — Bundle Assembler RQ Job

Assembles a firmware provision bundle (.tar.gz) containing:
  provision.sh      — the generated VM provision script (executable)
  .env.bootstrap    — secrets-blanked .env.bootstrap template
  MANIFEST.txt      — SHA-256 checksums, firmware version, generated timestamp,
                      Alembic head, hostname, role

The bundle is written to BUNDLE_DIR/{hostname}-{timestamp}.tar.gz
The config_generated_scripts record is updated with the bundle_path after assembly.

This job is enqueued by POST /admin/api/config/generate.
It runs on PROC-01 RQ workers (default queue).

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("praesidium.jobs.config_bundle")

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_BUNDLE_DIR = os.environ.get("BUNDLE_DIR", "/opt/praesidium/bundles")
DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ── Main job function ─────────────────────────────────────────────────────────

def assemble_bundle(
    record_id: Optional[str],
    role: str,
    hostname: str,
    bundle_dir: str = DEFAULT_BUNDLE_DIR,
) -> dict:
    """
    RQ job: assemble a provision bundle for the given record_id.

    1. Load script_content + env_template from config_generated_scripts
    2. Compute SHA-256 checksums
    3. Build MANIFEST.txt
    4. Write all three files into a .tar.gz
    5. Update config_generated_scripts.notes with bundle_path

    Returns a summary dict for the RQ job result.
    """
    log.info(f"assemble_bundle: record_id={record_id} role={role} hostname={hostname}")

    if not record_id:
        log.warning("assemble_bundle: no record_id — skipping")
        return {"status": "skipped", "reason": "no record_id"}

    # Load record synchronously (RQ workers use sync psycopg2 or blocking call)
    record = _load_record_sync(record_id)
    if not record:
        log.error(f"assemble_bundle: record {record_id} not found in DB")
        return {"status": "error", "reason": f"record {record_id} not found"}

    script_content: str = record["script_content"]
    env_template: str = record["env_template"]
    firmware_version: str = record.get("firmware_version", "series-2.0")
    generated_at: str = str(record.get("generated_at", datetime.now(timezone.utc).isoformat()))

    # Compute checksums
    script_sha256 = hashlib.sha256(script_content.encode()).hexdigest()
    env_sha256 = hashlib.sha256(env_template.encode()).hexdigest()

    # Get Alembic head
    alembic_head = _get_alembic_head()

    # Build MANIFEST
    manifest = _build_manifest(
        role=role,
        hostname=hostname,
        firmware_version=firmware_version,
        generated_at=generated_at,
        alembic_head=alembic_head,
        script_sha256=script_sha256,
        env_sha256=env_sha256,
    )
    manifest_sha256 = hashlib.sha256(manifest.encode()).hexdigest()

    # Create bundle directory
    os.makedirs(bundle_dir, exist_ok=True)

    # Bundle filename: role-hostname-YYYYMMDD-HHMMSS.tar.gz
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    bundle_name = f"{role}-{hostname.lower()}-{ts}.tar.gz"
    bundle_path = os.path.join(bundle_dir, bundle_name)

    # Assemble tar.gz in memory then write
    with tarfile.open(bundle_path, "w:gz") as tar:
        _add_string_to_tar(tar, f"{role}-provision.sh", script_content, mode=0o755)
        _add_string_to_tar(tar, ".env.bootstrap", env_template, mode=0o600)
        _add_string_to_tar(tar, "MANIFEST.txt", manifest, mode=0o644)

    bundle_sha256 = _sha256_file(bundle_path)
    bundle_size = os.path.getsize(bundle_path)

    log.info(
        f"Bundle assembled: {bundle_path} "
        f"({bundle_size} bytes, sha256={bundle_sha256[:16]}...)"
    )

    # Update record with bundle path
    _update_record_bundle_path(record_id, bundle_path, bundle_sha256)

    return {
        "status": "ok",
        "record_id": record_id,
        "bundle_path": bundle_path,
        "bundle_sha256": bundle_sha256,
        "bundle_size": bundle_size,
        "alembic_head": alembic_head,
        "files": {
            "provision.sh": script_sha256,
            ".env.bootstrap": env_sha256,
            "MANIFEST.txt": manifest_sha256,
        },
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_manifest(
    role: str,
    hostname: str,
    firmware_version: str,
    generated_at: str,
    alembic_head: str,
    script_sha256: str,
    env_sha256: str,
) -> str:
    lines = [
        "═" * 70,
        "  PRAESIDIUM FIRMWARE PROVISION BUNDLE — MANIFEST",
        "═" * 70,
        "",
        f"  Role:              {role.upper()}",
        f"  Hostname:          {hostname}",
        f"  Firmware version:  {firmware_version}",
        f"  Generated at:      {generated_at}",
        f"  Alembic head:      {alembic_head}",
        f"  Bundle assembled:  {datetime.now(timezone.utc).isoformat()}",
        "",
        "  File checksums (SHA-256):",
        f"  {script_sha256}  {role}-provision.sh",
        f"  {env_sha256}  .env.bootstrap",
        "",
        "  Verify after extraction:",
        f"  sha256sum -c <(grep -E 'provision|bootstrap' MANIFEST.txt | awk '{{print $1\" \"$2}}')",
        "",
        "  Deploy order reminder:",
        "  1. MAIN-DMZ-RPRX-01  (rprx)   — ALWAYS FIRST",
        "  2. MAIN-PRD-DB-01    (db)      — data layer before app",
        "  3. MAIN-PRD-PROC-01  (proc)    — workers before WEB reads",
        "  4. MAIN-PRD-WEB-01   (web)     — primary app server",
        "  5. MAIN-PRD-FBRG-01  (fbrg)   — file bridge",
        "  6. MAIN-PRD-WSS-01   (wss)    — Whisper STT (optional)",
        "",
        "  Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168",
        "  Confidential — Engineering Use Only",
        "═" * 70,
    ]
    return "\n".join(lines) + "\n"


def _add_string_to_tar(
    tar: tarfile.TarFile,
    arcname: str,
    content: str,
    mode: int = 0o644,
) -> None:
    data = content.encode("utf-8")
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mode = mode
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    tar.addfile(info, io.BytesIO(data))


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_alembic_head() -> str:
    """Get current Alembic head from the running app's migrations dir."""
    try:
        result = subprocess.run(
            ["alembic", "current"],
            capture_output=True, text=True, timeout=15,
            cwd=os.environ.get("APP_ROOT", "/app"),
        )
        lines = result.stdout.strip().splitlines()
        for line in lines:
            if "(head)" in line or len(line) >= 12:
                return line.split()[0][:12] if line.split() else "unknown"
        return "unknown"
    except Exception as exc:
        log.warning(f"Could not get alembic head: {exc}")
        return "unknown"


def _load_record_sync(record_id: str) -> Optional[dict]:
    """
    Load a config_generated_scripts record synchronously.
    RQ workers are sync; we use psycopg2 directly.
    Falls back gracefully if psycopg2 unavailable.
    """
    try:
        import psycopg2
        import psycopg2.extras

        # Convert asyncpg URL to psycopg2 URL
        db_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        conn = psycopg2.connect(db_url, connect_timeout=10)
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, source_vm, target_role, target_hostname, target_ip,
                           generated_at, generated_by, firmware_version,
                           script_content, env_template, notes
                    FROM config_generated_scripts
                    WHERE id = %s
                    """,
                    (record_id,)
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()
    except ImportError:
        log.warning("psycopg2 not available — cannot load record from DB")
        return None
    except Exception as exc:
        log.error(f"_load_record_sync failed: {exc}")
        return None


def _update_record_bundle_path(
    record_id: str,
    bundle_path: str,
    bundle_sha256: str,
) -> None:
    """Update config_generated_scripts.notes with the bundle path and sha256."""
    try:
        import psycopg2

        db_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        conn = psycopg2.connect(db_url, connect_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE config_generated_scripts
                    SET notes = COALESCE(notes || E'\n', '') ||
                                'bundle_path=' || %s || E'\n' ||
                                'bundle_sha256=' || %s
                    WHERE id = %s
                    """,
                    (bundle_path, bundle_sha256, record_id)
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.warning(f"Could not update bundle_path in record: {exc}")
        # Non-fatal — the bundle file exists; the record update is best-effort
