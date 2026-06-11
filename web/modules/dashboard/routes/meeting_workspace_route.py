"""
modules/dashboard/routes/meeting_workspace_route.py — Meeting Workspace page shell

Serves the React shell for /workspaces/{workspace_id}
and the workspace list at /workspaces/

Patent Pending — Series 1/2/3/4 — D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

router = APIRouter(tags=["meeting-workspace-pages"])

templates = Jinja2Templates(
    directory=[
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "core", "templates"),
        os.path.join(os.path.dirname(__file__), "..", "templates"),
    ]
)


@router.get("/workspaces/", response_class=HTMLResponse)
@router.get("/workspaces", response_class=HTMLResponse)
async def workspaces_landing(request: Request):
    """Workspaces list — recent, upcoming, by matter."""
    user = getattr(request.state, "current_user", None)
    if not user:
        return HTMLResponse(status_code=302, headers={"Location": "/login"})

    from app import get_brand_context
    from core.services.nav_context import get_nav_context
    ctx = {"request": request}
    ctx.update(await get_brand_context(request))
    ctx.update(await get_nav_context(request))
    ctx["page"] = "workspaces"

    return templates.TemplateResponse("workspaces/workspace_list_react.html", ctx)


@router.get("/workspaces/{workspace_id}", response_class=HTMLResponse)
async def workspace_detail_page(request: Request, workspace_id: str):
    """Workspace detail — React shell with widget composition."""
    user = getattr(request.state, "current_user", None)
    if not user:
        return HTMLResponse(status_code=302, headers={"Location": "/login"})

    tid = (getattr(request.state, "tenant_id", "") or "").strip()

    async with AsyncSessionLocal() as db:
        ws = (await db.execute(sa_text("""
            SELECT w.id::text, w.title, w.workspace_type, w.status,
                   w.matter_id::text, w.project_id::text,
                   w.config, w.scheduled_at,
                   m.matter_name, m.matter_number,
                   c.client_name,
                   p.title as project_title
            FROM meeting_workspaces w
            LEFT JOIN matters m ON w.matter_id = m.id
            LEFT JOIN clients c ON c.id = m.client_id
            LEFT JOIN projects p ON w.project_id = p.id
            WHERE w.id = CAST(:wid AS uuid) AND TRIM(w.tenant_id) = :tid
        """), {"wid": workspace_id, "tid": tid})).fetchone()

    if not ws:
        raise HTTPException(404, "Workspace not found")

    from app import get_brand_context
    from core.services.nav_context import get_nav_context
    ctx = {"request": request}
    ctx.update(await get_brand_context(request))
    ctx.update(await get_nav_context(request))
    ctx.update({
        "page": "workspaces",
        "workspace_id": workspace_id,
        "workspace_title": ws.title or "Untitled Workspace",
        "workspace_type": ws.workspace_type or "meeting",
        "matter_id": ws.matter_id or "",
        "matter_name": ws.matter_name or "",
        "matter_number": ws.matter_number or "",
        "client_name": ws.client_name or "",
        "project_id": ws.project_id or "",
        "project_title": ws.project_title or "",
    })

    return templates.TemplateResponse("workspaces/workspace_detail_react.html", ctx)
