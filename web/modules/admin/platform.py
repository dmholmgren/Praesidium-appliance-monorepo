"""
modules/admin/platform.py
Module 8 Component 0c — Platform Routing & Middleware Server API

Delivers:
  GET  /health                          — WEB-01 health (no auth — sidecar + monitoring)
  GET  /admin/api/middleware-servers    — list all middleware servers
  POST /admin/api/middleware-servers    — register new middleware server
  GET  /admin/api/middleware-servers/{id} — get single server
  PUT  /admin/api/middleware-servers/{id} — update server
  POST /admin/api/middleware-servers/{id}/ping — trigger live health check

  GET  /admin/api/sidecar/health        — proxy GET /health from RPRX sidecar
  GET  /admin/api/sidecar/storage       — proxy GET /storage/health from RPRX sidecar
  GET  /admin/api/sidecar/slots         — proxy GET /slots from RPRX sidecar
  POST /admin/api/sidecar/slots/default — proxy POST /slots/default to RPRX sidecar
  GET  /admin/api/sidecar/events        — proxy GET /events from RPRX sidecar
  GET  /admin/api/sidecar/nginx/status  — proxy GET /nginx/status from RPRX sidecar

All sidecar proxy endpoints require platform admin auth (X-Platform-Admin header).
Sidecar token is stored in SIDECAR_TOKEN env var (set after first sidecar login).

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

import asyncio
import logging
import os
import socket
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.platform")

router = APIRouter()

# ── Sidecar config ────────────────────────────────────────────────────────────
SIDECAR_URL = os.environ.get("SIDECAR_URL", "http://10.10.40.50:8099")
SIDECAR_TOKEN = os.environ.get("SIDECAR_TOKEN", "")
SIDECAR_TIMEOUT = 10.0

# ── Platform admin auth ───────────────────────────────────────────────────────
PLATFORM_ADMIN_TOKEN = os.environ.get("PLATFORM_ADMIN_TOKEN", "")


async def require_platform_admin(request: Request) -> str:
    """
    Simple platform admin auth for internal API endpoints.
    Checks X-Platform-Admin header against PLATFORM_ADMIN_TOKEN env var.
    Full admin panel auth (bcrypt + session) is built in Component 1.
    This is the bootstrap auth used by Component 0c endpoints only.
    """
    token = request.headers.get("X-Platform-Admin", "")
    if not PLATFORM_ADMIN_TOKEN:
        # Token not configured — allow from internal network only
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


# ── Models ────────────────────────────────────────────────────────────────────
class MiddlewareServerCreate(BaseModel):
    name: str
    ip: str
    port: int = 8000
    role: str  # web | web-dev | rprx | proc | fbrg | wss | db
    is_default: bool = False
    notes: Optional[str] = None


class MiddlewareServerUpdate(BaseModel):
    ip: Optional[str] = None
    port: Optional[int] = None
    status: Optional[str] = None  # active | draining | offline
    is_default: Optional[bool] = None
    notes: Optional[str] = None


class SetDefaultRequest(BaseModel):
    server_name: str
    upstream: str  # e.g. "10.10.60.10:8000"


# NOTE: /health endpoint is defined in app.py — not duplicated here.
# The existing endpoint returns 200 which satisfies the sidecar http_ok check.


# ── Middleware server API ─────────────────────────────────────────────────────
@router.get("/admin/api/middleware-servers")
async def list_middleware_servers(
    _: str = Depends(require_platform_admin),
):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT id, name, ip, port, role, status, is_default,
                   added_at, last_seen, notes
            FROM middleware_servers
            ORDER BY role, name
            """)
        )
        rows = result.mappings().fetchall()
    return {"servers": [dict(r) for r in rows]}


@router.post("/admin/api/middleware-servers", status_code=201)
async def create_middleware_server(
    body: MiddlewareServerCreate,
    _: str = Depends(require_platform_admin),
):
    async with AsyncSessionLocal() as session:
        # Check duplicate name
        existing = await session.execute(
            text("SELECT id FROM middleware_servers WHERE name = :name"),
            {"name": body.name}
        )
        if existing.fetchone():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Middleware server '{body.name}' already exists",
            )

        # If setting as default, clear existing defaults for this role
        if body.is_default:
            await session.execute(
                text("UPDATE middleware_servers SET is_default = false WHERE role = :role"),
                {"role": body.role}
            )

        await session.execute(
            text("""
            INSERT INTO middleware_servers
              (id, name, ip, port, role, status, is_default, notes)
            VALUES
              (uuid_generate_v4()::text, :name, :ip, :port, :role, 'active', :is_default, :notes)
            """),
            {
                "name": body.name,
                "ip": body.ip,
                "port": body.port,
                "role": body.role,
                "is_default": body.is_default,
                "notes": body.notes,
            }
        )

        # Seed firmware banks for new VM
        for bank in ("bank_a", "bank_b"):
            is_active = bank == "bank_a"
            await session.execute(
                text("""
                INSERT INTO firmware_banks (id, vm_name, bank, is_active, pending_active)
                VALUES (uuid_generate_v4()::text, :vm_name, :bank, :is_active, false)
                ON CONFLICT (vm_name, bank) DO NOTHING
                """),
                {"vm_name": body.name, "bank": bank, "is_active": is_active}
            )

        await session.commit()

    return {"ok": True, "name": body.name}


@router.get("/admin/api/middleware-servers/{server_id}")
async def get_middleware_server(
    server_id: str,
    _: str = Depends(require_platform_admin),
):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT ms.id, ms.name, ms.ip, ms.port, ms.role, ms.status,
                   ms.is_default, ms.added_at, ms.last_seen, ms.notes,
                   fb_a.firmware_version_id as bank_a_version,
                   fb_a.is_active as bank_a_active,
                   fb_b.firmware_version_id as bank_b_version,
                   fb_b.is_active as bank_b_active
            FROM middleware_servers ms
            LEFT JOIN firmware_banks fb_a
                   ON fb_a.vm_name = ms.name AND fb_a.bank = 'bank_a'
            LEFT JOIN firmware_banks fb_b
                   ON fb_b.vm_name = ms.name AND fb_b.bank = 'bank_b'
            WHERE ms.id = :id
            """),
            {"id": server_id}
        )
        row = result.mappings().fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Middleware server not found")
    return dict(row)


@router.put("/admin/api/middleware-servers/{server_id}")
async def update_middleware_server(
    server_id: str,
    body: MiddlewareServerUpdate,
    _: str = Depends(require_platform_admin),
):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clauses = ", ".join(f"{k} = :{k}" for k in updates)
    updates["id"] = server_id

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(f"UPDATE middleware_servers SET {set_clauses} WHERE id = :id"),
            updates
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Middleware server not found")
        await session.commit()

    return {"ok": True}


@router.post("/admin/api/middleware-servers/{server_id}/ping")
async def ping_middleware_server(
    server_id: str,
    _: str = Depends(require_platform_admin),
):
    """Live TCP + HTTP health check for a single middleware server."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT name, ip, port, role FROM middleware_servers WHERE id = :id"),
            {"id": server_id}
        )
        row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Middleware server not found")

    name, ip, port, role = row

    # TCP check
    port_ok = False
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=3.0
        )
        writer.close()
        await writer.wait_closed()
        port_ok = True
    except Exception:
        pass

    # HTTP /health check for app VMs
    http_ok = False
    if role in ("web", "web-dev", "fbrg", "wss"):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"http://{ip}:{port}/health")
                http_ok = resp.status_code in (200, 204)
        except Exception:
            pass

    # Update last_seen if port is reachable
    if port_ok:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("UPDATE middleware_servers SET last_seen = NOW() WHERE id = :id"),
                {"id": server_id}
            )
            await session.commit()

    return {
        "name": name,
        "ip": ip,
        "port": port,
        "port_ok": port_ok,
        "http_ok": http_ok,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Sidecar proxy endpoints ───────────────────────────────────────────────────
def _sidecar_headers() -> dict:
    """Build auth headers for sidecar requests."""
    if SIDECAR_TOKEN:
        return {"X-Sidecar-Token": SIDECAR_TOKEN}
    return {}


async def _sidecar_get(path: str) -> dict:
    """Proxy a GET request to the RPRX sidecar."""
    try:
        async with httpx.AsyncClient(timeout=SIDECAR_TIMEOUT) as client:
            resp = await client.get(
                f"{SIDECAR_URL}{path}",
                headers=_sidecar_headers(),
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"Sidecar returned {e.response.status_code}: {e.response.text[:200]}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Sidecar unreachable: {str(e)}",
        )


async def _sidecar_post(path: str, body: dict) -> dict:
    """Proxy a POST request to the RPRX sidecar."""
    try:
        async with httpx.AsyncClient(timeout=SIDECAR_TIMEOUT) as client:
            resp = await client.post(
                f"{SIDECAR_URL}{path}",
                json=body,
                headers=_sidecar_headers(),
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"Sidecar returned {e.response.status_code}: {e.response.text[:200]}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Sidecar unreachable: {str(e)}",
        )


@router.get("/admin/api/sidecar/health")
async def sidecar_health(_: str = Depends(require_platform_admin)):
    """Proxy VM health grid from RPRX sidecar."""
    return await _sidecar_get("/health")


@router.get("/admin/api/sidecar/storage")
async def sidecar_storage(_: str = Depends(require_platform_admin)):
    """Proxy storage health from RPRX sidecar."""
    return await _sidecar_get("/storage/health")


@router.get("/admin/api/sidecar/slots")
async def sidecar_slots(_: str = Depends(require_platform_admin)):
    """Proxy current slot map from RPRX sidecar."""
    return await _sidecar_get("/slots")


@router.post("/admin/api/sidecar/slots/default")
async def sidecar_set_default(
    body: SetDefaultRequest,
    _: str = Depends(require_platform_admin),
):
    """Set default upstream slot via RPRX sidecar."""
    result = await _sidecar_post(
        "/slots/default",
        {"server_name": body.server_name, "upstream": body.upstream}
    )

    # Record in firmware_events
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
            INSERT INTO firmware_events
              (event_type, vm_name, triggered_by, detail, created_at)
            VALUES
              ('slot_change', 'MAIN-DMZ-RPRX-01', 'admin-api',
               CAST(:detail AS jsonb), NOW())
            """),
            {"detail": f'{{"default_server": "{body.server_name}", "upstream": "{body.upstream}"}}'}
        )
        await session.commit()

    return result


@router.get("/admin/api/sidecar/events")
async def sidecar_events(_: str = Depends(require_platform_admin)):
    """Proxy event ring buffer from RPRX sidecar."""
    return await _sidecar_get("/events")


@router.get("/admin/api/sidecar/nginx/status")
async def sidecar_nginx_status(_: str = Depends(require_platform_admin)):
    """Proxy nginx stub_status from RPRX sidecar."""
    return await _sidecar_get("/nginx/status")
