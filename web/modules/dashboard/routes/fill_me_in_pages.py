"""
dashboard/routes/fill_me_in_pages.py
Serves the Fill Me In React shell page.
GET /fill-me-in  — intelligence briefing page
"""
import logging
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["fill-me-in-pages"])


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


async def _render_shell(request: Request, template_name: str, extra_ctx: dict = None):
    from core.services.branding import BrandingService, BrandingConfig
    from core.services.nav_context import get_nav_context

    brand = getattr(request.state, "branding", None)
    if not brand:
        brand = BrandingConfig(tenant_id=getattr(request.state, "tenant_id", None) or "unknown")

    nav_items = {"main": [], "bottom": [], "divider_positions": []}
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT label, rail_label, url_path, page_key, display_order,
                       section, icon_svg
                FROM ui_nav_items
                WHERE tenant_id IS NULL OR TRIM(tenant_id) = :tid
                ORDER BY display_order
            """), {"tid": _tid(request)})
            for row in r.mappings():
                sec = row.get("section") or "main"
                if sec == "divider":
                    nav_items["divider_positions"].append(row["display_order"])
                elif sec == "bottom":
                    nav_items["bottom"].append(dict(row))
                else:
                    nav_items["main"].append(dict(row))
    except Exception as exc:
        logger.warning("Nav items load failed: %s", exc)

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
        "nav_items": nav_items,
        "current_user": getattr(request.state, "current_user", None),
        "page": "dashboard",
        **(extra_ctx or {}),
    }
    return HTMLResponse(tmpl.render(ctx))


@router.get("/fill-me-in", response_class=HTMLResponse)
@router.get("/fill-me-in/", response_class=HTMLResponse)
async def fill_me_in_page(request: Request):
    """Fill Me In — intelligence briefing page."""
    return await _render_shell(request, "fill_me_in_react.html")
