"""
modules/dashboard/routes/project_workspace_route.py — Project Workspace page route

Serves the React shell for /projects/{project_id} when template_type is document_assembly.
Other template types fall through to the generic projects_router.

Patent Pending — Series 1/2/3 — D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

router = APIRouter(tags=["project-workspace"])

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "..", "..", "..", "core", "templates")
)


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_workspace_page(request: Request, project_id: str):
    """Serve the project workspace React page for document_assembly projects."""
    user = getattr(request.state, "current_user", None)
    if not user:
        return HTMLResponse(status_code=302, headers={"Location": "/login"})

    tid = (getattr(request.state, "tenant_id", None) or "").strip()

    async with AsyncSessionLocal() as db:
        row = (await db.execute(sa_text("""
            SELECT p.id::text, p.title, p.template_type, p.status,
                   p.matter_id::text, m.matter_name, m.matter_number,
                   c.client_name
            FROM projects p
            LEFT JOIN matters m ON p.matter_id = m.id
            LEFT JOIN clients c ON c.id = m.client_id
            WHERE p.id = CAST(:pid AS uuid) AND TRIM(p.tenant_id) = :tid
        """), {"pid": project_id, "tid": tid})).fetchone()

    if not row:
        raise HTTPException(404, "Project not found")

    from app import get_brand_context
    from core.services.nav_context import get_nav_context
    context = {"request": request}
    context.update(await get_brand_context(request))
    context.update(await get_nav_context(request))
    context.update({
        "page": "projects",
        "project_id": project_id,
        "project_title": row.title or "Untitled Project",
        "matter_id": row.matter_id,
        "matter_name": row.matter_name or "",
        "matter_number": row.matter_number or "",
        "client_name": row.client_name or "",
        "template_type": row.template_type or "general",
    })

    # Document assembly projects use the workspace surface
    if row.template_type == "document_assembly":
        return templates.TemplateResponse("projects/project_workspace_react.html", context)

    # All other project types use the project detail page
    return templates.TemplateResponse("projects/project_detail_react.html", context)
