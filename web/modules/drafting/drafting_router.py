"""
modules/drafting/drafting_router.py

FastAPI router for Module 4 — Document Generation & Assembly.

Mounts at: /drafting/
Registered in: app.py

Routes:
    GET  /drafting/                              -- panel home (matter selector)
    GET  /drafting/matters/{matter_id}           -- drafting panel for a matter
    POST /drafting/matters/{matter_id}/sessions  -- create new session
    GET  /drafting/sessions/{session_id}         -- session detail / resume
    POST /drafting/sessions/{session_id}/assemble   -- run assembly
    POST /drafting/sessions/{session_id}/sanity     -- run sanity check
    POST /drafting/sessions/{session_id}/dismiss    -- dismiss session
    POST /drafting/sessions/{session_id}/bates/{log_id}/dispose  -- accept/reject bates item
    GET  /drafting/templates                     -- list templates (HTMX partial)
    GET  /drafting/exemplars                     -- list exemplars (HTMX partial)

Architecture:
    - No DB access in this file -- all calls go through drafting_service
    - TemplateResponse(request, "name.html", {...}) signature
    - Static paths before path parameters
    - get_nav_context called on every TemplateResponse
    - tenant_id from request.state (ActivityMiddleware)
    - user_id from request.state (ActivityMiddleware)
"""

from __future__ import annotations

import logging
import os
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from modules.dashboard.services.nav_context import get_nav_context
from modules.drafting.drafting_service import (
    create_session,
    dismiss_session,
    get_session,
    get_session_summary,
    list_sessions,
    run_assembly,
    run_sanity,
)
from modules.drafting.bates_service import dispose_insertion

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/drafting", tags=["drafting"])

_TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "templates",
)
templates = Jinja2Templates(directory=[
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "core", "templates"),
    _TEMPLATE_DIR,
    os.path.join(_TEMPLATE_DIR, "drafting"),
])


def _get_tenant(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _get_user(request: Request) -> Optional[int]:
    return getattr(request.state, "user_id", None)


# ---------------------------------------------------------------------------
# Panel home — matter selector
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def drafting_home(request: Request):
    tenant_id = _get_tenant(request)
    if not tenant_id:
        return RedirectResponse("/login", status_code=303)

    sessions = await list_sessions(tenant_id=tenant_id, limit=20)
    nav = await get_nav_context(request, page="drafting")

    return templates.TemplateResponse(
        request,
        "drafting/drafting_home.html",
        {
            "nav": nav,
            "recent_sessions": sessions,
            "page_title": "Document Drafting",
        },
    )


# ---------------------------------------------------------------------------
# Matter-scoped drafting panel
# ---------------------------------------------------------------------------

@router.get("/matters/{matter_id}", response_class=HTMLResponse)
async def drafting_panel(request: Request, matter_id: UUID):
    tenant_id = _get_tenant(request)
    if not tenant_id:
        return RedirectResponse("/login", status_code=303)

    sessions = await list_sessions(
        tenant_id=tenant_id,
        matter_id=matter_id,
        limit=20,
    )
    nav = await get_nav_context(request, page="drafting")

    return templates.TemplateResponse(
        request,
        "drafting/drafting_panel.html",
        {
            "nav": nav,
            "matter_id": str(matter_id),
            "sessions": sessions,
            "page_title": "Document Drafting",
            "document_types": [
                ("motion", "Motion"),
                ("brief", "Brief"),
                ("contract", "Contract"),
                ("discovery_request", "Discovery Request"),
                ("discovery_response", "Discovery Response"),
                ("pleading", "Pleading"),
                ("letter", "Letter"),
                ("disclosure", "Disclosure"),
                ("agreement", "Agreement"),
                ("other", "Other"),
            ],
            "practice_areas": [
                ("litigation", "Litigation"),
                ("transactional", "Transactional"),
                ("real_estate", "Real Estate"),
                ("securities", "Securities"),
                ("employment", "Employment"),
                ("appellate", "Appellate"),
                ("federal", "Federal"),
                ("ediscovery", "eDiscovery"),
                ("general", "General"),
            ],
        },
    )


# ---------------------------------------------------------------------------
# Create session
# ---------------------------------------------------------------------------

@router.post("/matters/{matter_id}/sessions")
async def create_drafting_session(
    request: Request,
    matter_id: UUID,
    document_type: str = Form(...),
    practice_area: str = Form(...),
    title: Optional[str] = Form(None),
    template_id: Optional[str] = Form(None),
    assembly_prompt: Optional[str] = Form(None),
):
    tenant_id = _get_tenant(request)
    user_id = _get_user(request)
    if not tenant_id:
        return RedirectResponse("/login", status_code=303)

    tmpl_uuid = UUID(template_id) if template_id else None

    sess = await create_session(
        tenant_id=tenant_id,
        matter_id=matter_id,
        document_type=document_type,
        practice_area=practice_area,
        title=title,
        template_id=tmpl_uuid,
        source_doc_ids=[],
        assembly_prompt=assembly_prompt,
        created_by=user_id,
    )

    return RedirectResponse(
        f"/drafting/sessions/{sess['id']}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Session detail — resume / view
# ---------------------------------------------------------------------------

@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_detail(request: Request, session_id: UUID):
    tenant_id = _get_tenant(request)
    if not tenant_id:
        return RedirectResponse("/login", status_code=303)

    summary = await get_session_summary(session_id, tenant_id)
    if not summary:
        return HTMLResponse("Session not found", status_code=404)

    nav = await get_nav_context(request, page="drafting")

    return templates.TemplateResponse(
        request,
        "drafting/drafting_session.html",
        {
            "nav": nav,
            "summary": summary,
            "session": summary["session"],
            "latest_doc": summary["latest_document"],
            "sanity": summary["sanity"],
            "bates": summary["bates"],
            "contributions": summary["ai_contributions"],
            "page_title": summary["session"].get("title", "Draft"),
        },
    )


# ---------------------------------------------------------------------------
# Run assembly
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/assemble")
async def assemble(request: Request, session_id: UUID):
    tenant_id = _get_tenant(request)
    user_id = _get_user(request)
    if not tenant_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        result = await run_assembly(
            session_id=session_id,
            tenant_id=tenant_id,
            created_by=user_id,
        )
        # HTMX redirect to session detail to reload with new draft
        return RedirectResponse(
            f"/drafting/sessions/{session_id}",
            status_code=303,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("Assembly failed session=%s: %s", session_id, exc)
        return JSONResponse({"error": "Assembly failed. Check logs."}, status_code=500)


# ---------------------------------------------------------------------------
# Run sanity check
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/sanity")
async def sanity(request: Request, session_id: UUID):
    tenant_id = _get_tenant(request)
    if not tenant_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        result = await run_sanity(
            session_id=session_id,
            tenant_id=tenant_id,
        )
        return RedirectResponse(
            f"/drafting/sessions/{session_id}",
            status_code=303,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("Sanity check failed session=%s: %s", session_id, exc)
        return JSONResponse({"error": "Sanity check failed. Check logs."}, status_code=500)


# ---------------------------------------------------------------------------
# Dismiss session
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/dismiss")
async def dismiss(request: Request, session_id: UUID):
    tenant_id = _get_tenant(request)
    if not tenant_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    await dismiss_session(session_id, tenant_id)

    matter_id = request.query_params.get("matter_id")
    if matter_id:
        return RedirectResponse(f"/drafting/matters/{matter_id}", status_code=303)
    return RedirectResponse("/drafting/", status_code=303)


# ---------------------------------------------------------------------------
# Dispose Bates insertion (accept / reject)
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/bates/{log_id}/dispose")
async def dispose_bates(
    request: Request,
    session_id: UUID,
    log_id: UUID,
    disposition: str = Form(...),
):
    tenant_id = _get_tenant(request)
    user_id = _get_user(request)
    if not tenant_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if disposition not in ("accepted", "rejected"):
        return JSONResponse({"error": "Invalid disposition"}, status_code=400)

    updated = await dispose_insertion(
        log_id=log_id,
        session_id=session_id,
        tenant_id=tenant_id,
        disposition=disposition,
        disposed_by=user_id or 0,
    )

    if not updated:
        return JSONResponse(
            {"error": "Item not found or already disposed"},
            status_code=404,
        )

    # HTMX partial refresh — return updated bates row
    from modules.drafting.bates_service import get_bates_insertions
    items = await get_bates_insertions(session_id, tenant_id)
    item = next((i for i in items if i["id"] == str(log_id)), None)

    return templates.TemplateResponse(
        request,
        "drafting/partials/bates_row.html",
        {"item": item},
    )


# ---------------------------------------------------------------------------
# Template list (HTMX partial)
# ---------------------------------------------------------------------------

@router.get("/templates", response_class=HTMLResponse)
async def list_templates(
    request: Request,
    document_type: Optional[str] = None,
    practice_area: Optional[str] = None,
):
    tenant_id = _get_tenant(request)
    if not tenant_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal

    filters = ["trim(tenant_id) = :tid", "is_active = TRUE"]
    params: dict = {"tid": tenant_id}
    if document_type:
        filters.append("document_type = :dt")
        params["dt"] = document_type
    if practice_area:
        filters.append("practice_area = :pa")
        params["pa"] = practice_area

    where = " AND ".join(filters)
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            text(
                f"SELECT id, name, document_type, practice_area, scope, version "
                f"FROM template_library WHERE {where} "
                f"ORDER BY practice_area, document_type, name"
            ),
            params,
        )
        tmpl_list = [
            {
                "id": str(r.id),
                "name": r.name,
                "document_type": r.document_type,
                "practice_area": r.practice_area,
                "scope": r.scope,
                "version": r.version,
            }
            for r in rows.fetchall()
        ]

    return templates.TemplateResponse(
        request,
        "drafting/partials/template_list.html",
        {"templates": tmpl_list},
    )


# ---------------------------------------------------------------------------
# Exemplar list (HTMX partial)
# ---------------------------------------------------------------------------

@router.get("/exemplars", response_class=HTMLResponse)
async def list_exemplars(
    request: Request,
    document_type: Optional[str] = None,
    matter_id: Optional[str] = None,
):
    tenant_id = _get_tenant(request)
    if not tenant_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal

    filters = ["trim(tenant_id) = :tid", "is_active = TRUE"]
    params: dict = {"tid": tenant_id}
    if document_type:
        filters.append("document_type = :dt")
        params["dt"] = document_type
    if matter_id:
        filters.append("(matter_id IS NULL OR matter_id = :mid)")
        params["mid"] = matter_id

    where = " AND ".join(filters)
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            text(
                f"SELECT id, name, document_type, practice_area, "
                f"       matter_id, created_at "
                f"FROM exemplar_library WHERE {where} "
                f"ORDER BY matter_id NULLS LAST, document_type, name"
            ),
            params,
        )
        ex_list = [
            {
                "id": str(r.id),
                "name": r.name,
                "document_type": r.document_type,
                "practice_area": r.practice_area,
                "scope": "matter" if r.matter_id else "firm",
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows.fetchall()
        ]

    return templates.TemplateResponse(
        request,
        "drafting/partials/exemplar_list.html",
        {"exemplars": ex_list},
    )
