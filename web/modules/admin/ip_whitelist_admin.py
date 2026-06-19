"""Platform Admin — appliance IP whitelist management.

CRUD over platform_ip_allowlist + a read-only PREVIEW of the rules that would
be rendered for each enforcement surface (nginx allow lines / iptables rules).
Actual application to the host firewall is performed by a host-side script
(firewall application requires host privileges the web container does not have).

Gated by the admin-console session, consistent with the rest of /admin/*.
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import text as sa_text

from modules.admin.admin_panel import require_admin_session, templates
from core.db.base import AsyncSessionLocal

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

TARGETS = {"nginx": "nginx edge (MCP vhosts)",
           "firewall": "host firewall (INPUT)",
           "both": "both surfaces"}


def _normalize_cidr(raw: str) -> str:
    """Accept a single IP or CIDR; return canonical 'a.b.c.d/len'. Raises ValueError."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty")
    net = ipaddress.ip_network(raw, strict=False)  # strict=False tolerates host bits
    return str(net)


@router.get("/ip-whitelist", response_class=HTMLResponse)
async def ip_whitelist_page(request: Request, _: str = Depends(require_admin_session)):
    return templates.TemplateResponse(
        "ip_whitelist.html",
        {"request": request, "page": "ip_whitelist", "targets": TARGETS},
    )


@router.get("/api/ip-whitelist")
async def ip_list(_: str = Depends(require_admin_session)):
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(sa_text("""
            SELECT id, cidr, label, target, is_active, notes, created_by,
                   created_at, last_applied_at
            FROM platform_ip_allowlist
            ORDER BY is_active DESC, target, cidr
        """))).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        for k in ("created_at", "last_applied_at"):
            d[k] = d[k].isoformat() if d.get(k) else None
        out.append(d)
    return {"entries": out, "preview": _render_preview(out)}


def _render_preview(entries: list[dict]) -> dict:
    nginx_cidrs = [e["cidr"] for e in entries if e["is_active"] and e["target"] in ("nginx", "both")]
    fw_cidrs = [e["cidr"] for e in entries if e["is_active"] and e["target"] in ("firewall", "both")]
    nginx_lines = "\n".join(f"allow {c};" for c in nginx_cidrs) + ("\ndeny all;" if nginx_cidrs else "")
    fw_lines = "\n".join(
        f"iptables -A PRAESIDIUM_ALLOW -s {c} -j ACCEPT" for c in fw_cidrs)
    return {"nginx": nginx_lines or "(no active nginx entries)",
            "firewall": fw_lines or "(no active firewall entries)"}


class AddReq(BaseModel):
    cidr: str
    label: str
    target: str = "firewall"
    notes: Optional[str] = None


@router.post("/api/ip-whitelist")
async def ip_add(body: AddReq, _: str = Depends(require_admin_session)):
    if body.target not in TARGETS:
        raise HTTPException(400, "invalid target")
    try:
        cidr = _normalize_cidr(body.cidr)
    except ValueError:
        raise HTTPException(400, f"invalid CIDR/IP: {body.cidr!r}")
    label = (body.label or "").strip() or "unnamed"
    async with AsyncSessionLocal() as s:
        try:
            row = (await s.execute(sa_text("""
                INSERT INTO platform_ip_allowlist (cidr, label, target, notes)
                VALUES (:cidr, :label, :target, :notes)
                RETURNING id
            """), {"cidr": cidr, "label": label, "target": body.target,
                   "notes": body.notes})).mappings().one()
            await s.commit()
        except Exception as exc:
            await s.rollback()
            raise HTTPException(409, f"duplicate or invalid: {exc.__class__.__name__}")
    return {"id": str(row["id"]), "cidr": cidr, "target": body.target}


@router.post("/api/ip-whitelist/{entry_id}/toggle")
async def ip_toggle(entry_id: str, _: str = Depends(require_admin_session)):
    async with AsyncSessionLocal() as s:
        res = await s.execute(sa_text("""
            UPDATE platform_ip_allowlist SET is_active = NOT is_active
            WHERE id = CAST(:id AS uuid) RETURNING is_active
        """), {"id": entry_id})
        row = res.mappings().first()
        await s.commit()
    if not row:
        raise HTTPException(404, "not found")
    return {"is_active": row["is_active"]}


@router.delete("/api/ip-whitelist/{entry_id}")
async def ip_delete(entry_id: str, _: str = Depends(require_admin_session)):
    async with AsyncSessionLocal() as s:
        res = await s.execute(sa_text(
            "DELETE FROM platform_ip_allowlist WHERE id = CAST(:id AS uuid)"),
            {"id": entry_id})
        await s.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "not found")
    return {"deleted": True}
