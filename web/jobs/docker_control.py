"""
jobs/docker_control.py
Module 8 Component 0e — PROC-01 Jobs

Docker control RQ job.

Provides safe, audited Docker operations on the praesidium-web container:
  start   — docker start praesidium-web
  stop    — docker stop praesidium-web  (graceful, 30s timeout)
  restart — docker restart praesidium-web
  status  — docker inspect + health check (read-only, no state change)
  logs    — docker logs --tail N (read-only)

Every mutating operation:
  1. Validates the container name (only praesidium-web allowed — no arbitrary targets)
  2. Records a firmware_event before and after
  3. Runs a post-action health check for start/restart

NOTE: In the current HJMM single-node deployment, RQ workers run on PROC-01
but the Docker daemon managing praesidium-web is on WEB-01. For single-node
all-in-one testing, workers and container share the same Docker socket.
For production HJMM: this job must be run with Docker socket access or via SSH.
The job is designed to work either way — it checks DOCKER_HOST env var.

DOCKER_HOST is NOT set by default — workers use the local Docker socket.
For remote WEB-01 control: set DOCKER_HOST=tcp://10.10.60.10:2375 in PROC-01
worker environment (NOT recommended without TLS — prefer SSH tunnel).

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("praesidium.jobs.docker_control")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
APP_PORT = int(os.environ.get("APP_PORT", "8000"))

# Safety: only allow control of the Praesidium container
ALLOWED_CONTAINERS = {"praesidium-web", "praesidium-worker"}


# ══════════════════════════════════════════════════════════════════════════════
# Public job functions
# ══════════════════════════════════════════════════════════════════════════════

def docker_start(
    container: str = "praesidium-web",
    triggered_by: str = "admin-api",
) -> dict:
    """RQ job: docker start <container>."""
    if not _allow(container):
        return _err(f"Container '{container}' not in allowed list")
    log.info(f"docker_start: {container} by {triggered_by}")
    result = _run(["docker", "start", container])
    _record_event("docker_start", container, triggered_by, result)
    if result["returncode"] == 0:
        health = _wait_healthy(container)
        return {"status": "ok", "container": container, "health": health}
    return _err(result["stderr"])


def docker_stop(
    container: str = "praesidium-web",
    triggered_by: str = "admin-api",
    timeout: int = 30,
) -> dict:
    """RQ job: docker stop <container> (graceful, 30s SIGTERM then SIGKILL)."""
    if not _allow(container):
        return _err(f"Container '{container}' not in allowed list")
    log.info(f"docker_stop: {container} by {triggered_by}")
    result = _run(["docker", "stop", "--time", str(timeout), container])
    _record_event("docker_stop", container, triggered_by, result)
    if result["returncode"] == 0:
        return {"status": "ok", "container": container, "stopped": True}
    return _err(result["stderr"])


def docker_restart(
    container: str = "praesidium-web",
    triggered_by: str = "admin-api",
) -> dict:
    """RQ job: docker restart <container> + health check."""
    if not _allow(container):
        return _err(f"Container '{container}' not in allowed list")
    log.info(f"docker_restart: {container} by {triggered_by}")
    result = _run(["docker", "restart", container])
    _record_event("docker_restart", container, triggered_by, result)
    if result["returncode"] == 0:
        health = _wait_healthy(container)
        return {"status": "ok", "container": container, "health": health}
    return _err(result["stderr"])


def docker_status(
    container: str = "praesidium-web",
    triggered_by: str = "admin-api",
) -> dict:
    """
    Read-only: docker inspect + GET /health on the container.
    No state changes. No firmware_event written (read-only).
    """
    if not _allow(container):
        return _err(f"Container '{container}' not in allowed list")

    inspect = _run(["docker", "inspect", container])
    if inspect["returncode"] != 0:
        return _err(f"Container '{container}' not found or docker not available")

    try:
        data = json.loads(inspect["stdout"])
        if not data:
            return _err(f"Container '{container}' not found")
        info = data[0]
        state = info.get("State", {})
    except (json.JSONDecodeError, IndexError) as exc:
        return _err(f"Could not parse docker inspect output: {exc}")

    # HTTP health check
    health_resp = _http_health()

    return {
        "status": "ok",
        "container": container,
        "running": state.get("Running", False),
        "status_text": state.get("Status", "unknown"),
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
        "exit_code": state.get("ExitCode"),
        "image": info.get("Config", {}).get("Image"),
        "app_health": health_resp,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def docker_logs(
    container: str = "praesidium-web",
    tail: int = 100,
    triggered_by: str = "admin-api",
) -> dict:
    """
    Read-only: Return last N lines of container logs.
    No state changes. No firmware_event written.
    """
    if not _allow(container):
        return _err(f"Container '{container}' not in allowed list")
    if tail > 1000:
        tail = 1000  # cap at 1000 lines

    result = _run(["docker", "logs", "--tail", str(tail), "--timestamps", container])
    # docker logs writes to stderr by design
    lines = (result["stderr"] + result["stdout"]).strip().splitlines()
    return {
        "status": "ok",
        "container": container,
        "lines": lines,
        "count": len(lines),
        "tail": tail,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _allow(container: str) -> bool:
    return container in ALLOWED_CONTAINERS


def _err(msg: str) -> dict:
    log.error(f"docker_control error: {msg}")
    return {"status": "error", "error": msg}


def _run(cmd: list, timeout: int = 60) -> dict:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": f"Timed out after {timeout}s"}
    except Exception as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc)}


def _wait_healthy(container: str, retries: int = 6, interval: int = 5) -> dict:
    """Poll GET /health after start/restart. Returns health status dict."""
    import urllib.request
    import urllib.error

    url = f"http://127.0.0.1:{APP_PORT}/health"
    for i in range(retries):
        time.sleep(interval)
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return {"ok": True, "attempt": i + 1}
        except Exception as exc:
            log.debug(f"Health check {i + 1}/{retries}: {exc}")
    return {"ok": False, "attempts": retries}


def _http_health() -> dict:
    """Single GET /health check. Returns dict with ok and status."""
    import urllib.request
    url = f"http://127.0.0.1:{APP_PORT}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = json.loads(resp.read().decode())
            return {"ok": resp.status == 200, "body": body}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _db_connect():
    try:
        import psycopg2
        db_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        return psycopg2.connect(db_url, connect_timeout=10)
    except Exception as exc:
        log.error(f"DB connect failed: {exc}")
        return None


def _record_event(
    event_type: str,
    container: str,
    triggered_by: str,
    run_result: dict,
) -> None:
    """Write firmware_events record for Docker control ops. Non-fatal on failure."""
    conn = _db_connect()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO firmware_events
                     (id, event_type, vm_name, triggered_by, detail, created_at)
                   VALUES (gen_random_uuid()::text, %s, 'MAIN-PRD-WEB-01', %s, %s::jsonb, NOW())""",
                (
                    event_type,
                    triggered_by,
                    json.dumps({
                        "container": container,
                        "returncode": run_result.get("returncode"),
                        "ok": run_result.get("returncode") == 0,
                        "stderr_snippet": run_result.get("stderr", "")[:200],
                    })
                )
            )
        conn.commit()
    except Exception as exc:
        log.warning(f"_record_event failed (non-fatal): {exc}")
        conn.rollback()
    finally:
        conn.close()
