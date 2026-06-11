"""
dashboard/routes/matters_pages.py
==================================
Serves the React page shells for:
    GET /matters           — Matters Home (search-first)
    GET /matters/new       — New Matter form
    GET /matters/{id}      — Matter Dashboard (typed sub-tabs)

Both extend shell.html and mount React roots.
"""
import logging
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["matters-pages"])


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


async def _render_shell(request: Request, template_name: str, extra_ctx: dict = None):
    """Render a shell.html-extending template with full nav context.

    Uses core.services.nav_context which resolves nav from ui_nav_items
    via nav_service (data-driven, tenant-overridable, Redis-cached).
    """
    from modules.dashboard.services.brand_helper import get_brand
    from core.services.nav_context import get_nav_context

    brand = get_brand(request)
    nav_ctx = await get_nav_context(request)

    from jinja2 import Environment, FileSystemLoader, select_autoescape
    env = Environment(
        loader=FileSystemLoader([
            "/app/core/templates",
            "/app/modules/dashboard/templates",
        ]),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template(template_name)
    ctx = {
        "request": request,
        "brand": brand,
        "current_user": getattr(request.state, "current_user", None),
        "page": "matters",
        **nav_ctx,
        **(extra_ctx or {}),
    }
    return HTMLResponse(tmpl.render(ctx))


@router.get("/matters", response_class=HTMLResponse)
async def matters_home_page(request: Request):
    """Matters Home — search-first landing page."""
    return await _render_shell(request, "matters_home_react.html")


@router.get("/matters/new", response_class=HTMLResponse)
async def matter_new_page(request: Request):
    """New Matter — create-matter form page."""
    return await _render_shell(request, "matter_dashboard_react.html", {
        "matter_id": "new",
        "matter_name": "New Matter",
    })


@router.get("/matters/{matter_id}/spine-review", response_class=HTMLResponse)
async def matter_spine_review_page(request: Request, matter_id: str):
    """Allegation Spine Review — per-matter, span-grounded."""
    return await _render_shell(request, "matter_spine_review_react.html", {
        "matter_id": matter_id,
    })


@router.get("/matters/{matter_id}", response_class=HTMLResponse)
async def matter_dashboard_page(request: Request, matter_id: str):
    """Matter Dashboard — per-matter page with typed sub-tabs."""
    tid = _tid(request)
    matter_name = "Matter"
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT matter_name FROM matters
                WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
            """), {"mid": matter_id, "tid": tid})
            row = r.fetchone()
            if row:
                matter_name = row[0] or "Matter"
    except Exception as exc:
        logger.warning("matter_dashboard_page name fetch: %s", exc)

    return await _render_shell(request, "matter_dashboard_react.html", {
        "matter_id": matter_id,
        "matter_name": matter_name,
    })


@router.get("/chat-history", response_class=HTMLResponse)
async def chat_history_page(request: Request):
    """Chat History — AI conversation archive."""
    return await _render_shell(request, "chat_history_react.html", {"page": "chat-history"})


@router.get("/matters/{matter_id}/claims", response_class=HTMLResponse)
async def claims_elements_page(request: Request, matter_id: str):
    """Claims & Elements - structured issue map for litigation matters."""
    tid = _tid(request)
    matter_name = "Matter"
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT matter_name FROM matters
                WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
            """), {"mid": matter_id, "tid": tid})
            row = r.fetchone()
            if row:
                matter_name = row[0] or "Matter"
    except Exception as exc:
        logger.warning("claims_elements_page name fetch: %s", exc)

    return await _render_shell(request, "claims_elements_react.html", {
        "matter_id": matter_id,
        "matter_name": matter_name,
    })
