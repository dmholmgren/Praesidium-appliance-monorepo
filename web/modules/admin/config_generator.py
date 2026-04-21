"""
modules/admin/config_generator.py
Module 8 Component 0d — Scripts & Config Generator

Delivers:
  POST /admin/api/config/generate          — generate provision script + .env.bootstrap
                                             template for a given VM role; enqueues
                                             bundle assembler RQ job; records in
                                             config_generated_scripts table
  GET  /admin/api/config/generated         — list all generated script records
  GET  /admin/api/config/generated/{id}    — fetch a single generated script record
  GET  /admin/api/config/generated/{id}/download — stream the script as a file

All endpoints require platform admin auth (X-Platform-Admin header or internal IP).

The bundle assembler RQ job (jobs/config_bundle.py) is enqueued on every successful
generation call.  It assembles a .tar.gz containing:
  provision.sh (the generated role script)
  .env.bootstrap (secrets-blanked template)
  MANIFEST.txt  (SHA-256 checksums + firmware version + generated timestamp)

The bundle path is written back to config_generated_scripts.bundle_path after the
RQ job completes.

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import hashlib
import logging
import os
import textwrap
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.config_generator")

router = APIRouter()

# ── Platform admin auth (re-imported from platform.py pattern) ───────────────
PLATFORM_ADMIN_TOKEN = os.environ.get("PLATFORM_ADMIN_TOKEN", "")
FIRMWARE_VERSION = os.environ.get("FIRMWARE_VERSION", "series-2.0")
REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")

# Bundle output dir — must exist on the host FS (mounted into container or
# written to /opt/praesidium/bundles which is bind-mounted).
# Component 0d writes to this path; the RQ job on PROC-01 reads from it.
BUNDLE_DIR = os.environ.get("BUNDLE_DIR", "/opt/praesidium/bundles")


async def require_platform_admin(request: Request) -> str:
    """Bootstrap platform admin auth — identical pattern to platform.py."""
    token = request.headers.get("X-Platform-Admin", "")
    if not PLATFORM_ADMIN_TOKEN:
        client_ip = request.client.host if request.client else ""
        if not (client_ip.startswith("10.10.") or client_ip == "127.0.0.1"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="PLATFORM_ADMIN_TOKEN not configured and request not from internal network",
            )
        return "internal"
    if token != PLATFORM_ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid platform admin token",
        )
    return token


# ── Valid VM roles ────────────────────────────────────────────────────────────
VALID_ROLES = {"web", "web-dev", "rprx", "proc", "fbrg", "wss", "db"}

# Role → default IP mapping (matches middleware_servers seed from C0a migration)
ROLE_DEFAULT_IP: dict[str, str] = {
    "rprx":    "10.10.40.50",
    "web":     "10.10.60.10",
    "db":      "10.10.60.11",
    "proc":    "10.10.60.12",
    "fbrg":    "10.10.60.13",
    "wss":     "10.10.60.14",
    "web-dev": "10.10.60.15",
}

# Role → hostname pattern
ROLE_DEFAULT_HOSTNAME: dict[str, str] = {
    "rprx":    "MAIN-DMZ-RPRX-01",
    "web":     "MAIN-PRD-WEB-01",
    "db":      "MAIN-PRD-DB-01",
    "proc":    "MAIN-PRD-PROC-01",
    "fbrg":    "MAIN-PRD-FBRG-01",
    "wss":     "MAIN-PRD-WSS-01",
    "web-dev": "MAIN-DEV-WEB-01",
}


# ── Pydantic models ───────────────────────────────────────────────────────────

class GenerateScriptRequest(BaseModel):
    target_role: str              # web | web-dev | rprx | proc | fbrg | wss | db
    target_hostname: Optional[str] = None
    target_ip: Optional[str] = None
    firmware_version: Optional[str] = None
    notes: Optional[str] = None
    # Optional env overrides — values stored in template only; never stored as plain text
    # The caller may pass non-secret values (IPs, hostnames) only.
    # Secrets (passwords, keys) are ALWAYS blanked in the stored template.
    env_overrides: Optional[dict] = None


# ── Bootstrap .env.bootstrap template builder ────────────────────────────────

def _build_env_bootstrap_template(
    role: str,
    hostname: str,
    ip: str,
    firmware_version: str,
    overrides: Optional[dict],
) -> str:
    """
    Build the .env.bootstrap template for the given role.
    The bootstrap .env holds exactly 6 values that are needed before the
    database is reachable.  All other config lives in the database.
    Secrets are ALWAYS replaced with __FILL_IN__ placeholders.
    """
    overrides = overrides or {}

    db_host = overrides.get("DB_HOST", "10.10.60.11")
    redis_host = overrides.get("REDIS_HOST", "10.10.60.12")
    db_name = overrides.get("DB_NAME", "praesidium_hjmm")

    lines = [
        "# ════════════════════════════════════════════════════════════════════",
        f"# Praesidium Platform — Bootstrap .env",
        f"# Role:              {role.upper()}",
        f"# Hostname:          {hostname}",
        f"# IP:                {ip}",
        f"# Firmware version:  {firmware_version}",
        f"# Generated:         {datetime.now(timezone.utc).isoformat()}",
        "#",
        "# IMPORTANT: This file holds ONLY the 6 bootstrap values needed",
        "# before the database is reachable.  All other config lives in the DB.",
        "# Written ONCE at first provision.  Never overwritten by firmware ops.",
        "#",
        "# Replace every __FILL_IN__ with the real value before first boot.",
        "# ════════════════════════════════════════════════════════════════════",
        "",
        "# 1. Database URL (PgBouncer port 6432)",
        f"DATABASE_URL=postgresql+asyncpg://praesidium_db:__FILL_IN__@{db_host}:6432/{db_name}",
        "",
        "# 2. Redis URL",
        f"REDIS_URL=redis://{redis_host}:6379/0",
        "",
        "# 3. App secret key — generate: openssl rand -hex 32",
        "SECRET_KEY=__FILL_IN__",
        "",
        "# 4. File bridge shared secret",
        "BRIDGE_SECRET=__FILL_IN__",
        "",
        "# 5. Platform admin password hash — generate via:",
        "#    python3 -c \"import bcrypt; print(bcrypt.hashpw(b'yourpass', bcrypt.gensalt()).decode())\"",
        "PLATFORM_ADMIN_PASSWORD_HASH=__FILL_IN__",
        "",
        "# 6. OpenAI API key (for embeddings — text-embedding-3-small)",
        "OPENAI_API_KEY=__FILL_IN__",
        "",
        "# ── Role-specific additions ──────────────────────────────────────────",
    ]

    if role in ("rprx",):
        lines += [
            "# RPRX sidecar token — set after first admin login",
            "SIDECAR_TOKEN=__FILL_IN_AFTER_FIRST_LOGIN__",
            "",
            "# Nginx: downstream WEB-01 IP",
            f"WEB_BACKEND_IP={overrides.get('WEB_BACKEND_IP', '10.10.60.10')}",
        ]

    if role in ("web", "web-dev"):
        lines += [
            "# SIDECAR proxy token — set after RPRX sidecar is live",
            "SIDECAR_TOKEN=__FILL_IN_AFTER_RPRX_LIVE__",
            f"SIDECAR_URL=http://{overrides.get('SIDECAR_HOST', '10.10.40.50')}:8099",
            "",
            "# Firmware bundle path",
            f"FIRMWARE_VERSION={firmware_version}",
            "BUNDLE_DIR=/opt/praesidium/bundles",
        ]

    if role in ("proc",):
        lines += [
            "# RQ worker queues",
            "RQ_QUEUES=default,billing,dms,court,ediscovery,intelligence",
            "WORKER_REPLICAS=4",
        ]

    if role in ("fbrg",):
        lines += [
            "# CIFS credentials — keep this file at 600",
            "CIFS_SHARE_PATH=__FILL_IN__",
            "CIFS_DOMAIN=__FILL_IN__",
            "CIFS_USERNAME=__FILL_IN__",
            "CIFS_PASSWORD=__FILL_IN__",
        ]

    if role in ("wss",):
        lines += [
            "WHISPER_MODEL=large-v3",
            "WHISPER_DEVICE=cpu",
            "WHISPER_COMPUTE_TYPE=int8",
        ]

    return "\n".join(lines) + "\n"


# ── Provision script lookup ───────────────────────────────────────────────────

# Map role → canonical script filename (v3.0 scripts delivered by C0d)
_SCRIPT_MAP: dict[str, str] = {
    "rprx":    "01-praesidium-rprx.sh",
    "web":     "02-praesidium-web.sh",
    "db":      "03-praesidium-db.sh",
    "proc":    "04-praesidium-proc.sh",
    "fbrg":    "05-praesidium-fbrg.sh",
    "wss":     "06-praesidium-wss.sh",
    "web-dev": "02-praesidium-web.sh",  # same script, different env
}

# Script search paths — checked in order
_SCRIPT_SEARCH_DIRS = [
    "/opt/praesidium/scripts",
    "/app/scripts",
    "/opt/praesidium",
]


def _load_provision_script(role: str) -> str:
    """
    Load the provision script for the given role from the filesystem.
    Returns the script content as a string.
    Raises FileNotFoundError if not found in any search path.
    """
    filename = _SCRIPT_MAP.get(role, f"{role}-provision.sh")
    for d in _SCRIPT_SEARCH_DIRS:
        path = os.path.join(d, filename)
        if os.path.isfile(path):
            with open(path, "r") as fh:
                return fh.read()
    raise FileNotFoundError(
        f"Provision script '{filename}' not found in {_SCRIPT_SEARCH_DIRS}"
    )


def _script_sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


# ── API endpoints ─────────────────────────────────────────────────────────────

@router.post("/admin/api/config/generate", status_code=202)
async def generate_config(
    body: GenerateScriptRequest,
    request: Request,
    _: str = Depends(require_platform_admin),
):
    """
    Generate a provision script + .env.bootstrap template for a given VM role.

    Steps:
      1. Validate role
      2. Load the v3.0 provision script from /opt/praesidium/scripts/
      3. Build the .env.bootstrap template (secrets blanked)
      4. Insert record into config_generated_scripts
      5. Enqueue bundle assembler RQ job
      6. Return the record ID + script preview

    The provision script is stored verbatim.  The .env.bootstrap template has
    all secret values replaced with __FILL_IN__ placeholders.
    """
    if body.target_role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid target_role '{body.target_role}'. "
                   f"Must be one of: {sorted(VALID_ROLES)}",
        )

    role = body.target_role
    hostname = body.target_hostname or ROLE_DEFAULT_HOSTNAME.get(role, f"MAIN-PRD-{role.upper()}-01")
    ip = body.target_ip or ROLE_DEFAULT_IP.get(role, "0.0.0.0")
    fw_version = body.firmware_version or FIRMWARE_VERSION

    # Load provision script
    try:
        script_content = _load_provision_script(role)
    except FileNotFoundError as exc:
        log.warning(f"Config generate: script not found for role={role}: {exc}")
        # Return a clear, actionable error — do NOT silently serve a blank template
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Provision script for role '{role}' not found. "
                "Ensure v3.0 scripts are deployed to /opt/praesidium/scripts/ "
                "on WEB-01 per the Component 0d install guide."
            ),
        )

    # Build .env.bootstrap template
    env_template = _build_env_bootstrap_template(
        role=role,
        hostname=hostname,
        ip=ip,
        firmware_version=fw_version,
        overrides=body.env_overrides,
    )

    script_hash = _script_sha256(script_content)

    # Insert into config_generated_scripts
    generated_by = request.headers.get("X-Platform-Admin", "internal")

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
            INSERT INTO config_generated_scripts
              (id, source_vm, target_role, target_hostname, target_ip,
               generated_by, firmware_version, script_content, env_template, notes)
            VALUES
              (uuid_generate_v4()::text,
               :source_vm, :target_role, :target_hostname, :target_ip,
               :generated_by, :firmware_version, :script_content, :env_template, :notes)
            """),
            {
                "source_vm":       "MAIN-PRD-WEB-01",
                "target_role":     role,
                "target_hostname": hostname,
                "target_ip":       ip,
                "generated_by":    generated_by,
                "firmware_version": fw_version,
                "script_content":  script_content,
                "env_template":    env_template,
                "notes":           body.notes,
            }
        )
        # Fetch the newly inserted ID
        result = await session.execute(
            text("""
            SELECT id FROM config_generated_scripts
            WHERE source_vm = 'MAIN-PRD-WEB-01'
              AND target_role = :role
              AND generated_by = :generated_by
            ORDER BY generated_at DESC
            LIMIT 1
            """),
            {"role": role, "generated_by": generated_by}
        )
        row = result.fetchone()
        record_id = row[0] if row else None
        await session.commit()

    # Enqueue bundle assembler RQ job
    job_id = _enqueue_bundle_job(record_id=record_id, role=role, hostname=hostname)

    log.info(
        f"Config generated: role={role} hostname={hostname} "
        f"record_id={record_id} bundle_job={job_id}"
    )

    return {
        "record_id": record_id,
        "target_role": role,
        "target_hostname": hostname,
        "target_ip": ip,
        "firmware_version": fw_version,
        "script_sha256": script_hash,
        "env_template_lines": len(env_template.splitlines()),
        "bundle_job_id": job_id,
        "status": "queued",
        "message": (
            f"Script generated for role '{role}' ({hostname}). "
            "Bundle assembler job queued. "
            f"Download: GET /admin/api/config/generated/{record_id}/download"
        ),
    }


@router.get("/admin/api/config/generated")
async def list_generated_scripts(
    _: str = Depends(require_platform_admin),
    role: Optional[str] = None,
    limit: int = 50,
):
    """List all generated script records, optionally filtered by role."""
    where = "WHERE target_role = :role" if role else ""
    params: dict = {"limit": limit}
    if role:
        params["role"] = role

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(f"""
            SELECT id, source_vm, target_role, target_hostname, target_ip,
                   generated_at, generated_by, firmware_version, notes,
                   LENGTH(script_content) AS script_bytes,
                   LENGTH(env_template)   AS env_bytes
            FROM config_generated_scripts
            {where}
            ORDER BY generated_at DESC
            LIMIT :limit
            """),
            params
        )
        rows = result.mappings().fetchall()
    return {"records": [dict(r) for r in rows], "count": len(rows)}


@router.get("/admin/api/config/generated/{record_id}")
async def get_generated_script(
    record_id: str,
    _: str = Depends(require_platform_admin),
):
    """Fetch a single generated script record including full content."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT id, source_vm, target_role, target_hostname, target_ip,
                   generated_at, generated_by, firmware_version,
                   script_content, env_template, notes
            FROM config_generated_scripts
            WHERE id = :id
            """),
            {"id": record_id}
        )
        row = result.mappings().fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Generated script record '{record_id}' not found",
        )
    return dict(row)


@router.get("/admin/api/config/generated/{record_id}/download")
async def download_generated_script(
    record_id: str,
    file: str = "script",  # script | env | both
    _: str = Depends(require_platform_admin),
):
    """
    Stream the provision script or .env.bootstrap template as a file download.
    ?file=script  → provision.sh
    ?file=env     → .env.bootstrap template
    ?file=both    → not supported via this endpoint; use the RQ bundle instead
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT target_role, target_hostname, generated_at,
                   script_content, env_template
            FROM config_generated_scripts
            WHERE id = :id
            """),
            {"id": record_id}
        )
        row = result.mappings().fetchone()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Generated script record '{record_id}' not found",
        )

    if file == "env":
        content = row["env_template"]
        filename = f".env.bootstrap.{row['target_role']}.template"
        media_type = "text/plain"
    elif file == "script":
        content = row["script_content"]
        filename = f"{row['target_role']}-provision.sh"
        media_type = "application/x-sh"
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="?file must be 'script' or 'env'",
        )

    return Response(
        content=content.encode(),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Generated-At": str(row["generated_at"]),
            "X-Target-Role": row["target_role"],
            "X-Target-Hostname": row["target_hostname"] or "",
        },
    )


# ── RQ job enqueue helper ─────────────────────────────────────────────────────

def _enqueue_bundle_job(
    record_id: Optional[str],
    role: str,
    hostname: str,
) -> Optional[str]:
    """
    Enqueue the bundle assembler RQ job.
    Returns the RQ job ID, or None if Redis is unavailable (graceful degradation).
    The job assembles provision.sh + .env.bootstrap + MANIFEST.txt into a
    signed .tar.gz under BUNDLE_DIR.
    """
    try:
        import redis
        from rq import Queue

        r = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=3)
        r.ping()

        q = Queue("default", connection=r)
        job = q.enqueue(
            "jobs.config_bundle.assemble_bundle",
            record_id=record_id,
            role=role,
            hostname=hostname,
            bundle_dir=BUNDLE_DIR,
            job_timeout=300,
        )
        return job.id
    except Exception as exc:
        log.warning(
            f"Bundle assembler RQ enqueue failed (Redis unavailable?): {exc}. "
            "Script record saved — bundle will be assembled on next retry."
        )
        return None
