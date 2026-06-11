"""
modules/property/page_route.py

Standalone property detail page route.
Serves shell.html-extending React page at /property/{matter_id}/{property_id}

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
"""

from __future__ import annotations
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.modules.property.page_route")
router = APIRouter(tags=["property-pages"])


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


async def _render_shell(request: Request, template_name: str, extra_ctx: dict = None):
    from modules.dashboard.services.brand_helper import get_brand
    from core.services.nav_context import get_nav_context
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    brand = get_brand(request)
    nav_ctx = await get_nav_context(request)

    env = Environment(
        loader=FileSystemLoader([
            "/app/core/templates",
            "/app/templates",
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


@router.get("/property/{matter_id}/{property_id}", response_class=HTMLResponse)
async def property_detail_page(matter_id: str, property_id: str, request: Request):
    """Standalone property detail page."""
    return await _render_shell(request, "property_detail_react.html", {
        "matter_id": matter_id,
        "property_id": property_id,
    })
