"""
Projects Router — Project listing + creation workflow.

Routes:
  GET  /projects                          — projects landing (my projects + recent)
  GET  /projects/new                      — create project form
  GET  /projects/{project_id}             — project detail (workspace shell — future)
  GET  /projects/api/matter-search        — HTMX matter picker
  GET  /projects/api/templates            — HTMX template selector
  GET  /projects/api/team-search          — HTMX team member picker
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from core.services.nav_context import get_nav_context

log = logging.getLogger("praesidium.projects")

router = APIRouter(prefix="/projects", tags=["projects"])

templates = Jinja2Templates(directory=[
    "core/templates",
    "modules/dashboard/templates",
])


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _user(request: Request):
    return getattr(request.state, "current_user", None)


async def _brand_ctx(request: Request) -> dict:
    """Build template context with brand + nav for shell.html pages."""
    from app import get_brand_context
    ctx = {"request": request}
    ctx.update(await get_brand_context(request))
    ctx.update(await get_nav_context(request))
    return ctx


# ── Template types ──
WORKSPACE_TEMPLATES = [
    {
        "key": "simple_motion",
        "name": "Simple Motion Draft",
        "description": "Canvas + exhibits + assembly. Single-attorney motion workflow.",
        "icon": "edit",
        "category": "litigation",
        "default_tasks": [
            {"title": "Research legal standard", "task_type": "research", "priority": "high"},
            {"title": "Draft motion", "task_type": "drafting", "priority": "high"},
            {"title": "Assemble exhibits", "task_type": "assembly", "priority": "medium"},
            {"title": "Final review", "task_type": "review", "priority": "high"},
            {"title": "File with court", "task_type": "filing", "priority": "critical"},
        ],
    },
    {
        "key": "collaborative_motion",
        "name": "Collaborative Motion Draft",
        "description": "Canvas + tasks + chat + timeline. Multi-attorney motion workflow.",
        "icon": "users",
        "category": "litigation",
        "default_tasks": [
            {"title": "Assign research sections", "task_type": "coordination", "priority": "high"},
            {"title": "Draft argument sections", "task_type": "drafting", "priority": "high"},
            {"title": "Peer review drafts", "task_type": "review", "priority": "high"},
            {"title": "Merge and harmonize", "task_type": "drafting", "priority": "high"},
            {"title": "Assemble exhibits", "task_type": "assembly", "priority": "medium"},
            {"title": "Senior partner review", "task_type": "review", "priority": "critical"},
            {"title": "File with court", "task_type": "filing", "priority": "critical"},
        ],
    },
    {
        "key": "contract_assembly",
        "name": "Contract Assembly",
        "description": "Canvas + DMS panel + exhibit ordering + PDF assembly.",
        "icon": "file-text",
        "category": "transactional",
        "default_tasks": [
            {"title": "Select template / exemplar", "task_type": "preparation", "priority": "high"},
            {"title": "Draft contract terms", "task_type": "drafting", "priority": "high"},
            {"title": "Redline review cycle", "task_type": "review", "priority": "high"},
            {"title": "Attach exhibits and schedules", "task_type": "assembly", "priority": "medium"},
            {"title": "Final approval", "task_type": "review", "priority": "critical"},
            {"title": "Circulate for execution", "task_type": "delivery", "priority": "critical"},
        ],
    },
    {
        "key": "deal_room",
        "name": "Deal Room",
        "description": "DMS dataroom + external portal + audit trail + watermarking.",
        "icon": "lock",
        "category": "transactional",
        "default_tasks": [
            {"title": "Organize data room folders", "task_type": "preparation", "priority": "high"},
            {"title": "Upload initial document set", "task_type": "assembly", "priority": "high"},
            {"title": "Configure access permissions", "task_type": "configuration", "priority": "high"},
            {"title": "Share portal with counterparty", "task_type": "delivery", "priority": "critical"},
        ],
    },
    {
        "key": "ediscovery_review",
        "name": "eDiscovery Review",
        "description": "Viewer + review queue + task assignment + privilege log.",
        "icon": "search",
        "category": "litigation",
        "default_tasks": [
            {"title": "Define search terms and custodians", "task_type": "preparation", "priority": "high"},
            {"title": "Run collections", "task_type": "processing", "priority": "high"},
            {"title": "First-pass review", "task_type": "review", "priority": "high"},
            {"title": "Privilege review", "task_type": "review", "priority": "critical"},
            {"title": "QC and production", "task_type": "review", "priority": "high"},
        ],
    },
    {
        "key": "discovery_requests",
        "name": "Discovery Requests",
        "description": "Draft interrogatories, RFPs, or RFAs with exemplar support.",
        "icon": "clipboard",
        "category": "litigation",
        "default_tasks": [
            {"title": "Review opposing pleadings", "task_type": "research", "priority": "high"},
            {"title": "Draft discovery requests", "task_type": "drafting", "priority": "high"},
            {"title": "Attorney review", "task_type": "review", "priority": "high"},
            {"title": "Serve on opposing counsel", "task_type": "delivery", "priority": "critical"},
        ],
    },
    {
        "key": "general",
        "name": "General Project",
        "description": "Blank project with no preset tasks. Add your own workflow.",
        "icon": "folder-plus",
        "category": "general",
        "default_tasks": [],
    },
]

_TEMPLATE_MAP = {t["key"]: t for t in WORKSPACE_TEMPLATES}


# ══════════════════════════════════════════════════════════════
# PAGES
# ══════════════════════════════════════════════════════════════

@router.get("/", response_class=HTMLResponse)
async def projects_landing(request: Request):
    """Projects landing — user's active projects + recently completed."""
    tenant_id = _tid(request)
    user = _user(request)
    ctx = await _brand_ctx(request)

    # My active projects (assigned or created by me)
    my_projects = []
    recent_projects = []

    if user:
        async with AsyncSessionLocal() as session:
            # Projects where I'm creator or lead attorney
            r = await session.execute(sa_text("""
                SELECT p.*, m.matter_name, m.matter_number,
                       (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND TRIM(t.tenant_id) = TRIM(p.tenant_id)) as task_count,
                       (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND TRIM(t.tenant_id) = TRIM(p.tenant_id) AND t.status = 'complete') as completed_count
                FROM projects p
                LEFT JOIN matters m ON p.matter_id = m.id AND TRIM(p.tenant_id) = TRIM(m.tenant_id)
                WHERE TRIM(p.tenant_id) = TRIM(:tid)
                  AND p.status = 'active'
                  AND (p.created_by = :uid OR p.lead_attorney_id = :uid)
                ORDER BY p.updated_at DESC
                LIMIT 20
            """), {"tid": tenant_id, "uid": user.id})
            my_projects = [dict(row._mapping) for row in r.fetchall()]

            # Recent across firm
            r2 = await session.execute(sa_text("""
                SELECT p.*, m.matter_name, m.matter_number, u.full_name as created_by_name,
                       (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND TRIM(t.tenant_id) = TRIM(p.tenant_id)) as task_count,
                       (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND TRIM(t.tenant_id) = TRIM(p.tenant_id) AND t.status = 'complete') as completed_count
                FROM projects p
                LEFT JOIN matters m ON p.matter_id = m.id AND TRIM(p.tenant_id) = TRIM(m.tenant_id)
                LEFT JOIN users u ON p.created_by = u.id AND TRIM(p.tenant_id) = TRIM(u.tenant_id)
                WHERE TRIM(p.tenant_id) = TRIM(:tid)
                ORDER BY p.updated_at DESC
                LIMIT 10
            """), {"tid": tenant_id})
            recent_projects = [dict(row._mapping) for row in r2.fetchall()]

    ctx.update({
        "page": "projects",
        "my_projects": my_projects,
        "recent_projects": recent_projects,
        "templates": WORKSPACE_TEMPLATES,
    })

    return templates.TemplateResponse("projects/projects_landing.html", ctx)


@router.get("/new", response_class=HTMLResponse)
async def new_project_form(request: Request):
    """Project creation wizard — matter picker → template → details → create."""
    ctx = await _brand_ctx(request)
    ctx.update({
        "page": "projects",
        "templates": WORKSPACE_TEMPLATES,
    })
    return templates.TemplateResponse("projects/project_new.html", ctx)


@router.get("/{project_id}", response_class=HTMLResponse)
async def project_detail(request: Request, project_id: str):
    """Project workspace shell — loads widget composition per template type."""
    tenant_id = _tid(request)
    ctx = await _brand_ctx(request)

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT p.*, m.matter_name, m.matter_number, m.matter_type,
                   la.full_name as lead_attorney_name
            FROM projects p
            LEFT JOIN matters m ON p.matter_id = m.id AND TRIM(p.tenant_id) = TRIM(m.tenant_id)
            LEFT JOIN users la ON p.lead_attorney_id = la.id AND TRIM(p.tenant_id) = TRIM(la.tenant_id)
            WHERE p.id = CAST(:pid AS uuid) AND TRIM(p.tenant_id) = TRIM(:tid)
        """), {"pid": project_id, "tid": tenant_id})
        project = r.mappings().fetchone()

    if not project:
        raise HTTPException(404, "Project not found")

    project = dict(project)

    # Load tasks for this project
    async with AsyncSessionLocal() as session:
        r2 = await session.execute(sa_text("""
            SELECT t.*,
                   STRING_AGG(DISTINCT u.full_name, ', ') as assignee_names
            FROM tasks t
            LEFT JOIN task_assignments ta ON t.id = ta.task_id AND TRIM(t.tenant_id) = TRIM(ta.tenant_id)
            LEFT JOIN users u ON ta.user_id = u.id AND TRIM(ta.tenant_id) = TRIM(u.tenant_id)
            WHERE t.project_id = CAST(:pid AS uuid) AND TRIM(t.tenant_id) = TRIM(:tid)
            GROUP BY t.id
            ORDER BY t.sort_order ASC, t.due_date ASC NULLS LAST
        """), {"pid": project_id, "tid": tenant_id})
        tasks = [dict(row._mapping) for row in r2.fetchall()]

    template_info = _TEMPLATE_MAP.get(project.get("template_type"), _TEMPLATE_MAP["general"])

    ctx.update({
        "page": "projects",
        "project": project,
        "project_id": project_id,
        "tasks": tasks,
        "template_info": template_info,
    })

    return templates.TemplateResponse("projects/project_detail.html", ctx)


# ══════════════════════════════════════════════════════════════
# HTMX PARTIALS
# ══════════════════════════════════════════════════════════════

@router.get("/api/matter-search", response_class=HTMLResponse)
async def project_matter_search(request: Request, q: str = ""):
    """Matter picker dropdown for project creation."""
    tenant_id = _tid(request)
    if len(q.strip()) < 2:
        return HTMLResponse("")

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.id, m.matter_number, m.matter_name, m.status,
                   m.matter_type, c.client_name
            FROM matters m
            LEFT JOIN clients c ON c.id = m.client_id
            WHERE TRIM(m.tenant_id) = TRIM(:tid)
              AND (m.matter_number ILIKE :p OR m.matter_name ILIKE :p
                   OR c.client_name ILIKE :p)
            ORDER BY m.matter_name
            LIMIT 15
        """), {"tid": tenant_id, "p": f"%{q.strip()}%"})
        rows = r.fetchall()

    if not rows:
        return HTMLResponse(
            '<div style="padding:10px 12px;font-size:12px;color:var(--muted);">No matters found</div>'
        )

    parts = []
    for row in rows:
        label = f"{row.matter_number or ''} {row.matter_name}".strip()
        client = row.client_name or ""
        mtype = (row.matter_type or "").replace("_", " ").title()
        safe_label = label.replace('"', '&quot;')
        safe_type = (row.matter_type or "").replace('"', '&quot;')
        parts.append(
            f'<div class="picker-item" '
            f'onclick="selectMatter(\'{row.id}\', \'{safe_label}\', \'{safe_type}\')" '
            f'style="padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--border);transition:background 80ms;" '
            f'onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'transparent\'">'
            f'<div style="font-size:13px;font-weight:500;">{label}</div>'
            f'<div style="font-size:11px;color:var(--muted);">'
            f'{client}{" · " if client and mtype else ""}{mtype}{" · " if (client or mtype) and row.status else ""}{row.status or ""}'
            f'</div></div>'
        )
    return HTMLResponse("\n".join(parts))


@router.get("/api/team-search", response_class=HTMLResponse)
async def team_search(request: Request, q: str = ""):
    """User picker for team member assignment."""
    tenant_id = _tid(request)
    if len(q.strip()) < 1:
        # Return all active users
        q_filter = ""
        params = {"tid": tenant_id, "lim": 20}
    else:
        q_filter = "AND (u.full_name ILIKE :p OR u.username ILIKE :p)"
        params = {"tid": tenant_id, "p": f"%{q.strip()}%", "lim": 20}

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(f"""
            SELECT u.id, u.full_name, u.username, u.role
            FROM users u
            WHERE TRIM(u.tenant_id) = TRIM(:tid) AND u.is_active = TRUE
            {q_filter}
            ORDER BY u.full_name
            LIMIT :lim
        """), params)
        rows = r.fetchall()

    if not rows:
        return HTMLResponse('<div style="padding:10px 12px;font-size:12px;color:var(--muted);">No users found</div>')

    parts = []
    for row in rows:
        name = row.full_name or row.username
        role = (row.role or "").replace("_", " ").title()
        initial = (name[0] if name else "?").upper()
        parts.append(
            f'<label style="display:flex;align-items:center;gap:10px;padding:6px 12px;cursor:pointer;'
            f'transition:background 80ms;" '
            f'onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'transparent\'">'
            f'<input type="checkbox" name="team_member" value="{row.id}" onchange="updateTeam()">'
            f'<div style="width:28px;height:28px;border-radius:50%;background:#E2E8F0;display:flex;'
            f'align-items:center;justify-content:center;font-size:11px;font-weight:600;color:#475569;flex-shrink:0;">'
            f'{initial}</div>'
            f'<div style="flex:1;min-width:0;">'
            f'<div style="font-size:13px;font-weight:500;">{name}</div>'
            f'<div style="font-size:11px;color:var(--muted);">{role}</div>'
            f'</div></label>'
        )
    return HTMLResponse("\n".join(parts))


@router.get("/api/template-tasks", response_class=HTMLResponse)
async def template_tasks_preview(request: Request, template_type: str = "general"):
    """Preview default tasks for a template — shown during project creation."""
    tmpl = _TEMPLATE_MAP.get(template_type, _TEMPLATE_MAP["general"])
    tasks = tmpl.get("default_tasks", [])

    if not tasks:
        return HTMLResponse(
            '<div style="padding:12px;font-size:12px;color:var(--muted);">'
            'No default tasks — add your own after creating the project.</div>'
        )

    parts = ['<div style="display:flex;flex-direction:column;gap:4px;">']
    for i, t in enumerate(tasks):
        priority_colors = {
            "critical": "#991B1B", "high": "#92400E",
            "medium": "#475569", "low": "#94A3B8",
        }
        pcolor = priority_colors.get(t.get("priority", "medium"), "#475569")
        parts.append(
            f'<div style="display:flex;align-items:center;gap:8px;padding:6px 0;'
            f'border-bottom:1px solid var(--border);">'
            f'<span style="font-size:11px;color:{pcolor};font-weight:500;width:16px;text-align:center;">{i+1}</span>'
            f'<span style="font-size:13px;flex:1;">{t["title"]}</span>'
            f'<span style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.04em;">'
            f'{t.get("task_type", "general")}</span>'
            f'</div>'
        )
    parts.append('</div>')
    return HTMLResponse("\n".join(parts))
