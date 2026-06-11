"""
modules/drafting/drafting_api.py — JSON API for React drafting page.

GET  /api/v1/drafting/matters?q=<search>&limit=15
GET  /api/v1/drafting/recent-sessions?limit=5&matter_id=<optional>

Patent Pending — Series 1/2/3 — D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.drafting_api")

router = APIRouter(prefix="/api/v1/drafting", tags=["drafting-api"])


def _strip(v: Optional[str]) -> str:
    return (v or "").strip()


def _tid(request: Request) -> str:
    return _strip(getattr(request.state, "tenant_id", None))


def _user(request: Request):
    u = getattr(request.state, "current_user", None)
    if not u:
        raise HTTPException(401, "Not authenticated")
    return u


@router.get("/matters")
async def search_matters(request: Request, q: str = "", limit: int = 15):
    """Search matters for the matter picker typeahead."""
    _user(request)
    tid = _tid(request)
    if len(q.strip()) < 2:
        return JSONResponse([])

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            text("""
                SELECT m.id::text, m.matter_number, m.matter_name, m.status,
                       c.client_name
                FROM matters m
                LEFT JOIN clients c ON c.id = m.client_id
                WHERE TRIM(m.tenant_id) = :tid
                  AND (m.matter_number ILIKE :p OR m.matter_name ILIKE :p
                       OR c.client_name ILIKE :p)
                ORDER BY m.matter_name
                LIMIT :lim
            """),
            {"tid": tid, "p": f"%{q.strip()}%", "lim": min(limit, 50)},
        )).fetchall()

    return JSONResponse([
        {
            "id": str(r.id),
            "matter_number": r.matter_number or "",
            "matter_name": r.matter_name or "",
            "status": r.status or "",
            "client_name": r.client_name or "",
        }
        for r in rows
    ])


@router.get("/recent-sessions")
async def recent_sessions(request: Request, limit: int = 5, matter_id: str = ""):
    """Get recent drafting sessions for the current tenant."""
    _user(request)
    tid = _tid(request)

    filters = ["TRIM(ds.tenant_id) = :tid"]
    params: Dict[str, Any] = {"tid": tid, "lim": min(limit, 50)}

    if matter_id:
        filters.append("ds.matter_id = CAST(:mid AS uuid)")
        params["mid"] = matter_id

    where = " AND ".join(filters)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            text(f"""
                SELECT ds.id::text, ds.title, ds.document_type, ds.status,
                       ds.created_at, ds.updated_at,
                       m.matter_name, m.matter_number
                FROM drafting_sessions ds
                LEFT JOIN matters m ON m.id = ds.matter_id
                WHERE {where}
                ORDER BY ds.updated_at DESC NULLS LAST
                LIMIT :lim
            """),
            params,
        )).fetchall()

    return JSONResponse([
        {
            "id": str(r.id),
            "title": r.title or "",
            "document_type": r.document_type or "",
            "status": r.status or "",
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "matter_name": r.matter_name or "",
            "matter_number": r.matter_number or "",
        }
        for r in rows
    ])
