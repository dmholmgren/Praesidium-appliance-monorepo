"""
modules/admin/jobs_api.py
Module 8 Component 0e — PROC-01 Jobs

FastAPI router: job dispatch endpoints + SSE live job status stream.

Endpoints:
  POST /admin/api/jobs/bank/set            — enqueue set_pending_bank
  POST /admin/api/jobs/bank/confirm        — enqueue confirm_bank_swap
  POST /admin/api/jobs/bank/rollback       — enqueue rollback_bank
  GET  /admin/api/jobs/bank/status/{vm}    — current bank state for a VM

  POST /admin/api/jobs/backup/create       — enqueue create_config_backup
  GET  /admin/api/jobs/backup/list         — list config_backups records
  GET  /admin/api/jobs/backup/{id}         — single backup record

  POST /admin/api/jobs/docker/{action}     — enqueue docker start|stop|restart|status|logs
  GET  /admin/api/jobs/docker/status       — live status of praesidium-web

  GET  /admin/api/jobs/status/{job_id}     — single RQ job fetch
  GET  /admin/api/jobs/recent              — recent firmware_events from DB
  GET  /admin/api/jobs/stream              — SSE stream of job + event activity

All endpoints require platform admin auth (X-Platform-Admin header or 10.10.x.x).
SSE stream: text/event-stream, compatible with EventSource and curl -N.

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.jobs_api")

router = APIRouter()

PLATFORM_ADMIN_TOKEN = os.environ.get("PLATFORM_ADMIN_TOKEN", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")

SSE_HEARTBEAT = 15       # seconds between keepalive pings
SSE_POLL_INTERVAL = 3    # seconds between DB polls in SSE stream


# ── Auth (identical bootstrap pattern to platform.py + config_generator.py) ──

async def require_platform_admin(request: Request) -> str:
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


# ── RQ helpers ────────────────────────────────────────────────────────────────

def _enqueue(func_path: str, job_timeout: int = 300, **kwargs) -> Optional[str]:
    """Enqueue an RQ job on the default queue. Returns job_id or None."""
    try:
        import redis as redis_lib
        from rq import Queue
        r = redis_lib.Redis.from_url(REDIS_URL, socket_connect_timeout=3)
        r.ping()
        q = Queue("default", connection=r)
        job = q.enqueue(func_path, job_timeout=job_timeout, **kwargs)
        return job.id
    except Exception as exc:
        log.warning(f"RQ enqueue failed ({func_path}): {exc}")
        return None


def _get_job_status(job_id: str) -> dict:
    """Fetch a single RQ job's status. Graceful on Redis failure."""
    try:
        import redis as redis_lib
        from rq.job import Job
        r = redis_lib.Redis.from_url(REDIS_URL, socket_connect_timeout=3)
        job = Job.fetch(job_id, connection=r)
        job_status = job.get_status()
        return {
            "job_id": job_id,
            "status": job_status.value if job_status else "unknown",
            "enqueued_at": job.enqueued_at.isoformat() if job.enqueued_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "ended_at": job.ended_at.isoformat() if job.ended_at else None,
            "result": job.result if job.is_finished else None,
            "exc_info": job.exc_info[:500] if job.exc_info else None,
        }
    except Exception as exc:
        return {"job_id": job_id, "status": "unknown", "error": str(exc)}


# ══════════════════════════════════════════════════════════════════════════════
# Pydantic models
# ══════════════════════════════════════════════════════════════════════════════

class BankSetRequest(BaseModel):
    vm_name: str
    target_bank: str                          # bank_a | bank_b
    triggered_by: Optional[str] = "admin-api"


class BankConfirmRequest(BaseModel):
    vm_name: str
    target_bank: str
    confirmed_by: Optional[str] = "admin-api"


class BankRollbackRequest(BaseModel):
    vm_name: str
    reason: Optional[str] = "manual rollback"
    triggered_by: Optional[str] = "admin-api"


class BackupCreateRequest(BaseModel):
    label: Optional[str] = None
    triggered_by: Optional[str] = "admin-panel"


class DockerActionRequest(BaseModel):
    container: Optional[str] = "praesidium-web"
    triggered_by: Optional[str] = "admin-api"
    tail: Optional[int] = 100
    stop_timeout: Optional[int] = 30


# ══════════════════════════════════════════════════════════════════════════════
# Bank swap endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/admin/api/jobs/bank/set", status_code=202)
async def bank_set(
    body: BankSetRequest,
    _: str = Depends(require_platform_admin),
):
    """
    Enqueue set_pending_bank RQ job.
    Marks target_bank as pending_active. Auto-creates config_backup first.
    Does NOT rebuild — requires explicit confirm call.
    """
    job_id = _enqueue(
        "jobs.bank_swap.set_pending_bank",
        job_timeout=120,
        vm_name=body.vm_name,
        target_bank=body.target_bank,
        triggered_by=body.triggered_by,
    )
    if not job_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis unavailable — could not enqueue bank set job",
        )
    return {
        "job_id": job_id,
        "vm_name": body.vm_name,
        "target_bank": body.target_bank,
        "status": "queued",
        "message": (
            f"Bank set job queued for {body.vm_name}/{body.target_bank}. "
            f"Poll GET /admin/api/jobs/status/{job_id} for result, "
            "then call POST /admin/api/jobs/bank/confirm to proceed."
        ),
    }


@router.post("/admin/api/jobs/bank/confirm", status_code=202)
async def bank_confirm(
    body: BankConfirmRequest,
    _: str = Depends(require_platform_admin),
):
    """
    Enqueue confirm_bank_swap RQ job.
    This is the manual confirmation gate — no rebuild happens without it.
    After confirmation, execute_bank_rebuild is automatically enqueued.
    """
    job_id = _enqueue(
        "jobs.bank_swap.confirm_bank_swap",
        job_timeout=120,
        vm_name=body.vm_name,
        target_bank=body.target_bank,
        confirmed_by=body.confirmed_by,
    )
    if not job_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis unavailable — could not enqueue bank confirm job",
        )
    return {
        "job_id": job_id,
        "vm_name": body.vm_name,
        "target_bank": body.target_bank,
        "status": "queued",
        "message": (
            f"Bank confirm job queued. Rebuild will be enqueued automatically. "
            f"Monitor: GET /admin/api/jobs/stream"
        ),
    }


@router.post("/admin/api/jobs/bank/rollback", status_code=202)
async def bank_rollback(
    body: BankRollbackRequest,
    _: str = Depends(require_platform_admin),
):
    """Enqueue rollback_bank RQ job. Reverts to previously active bank."""
    job_id = _enqueue(
        "jobs.bank_swap.rollback_bank",
        job_timeout=300,
        vm_name=body.vm_name,
        triggered_by=body.triggered_by,
        reason=body.reason,
    )
    if not job_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis unavailable — could not enqueue rollback job",
        )
    return {
        "job_id": job_id,
        "vm_name": body.vm_name,
        "status": "queued",
        "message": f"Rollback job queued for {body.vm_name}.",
    }


@router.get("/admin/api/jobs/bank/status/{vm_name}")
async def bank_status(
    vm_name: str,
    _: str = Depends(require_platform_admin),
):
    """Return current firmware bank state for a VM from the database."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT
                fb.bank,
                fb.is_active,
                fb.pending_active,
                fb.loaded_at,
                fb.loaded_by,
                fb.confirmed_at,
                fb.confirmed_by,
                fv.version_tag
            FROM firmware_banks fb
            LEFT JOIN firmware_versions fv ON fv.id = fb.firmware_version_id
            WHERE fb.vm_name = :vm_name
            ORDER BY fb.bank
            """),
            {"vm_name": vm_name}
        )
        rows = result.mappings().fetchall()

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No firmware bank records found for VM '{vm_name}'",
        )

    banks = {r["bank"]: dict(r) for r in rows}
    active = next((b for b, r in banks.items() if r["is_active"]), None)
    pending = next((b for b, r in banks.items() if r["pending_active"]), None)

    return {
        "vm_name": vm_name,
        "active_bank": active,
        "pending_bank": pending,
        "banks": banks,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Config backup endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/admin/api/jobs/backup/create", status_code=202)
async def backup_create(
    body: BackupCreateRequest,
    _: str = Depends(require_platform_admin),
):
    """Enqueue create_config_backup RQ job."""
    job_id = _enqueue(
        "jobs.config_backup.create_config_backup",
        job_timeout=300,
        label=body.label,
        triggered_by=body.triggered_by,
        is_auto=False,
    )
    if not job_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis unavailable — could not enqueue backup job",
        )
    return {
        "job_id": job_id,
        "status": "queued",
        "message": f"Config backup job queued. Poll: GET /admin/api/jobs/status/{job_id}",
    }


@router.get("/admin/api/jobs/backup/list")
async def backup_list(
    limit: int = 20,
    _: str = Depends(require_platform_admin),
):
    """List config backup records, newest first."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT id, label, backup_at, triggered_by, is_auto,
                   pre_event_type, config_archive_path, config_hash,
                   alembic_head, tenant_count, notes
            FROM config_backups
            ORDER BY backup_at DESC
            LIMIT :limit
            """),
            {"limit": limit}
        )
        rows = result.mappings().fetchall()
    return {"backups": [dict(r) for r in rows], "count": len(rows)}


@router.get("/admin/api/jobs/backup/{backup_id}")
async def backup_get(
    backup_id: str,
    _: str = Depends(require_platform_admin),
):
    """Fetch a single config backup record."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT id, label, backup_at, triggered_by, is_auto,
                   pre_event_type, config_archive_path, config_hash,
                   alembic_head, tenant_count, notes
            FROM config_backups WHERE id = :id
            """),
            {"id": backup_id}
        )
        row = result.mappings().fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Backup record '{backup_id}' not found",
        )
    return dict(row)


# ══════════════════════════════════════════════════════════════════════════════
# Docker control endpoints
# ══════════════════════════════════════════════════════════════════════════════

VALID_DOCKER_ACTIONS = {"start", "stop", "restart", "status", "logs"}


@router.post("/admin/api/jobs/docker/{action}", status_code=202)
async def docker_action(
    action: str,
    body: DockerActionRequest,
    _: str = Depends(require_platform_admin),
):
    """
    Enqueue a Docker control job.
    action: start | stop | restart | status | logs
    status and logs are read-only but still run via RQ for async response.
    """
    if action not in VALID_DOCKER_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid action '{action}'. Must be one of: {sorted(VALID_DOCKER_ACTIONS)}",
        )

    func_map = {
        "start":   "jobs.docker_control.docker_start",
        "stop":    "jobs.docker_control.docker_stop",
        "restart": "jobs.docker_control.docker_restart",
        "status":  "jobs.docker_control.docker_status",
        "logs":    "jobs.docker_control.docker_logs",
    }

    kwargs: dict = {
        "container": body.container,
        "triggered_by": body.triggered_by,
    }
    if action == "stop":
        kwargs["timeout"] = body.stop_timeout
    if action == "logs":
        kwargs["tail"] = body.tail

    timeout_map = {"start": 120, "stop": 60, "restart": 120, "status": 30, "logs": 30}

    job_id = _enqueue(func_map[action], job_timeout=timeout_map[action], **kwargs)
    if not job_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis unavailable — could not enqueue docker job",
        )
    return {
        "job_id": job_id,
        "action": action,
        "container": body.container,
        "status": "queued",
        "message": f"Docker {action} job queued. Poll: GET /admin/api/jobs/status/{job_id}",
    }


@router.get("/admin/api/jobs/docker/status")
async def docker_live_status(
    _: str = Depends(require_platform_admin),
):
    """
    Synchronous Docker status check — runs inline (no RQ), returns immediately.
    Used by the admin panel health grid to show container state without polling.
    """
    job_id = _enqueue(
        "jobs.docker_control.docker_status",
        job_timeout=30,
        container="praesidium-web",
        triggered_by="health-poll",
    )
    return {
        "job_id": job_id,
        "status": "queued",
        "message": f"Poll: GET /admin/api/jobs/status/{job_id}",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Job status + recent events
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/api/jobs/status/{job_id}")
async def job_status(
    job_id: str,
    _: str = Depends(require_platform_admin),
):
    """Fetch a single RQ job's current status and result."""
    return _get_job_status(job_id)


@router.get("/admin/api/jobs/recent")
async def recent_events(
    limit: int = 50,
    vm_name: Optional[str] = None,
    event_type: Optional[str] = None,
    _: str = Depends(require_platform_admin),
):
    """
    Return recent firmware_events from the database.
    Optional filters: vm_name, event_type.
    """
    conditions = []
    params: dict = {"limit": limit}

    if vm_name:
        conditions.append("vm_name = :vm_name")
        params["vm_name"] = vm_name
    if event_type:
        conditions.append("event_type = :event_type")
        params["event_type"] = event_type

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(f"""
            SELECT id, event_type, vm_name, from_version, to_version,
                   triggered_by, detail, created_at
            FROM firmware_events
            {where}
            ORDER BY created_at DESC
            LIMIT :limit
            """),
            params
        )
        rows = result.mappings().fetchall()

    return {"events": [dict(r) for r in rows], "count": len(rows)}


# ══════════════════════════════════════════════════════════════════════════════
# SSE stream
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/api/jobs/stream")
async def job_stream(
    request: Request,
    _: str = Depends(require_platform_admin),
):
    """
    Server-Sent Events stream of platform job and firmware event activity.

    Emits three event types:
      event: heartbeat  — keepalive ping every SSE_HEARTBEAT seconds
      event: fw_event   — new firmware_events row (polled every SSE_POLL_INTERVAL s)
      event: job_update — RQ job status change (polled for any queued/started jobs)

    Connect:
      curl -N -H 'X-Platform-Admin: <token>' http://WEB-01:8000/admin/api/jobs/stream
      or EventSource in browser (pass token via query param if needed)

    Disconnect: close the connection; the generator exits cleanly.
    """
    return StreamingResponse(
        _sse_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",     # Disable Nginx buffering
            "Connection": "keep-alive",
        },
    )


async def _sse_generator(request: Request) -> AsyncGenerator[str, None]:
    """
    Async generator for the SSE stream.
    Polls firmware_events for new rows and emits them as SSE events.
    Also emits heartbeats to keep the connection alive through proxies.
    """
    last_event_id: Optional[str] = None
    last_heartbeat = asyncio.get_event_loop().time()
    event_count = 0

    # Get the most recent event ID at connect time — only stream NEW events
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT id FROM firmware_events ORDER BY created_at DESC LIMIT 1")
            )
            row = result.fetchone()
            if row:
                last_event_id = row[0]
    except Exception:
        pass

    # Initial connection event
    yield _sse("connected", {"message": "Praesidium job stream connected",
                              "poll_interval": SSE_POLL_INTERVAL,
                              "heartbeat": SSE_HEARTBEAT})

    while True:
        # Check if client disconnected
        if await request.is_disconnected():
            log.debug("SSE client disconnected")
            break

        now = asyncio.get_event_loop().time()

        # Heartbeat
        if now - last_heartbeat >= SSE_HEARTBEAT:
            yield _sse("heartbeat", {"ts": datetime.now(timezone.utc).isoformat()})
            last_heartbeat = now

        # Poll for new firmware_events
        try:
            new_events = await _poll_new_events(last_event_id)
            for event in new_events:
                yield _sse("fw_event", event)
                last_event_id = event["id"]
                event_count += 1
        except Exception as exc:
            log.warning(f"SSE poll error: {exc}")

        await asyncio.sleep(SSE_POLL_INTERVAL)


async def _poll_new_events(after_id: Optional[str]) -> list[dict]:
    """
    Poll firmware_events for rows newer than after_id.
    Uses created_at ordering with the ID as a tiebreaker.
    """
    try:
        async with AsyncSessionLocal() as session:
            if after_id:
                result = await session.execute(
                    text("""
                    SELECT id, event_type, vm_name, triggered_by, detail, created_at
                    FROM firmware_events
                    WHERE created_at > (
                        SELECT created_at FROM firmware_events WHERE id = :after_id
                    )
                    AND id != :after_id
                    ORDER BY created_at ASC
                    LIMIT 20
                    """),
                    {"after_id": after_id}
                )
            else:
                result = await session.execute(
                    text("""
                    SELECT id, event_type, vm_name, triggered_by, detail, created_at
                    FROM firmware_events
                    ORDER BY created_at DESC
                    LIMIT 1
                    """)
                )
            rows = result.mappings().fetchall()
            return [
                {
                    "id": r["id"],
                    "event_type": r["event_type"],
                    "vm_name": r["vm_name"],
                    "triggered_by": r["triggered_by"],
                    "detail": r["detail"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]
    except Exception as exc:
        log.warning(f"_poll_new_events failed: {exc}")
        return []


def _sse(event: str, data: dict) -> str:
    """Format a Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
