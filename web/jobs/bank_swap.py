"""
jobs/bank_swap.py
Module 8 Component 0e — PROC-01 Jobs

Bank swap RQ job — Cisco dual-flash model.

State machine (enforced in DB, not just code):
  IDLE       → bank_set       → PENDING_CONFIRM
  PENDING_CONFIRM → confirmed → REBUILDING
  REBUILDING → health_ok      → ACTIVE (commit)
  REBUILDING → health_fail    → ROLLED_BACK (auto-rollback)
  ACTIVE     → (next cycle)   → IDLE (previous bank becomes standby)

Every bank operation auto-creates a config_backup BEFORE any change.
All state transitions are recorded in firmware_events.

Functions exposed to RQ:
  set_pending_bank(vm_name, target_bank, triggered_by)
  confirm_bank_swap(vm_name, target_bank, confirmed_by)
  execute_bank_rebuild(vm_name, target_bank, triggered_by)
  rollback_bank(vm_name, triggered_by, reason)

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("praesidium.jobs.bank_swap")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")

# Health check retry settings
HEALTH_CHECK_RETRIES = int(os.environ.get("BANK_SWAP_HEALTH_RETRIES", "6"))
HEALTH_CHECK_INTERVAL = int(os.environ.get("BANK_SWAP_HEALTH_INTERVAL", "10"))  # seconds
APP_PORT = int(os.environ.get("APP_PORT", "8000"))


# ══════════════════════════════════════════════════════════════════════════════
# Public job functions (called by RQ)
# ══════════════════════════════════════════════════════════════════════════════

def set_pending_bank(
    vm_name: str,
    target_bank: str,
    triggered_by: str = "admin-api",
) -> dict:
    """
    RQ job: Mark target_bank as pending_active for vm_name.

    Prerequisites enforced:
      - target_bank must be bank_a or bank_b
      - target_bank must NOT already be is_active
      - A config_backup is created before any state change

    This does NOT rebuild the container. It only sets pending_active = true
    and waits for confirm_bank_swap() to be called.
    """
    log.info(f"set_pending_bank: vm={vm_name} bank={target_bank} by={triggered_by}")

    if target_bank not in ("bank_a", "bank_b"):
        return _err(f"Invalid bank '{target_bank}' — must be bank_a or bank_b")

    conn = _db_connect()
    if not conn:
        return _err("DB connection failed")

    try:
        with conn.cursor() as cur:
            # Validate current state
            cur.execute(
                "SELECT is_active, pending_active FROM firmware_banks "
                "WHERE vm_name = %s AND bank = %s",
                (vm_name, target_bank)
            )
            row = cur.fetchone()
            if not row:
                return _err(f"No firmware_bank record for {vm_name}/{target_bank}")
            is_active, pending_active = row
            if is_active:
                return _err(f"{target_bank} is already the active bank for {vm_name}")
            if pending_active:
                return _err(f"{target_bank} already has pending_active=true for {vm_name}")

        # Auto-create config_backup before any bank operation
        backup_id = _create_config_backup(
            conn=conn,
            label=f"pre-bank-swap-{vm_name}-{target_bank}",
            triggered_by=triggered_by,
            pre_event_type="bank_set",
        )

        with conn.cursor() as cur:
            # Clear any stale pending flags for this VM
            cur.execute(
                "UPDATE firmware_banks SET pending_active = false "
                "WHERE vm_name = %s",
                (vm_name,)
            )
            # Set target bank as pending
            cur.execute(
                "UPDATE firmware_banks SET pending_active = true "
                "WHERE vm_name = %s AND bank = %s",
                (vm_name, target_bank)
            )
            # Record event
            _insert_event(cur, event_type="bank_set", vm_name=vm_name,
                          triggered_by=triggered_by,
                          detail={"target_bank": target_bank, "backup_id": backup_id})
        conn.commit()

        log.info(f"set_pending_bank OK: {vm_name}/{target_bank} pending — awaiting confirm")
        return {
            "status": "ok",
            "vm_name": vm_name,
            "target_bank": target_bank,
            "state": "pending_confirm",
            "backup_id": backup_id,
            "message": (
                f"{target_bank} is pending confirmation for {vm_name}. "
                "Call confirm_bank_swap() to proceed with rebuild."
            ),
        }
    except Exception as exc:
        conn.rollback()
        log.error(f"set_pending_bank failed: {exc}")
        return _err(str(exc))
    finally:
        conn.close()


def confirm_bank_swap(
    vm_name: str,
    target_bank: str,
    confirmed_by: str = "admin-api",
) -> dict:
    """
    RQ job: Confirm the pending bank swap and enqueue the rebuild job.

    This is the manual confirmation gate — no rebuild happens without it.
    After confirmation, execute_bank_rebuild() is enqueued on the RQ default queue.
    """
    log.info(f"confirm_bank_swap: vm={vm_name} bank={target_bank} by={confirmed_by}")

    conn = _db_connect()
    if not conn:
        return _err("DB connection failed")

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pending_active FROM firmware_banks "
                "WHERE vm_name = %s AND bank = %s",
                (vm_name, target_bank)
            )
            row = cur.fetchone()
            if not row or not row[0]:
                return _err(
                    f"{target_bank} is not in pending_active state for {vm_name}. "
                    "Call set_pending_bank() first."
                )

            cur.execute(
                """UPDATE firmware_banks
                   SET confirmed_at = NOW(), confirmed_by = %s
                   WHERE vm_name = %s AND bank = %s""",
                (confirmed_by, vm_name, target_bank)
            )
            _insert_event(cur, event_type="bank_confirmed", vm_name=vm_name,
                          triggered_by=confirmed_by,
                          detail={"target_bank": target_bank})
        conn.commit()

        # Enqueue the rebuild job
        rebuild_job_id = _enqueue_rebuild(vm_name, target_bank, confirmed_by)

        log.info(f"confirm_bank_swap OK: {vm_name}/{target_bank} confirmed — rebuild enqueued {rebuild_job_id}")
        return {
            "status": "ok",
            "vm_name": vm_name,
            "target_bank": target_bank,
            "state": "rebuilding",
            "rebuild_job_id": rebuild_job_id,
            "message": f"Bank swap confirmed. Rebuild job enqueued: {rebuild_job_id}",
        }
    except Exception as exc:
        conn.rollback()
        log.error(f"confirm_bank_swap failed: {exc}")
        return _err(str(exc))
    finally:
        conn.close()


def execute_bank_rebuild(
    vm_name: str,
    target_bank: str,
    triggered_by: str = "admin-api",
) -> dict:
    """
    RQ job: Rebuild the Docker container from the target bank's firmware path,
    run health checks, and commit or rollback.

    Steps:
      1. Locate /opt/praesidium/firmware/{target_bank}/
      2. docker compose build --no-cache (in that directory)
      3. docker compose up -d
      4. Health check loop (HEALTH_CHECK_RETRIES × HEALTH_CHECK_INTERVAL sec)
      5a. Health OK  → commit: set is_active=true, clear pending_active
      5b. Health FAIL → rollback: restart previous bank, record rollback event

    This job runs on PROC-01 workers but targets the WEB-01 Docker daemon via
    SSH or Docker socket. In the current deployment (single-node), it runs
    docker commands that target the local praesidium-web container.

    NOTE: In a multi-node deployment, this job would SSH to the target VM.
    For HJMM single-node: docker commands run on the worker host directly.
    """
    log.info(f"execute_bank_rebuild: vm={vm_name} bank={target_bank} by={triggered_by}")

    firmware_path = f"/opt/praesidium/firmware/{target_bank}"
    if not os.path.isdir(firmware_path):
        return _err(
            f"Firmware path '{firmware_path}' does not exist. "
            "Ensure firmware bundle is extracted to this path before rebuild."
        )

    conn = _db_connect()
    if not conn:
        return _err("DB connection failed")

    try:
        # Record rebuild start
        with conn.cursor() as cur:
            _insert_event(cur, event_type="rebuild", vm_name=vm_name,
                          triggered_by=triggered_by,
                          detail={"target_bank": target_bank, "firmware_path": firmware_path,
                                  "phase": "start"})
        conn.commit()

        # Docker rebuild
        rebuild_result = _docker_rebuild(firmware_path, vm_name)
        if not rebuild_result["ok"]:
            # Rollback immediately
            rollback_result = rollback_bank(
                vm_name=vm_name, triggered_by="auto-rollback",
                reason=f"Docker rebuild failed: {rebuild_result['error']}"
            )
            return {
                "status": "error",
                "phase": "rebuild_failed",
                "error": rebuild_result["error"],
                "rollback": rollback_result,
            }

        # Health check loop
        health_ok = _health_check_loop(vm_name)

        if health_ok:
            # Commit: activate target bank, deactivate previous
            _commit_bank_swap(conn, vm_name, target_bank, triggered_by)
            log.info(f"execute_bank_rebuild: COMMITTED {vm_name}/{target_bank}")
            return {
                "status": "ok",
                "phase": "committed",
                "vm_name": vm_name,
                "active_bank": target_bank,
                "message": f"Bank swap complete. {target_bank} is now active for {vm_name}.",
            }
        else:
            # Auto-rollback
            rollback_result = rollback_bank(
                vm_name=vm_name, triggered_by="auto-rollback",
                reason="Health checks failed after rebuild"
            )
            return {
                "status": "error",
                "phase": "health_check_failed",
                "error": "Container did not pass health checks after rebuild",
                "rollback": rollback_result,
            }
    except Exception as exc:
        conn.rollback()
        log.error(f"execute_bank_rebuild exception: {exc}")
        return _err(str(exc))
    finally:
        conn.close()


def rollback_bank(
    vm_name: str,
    triggered_by: str = "auto-rollback",
    reason: str = "manual rollback",
) -> dict:
    """
    RQ job: Roll back to the previously active bank.

    Finds the bank that is NOT pending_active and NOT currently active
    (i.e., the last-known-good bank) and restarts Docker from it.
    Records a bank_rollback event.
    """
    log.warning(f"rollback_bank: vm={vm_name} by={triggered_by} reason={reason}")

    conn = _db_connect()
    if not conn:
        return _err("DB connection failed")

    try:
        with conn.cursor() as cur:
            # Find the currently active bank (the one to roll back to)
            cur.execute(
                "SELECT bank FROM firmware_banks WHERE vm_name = %s AND is_active = true",
                (vm_name,)
            )
            row = cur.fetchone()
            fallback_bank = row[0] if row else "bank_a"

            # Clear all pending flags
            cur.execute(
                "UPDATE firmware_banks SET pending_active = false WHERE vm_name = %s",
                (vm_name,)
            )

            _insert_event(cur, event_type="bank_rollback", vm_name=vm_name,
                          triggered_by=triggered_by,
                          detail={"fallback_bank": fallback_bank, "reason": reason})
        conn.commit()

        # Restart Docker from fallback bank path
        fallback_path = f"/opt/praesidium/firmware/{fallback_bank}"
        if os.path.isdir(fallback_path):
            _docker_up(fallback_path, vm_name)
        else:
            # Fallback: just restart the existing container without rebuilding
            _run_cmd(["docker", "restart", "praesidium-web"])

        log.info(f"rollback_bank: {vm_name} rolled back to {fallback_bank}")
        return {
            "status": "ok",
            "vm_name": vm_name,
            "fallback_bank": fallback_bank,
            "reason": reason,
            "message": f"Rolled back to {fallback_bank} for {vm_name}.",
        }
    except Exception as exc:
        conn.rollback()
        log.error(f"rollback_bank failed: {exc}")
        return _err(str(exc))
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _err(msg: str) -> dict:
    log.error(f"bank_swap error: {msg}")
    return {"status": "error", "error": msg}


def _db_connect():
    """Synchronous psycopg2 connection for RQ worker use."""
    try:
        import psycopg2
        db_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        return psycopg2.connect(db_url, connect_timeout=10)
    except Exception as exc:
        log.error(f"DB connect failed: {exc}")
        return None


def _insert_event(cur, event_type: str, vm_name: str, triggered_by: str, detail: dict) -> None:
    """Insert a firmware_events row. Uses psycopg2 cursor (sync)."""
    import json
    cur.execute(
        """INSERT INTO firmware_events
             (id, event_type, vm_name, triggered_by, detail, created_at)
           VALUES
             (gen_random_uuid()::text, %s, %s, %s, %s::jsonb, NOW())""",
        (event_type, vm_name, triggered_by, json.dumps(detail))
    )


def _create_config_backup(
    conn,
    label: str,
    triggered_by: str,
    pre_event_type: str,
) -> Optional[str]:
    """
    Insert a config_backups record (lightweight — no archive yet; archive
    created by the config_backup job which is triggered separately).
    Returns the backup ID.
    """
    try:
        import subprocess
        # Get alembic head
        try:
            result = subprocess.run(
                ["alembic", "current"], capture_output=True, text=True, timeout=10,
                cwd=os.environ.get("APP_ROOT", "/app")
            )
            alembic_head = result.stdout.strip().split()[0][:12] if result.stdout.strip() else "unknown"
        except Exception:
            alembic_head = "unknown"

        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO config_backups
                     (id, label, triggered_by, is_auto, pre_event_type, alembic_head)
                   VALUES (gen_random_uuid()::text, %s, %s, true, %s, %s)
                   RETURNING id""",
                (label, triggered_by, pre_event_type, alembic_head)
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.warning(f"_create_config_backup failed (non-fatal): {exc}")
        return None


def _commit_bank_swap(conn, vm_name: str, target_bank: str, triggered_by: str) -> None:
    """Commit the bank swap: set target bank active, deactivate previous."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE firmware_banks SET is_active = false, pending_active = false "
            "WHERE vm_name = %s",
            (vm_name,)
        )
        cur.execute(
            "UPDATE firmware_banks SET is_active = true, pending_active = false "
            "WHERE vm_name = %s AND bank = %s",
            (vm_name, target_bank)
        )
        _insert_event(cur, event_type="bank_committed", vm_name=vm_name,
                      triggered_by=triggered_by,
                      detail={"active_bank": target_bank})
    conn.commit()


def _docker_rebuild(firmware_path: str, vm_name: str) -> dict:
    """Run docker compose build --no-cache in the firmware path."""
    log.info(f"Docker rebuild: {firmware_path}")
    result = _run_cmd(
        ["docker", "compose", "build", "--no-cache"],
        cwd=firmware_path,
        timeout=600,
    )
    if result["returncode"] != 0:
        return {"ok": False, "error": result["stderr"][:500]}
    up_result = _docker_up(firmware_path, vm_name)
    return up_result


def _docker_up(firmware_path: str, vm_name: str) -> dict:
    """Run docker compose up -d in the firmware path."""
    result = _run_cmd(
        ["docker", "compose", "up", "-d", "--force-recreate"],
        cwd=firmware_path,
        timeout=120,
    )
    if result["returncode"] != 0:
        return {"ok": False, "error": result["stderr"][:500]}
    return {"ok": True}


def _health_check_loop(vm_name: str) -> bool:
    """
    Poll GET /health on the app container until it returns 200 or retry limit.
    Returns True if healthy within the retry window, False otherwise.
    """
    import urllib.request
    import urllib.error

    url = f"http://127.0.0.1:{APP_PORT}/health"
    for attempt in range(1, HEALTH_CHECK_RETRIES + 1):
        time.sleep(HEALTH_CHECK_INTERVAL)
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    log.info(f"Health check OK on attempt {attempt}")
                    return True
        except Exception as exc:
            log.warning(f"Health check attempt {attempt}/{HEALTH_CHECK_RETRIES}: {exc}")
    log.error(f"Health checks exhausted after {HEALTH_CHECK_RETRIES} attempts")
    return False


def _run_cmd(
    cmd: list,
    cwd: Optional[str] = None,
    timeout: int = 60,
) -> dict:
    """Run a shell command. Returns {returncode, stdout, stderr}."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": f"Command timed out after {timeout}s"}
    except Exception as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc)}


def _enqueue_rebuild(vm_name: str, target_bank: str, triggered_by: str) -> Optional[str]:
    """Enqueue execute_bank_rebuild on the default RQ queue."""
    try:
        import redis
        from rq import Queue
        r = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=3)
        q = Queue("default", connection=r)
        job = q.enqueue(
            "jobs.bank_swap.execute_bank_rebuild",
            vm_name=vm_name,
            target_bank=target_bank,
            triggered_by=triggered_by,
            job_timeout=900,  # 15 min max for rebuild + health checks
        )
        return job.id
    except Exception as exc:
        log.error(f"Failed to enqueue rebuild job: {exc}")
        return None
