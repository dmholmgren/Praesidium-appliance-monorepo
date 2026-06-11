"""
witness_workspace_route.py — page shell for /matters/{mid}/witnesses/{cid}/
"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["witness-workspace-pages"])


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


async def _render_shell(request: Request, template_name: str, extra_ctx: dict = None):
    from modules.dashboard.services.brand_helper import get_brand
    from modules.dashboard.services.nav_context import get_nav_context
    brand = get_brand(request)
    nav = await get_nav_context(request, page="matters")
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
        loader=FileSystemLoader(["/app/core/templates", "/app/modules/dashboard/templates"]),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template(template_name)
    ctx = {
        "request": request, "brand": brand, "nav_items": nav_items,
        "current_user": getattr(request.state, "current_user", None),
        "page": "matters", **(extra_ctx or {}),
    }
    return HTMLResponse(tmpl.render(ctx))


@router.get("/matters/{matter_id}/witnesses/{contact_id}/", response_class=HTMLResponse)
@router.get("/matters/{matter_id}/witnesses/{contact_id}", response_class=HTMLResponse)
async def witness_workspace_page(request: Request, matter_id: str, contact_id: str):
    tid = _tid(request)
    witness_name = "Witness"
    matter_name = "Matter"
    matter_number = ""
    mc_id = ""
    try:
        cid_int = int(contact_id)
        async with AsyncSessionLocal() as session:
            row = (await session.execute(sa_text("""
                SELECT c.full_name, m.matter_name, m.matter_number, mc.id as mc_id
                FROM matter_contacts mc
                JOIN contacts c ON c.id = mc.contact_id
                JOIN matters m ON m.id = mc.matter_id
                WHERE mc.matter_id = CAST(:mid AS uuid)
                  AND mc.contact_id = :cid
                  AND TRIM(mc.tenant_id) = :tid
                LIMIT 1
            """), {"mid": matter_id, "cid": cid_int, "tid": tid})).fetchone()
            if row:
                witness_name = row.full_name or "Witness"
                matter_name = row.matter_name or "Matter"
                matter_number = row.matter_number or ""
                mc_id = str(row.mc_id) if row.mc_id else ""
    except Exception as exc:
        logger.warning("witness_workspace_page fetch: %s", exc)
    return await _render_shell(request, "witness_workspace_react.html", {
        "matter_id": matter_id,
        "contact_id": contact_id,
        "mc_id": mc_id,
        "witness_name": witness_name,
        "matter_name": matter_name,
        "matter_number": matter_number,
    })
