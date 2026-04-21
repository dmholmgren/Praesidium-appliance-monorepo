"""
dashboard/routes/matter_detail.py
===================================
Matter Detail Page — widget grid surface.
Replaces the old dashboard_panel_registry-driven matter dashboard.

Routes:
    GET /dashboard/matter/{matter_id}          — full page
    GET /dashboard/matter/{matter_id}/tabs     — tab row partial (HTMX)

The page itself is a shell — all content comes from:
    GET /widgets/matter_header?matter_id={id}&context=dashboard
    GET /layouts/matter_detail?tab_slug={slug}&matter_id={id}

On deploy: this module's router replaces the old matter_dashboard +
matter_panel routes in dashboard.py. The old routes can be removed
once this is confirmed working.

Alembic: no migration needed — layout_registry seed handles the layouts.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard-matter"])

_TEMPLATE_DIRS = [
    "/app/modules/dashboard/templates",
    "/app/core/templates",
]

_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIRS),
    autoescape=select_autoescape(["html"]),
)


def _render(template_name: str, context: dict) -> str:
    try:
        tmpl = _env.get_template(template_name)
        return tmpl.render(context)
    except Exception as exc:
        logger.error("Template render error [%s]: %s", template_name, exc)
        return (
            f'<div style="padding:12px;background:#fef2f2;border:1px solid #fecaca;'
            f'border-radius:6px;font-size:11px;color:#991b1b;">'
            f'⚠ Render error: {exc}</div>'
        )


# ---------------------------------------------------------------------------
# Tab loader — reads layout_tabs for layout_slug='matter_detail'
# ---------------------------------------------------------------------------

async def _get_matter_tabs(tenant_id: str, user_role: str) -> list[dict]:
    """Load tabs from layout_tabs for the matter_detail layout."""
    from core.db.base import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT tab_slug, display_name, display_order,
                       permission_level, icon
                FROM layout_tabs
                WHERE layout_slug = 'matter_detail'
                  AND is_visible = TRUE
                ORDER BY display_order
            """))
            rows = r.mappings().fetchall()

        role_rank = {"attorney": 1, "partner": 2, "admin": 3, "superadmin": 4}
        user_rank = role_rank.get(user_role or "attorney", 1)

        tabs = []
        for row in rows:
            min_role = row.get("permission_level") or "attorney"
            if user_rank >= role_rank.get(min_role, 1):
                tabs.append({
                    "tab_slug":    row["tab_slug"],
                    "display_name": row["display_name"],
                    "display_order": row["display_order"],
                    "icon":        row.get("icon") or "",
                })
        return tabs
    except Exception as exc:
        logger.error("_get_matter_tabs error: %s", exc)
        # Fallback — minimal tab set so the page still renders
        return [
            {"tab_slug": "overview", "display_name": "Overview",   "icon": ""},
            {"tab_slug": "billing",  "display_name": "Billing",    "icon": ""},
            {"tab_slug": "documents",     "display_name": "Documents",  "icon": ""},
            {"tab_slug": "matter_intelligence","display_name": "Matter Intelligence",  "icon": ""},
        ]


# ---------------------------------------------------------------------------
# GET /dashboard/matter/{matter_id}  — full page shell
# ---------------------------------------------------------------------------

@router.get("/matter/{matter_id}", response_class=HTMLResponse)
async def matter_detail_page(request: Request, matter_id: str):
    """
    Matter detail — widget grid surface.
    Replaces the old dashboard_panel_registry-driven matter.html.
    """
    from modules.dashboard.services.brand_helper import get_brand
    from core.db.base import AsyncSessionLocal

    tenant_id = (getattr(request.state, "tenant_id", "") or "").strip()
    brand = get_brand(request)

    # Fetch matter name for the <title> tag — everything else via widgets
    matter_name = "Matter"
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT matter_name FROM matters
                WHERE id = CAST(:mid AS uuid)
                  AND trim(tenant_id) = trim(:tid)
            """), {"mid": matter_id, "tid": tenant_id})
            row = r.fetchone()
            if row:
                matter_name = row[0] or "Matter"
    except Exception as exc:
        logger.warning("matter_detail_page name fetch: %s", exc)

    return HTMLResponse(_render("dashboard/matter_detail.html", {
        "request":     request,
        "brand":       brand,
        "matter_id":   matter_id,
        "matter_name": matter_name,
    }))


# ---------------------------------------------------------------------------
# GET /dashboard/matter/{matter_id}/tabs  — tab row HTMX partial
# ---------------------------------------------------------------------------

@router.get("/matter/{matter_id}/tabs", response_class=HTMLResponse)
async def matter_tabs_partial(request: Request, matter_id: str):
    """
    Renders the tab row for the matter detail page.
    Loaded via hx-get on page load.
    """
    tenant_id = (getattr(request.state, "tenant_id", "") or "").strip()
    current_user = getattr(request.state, "current_user", None)
    user_role = getattr(current_user, "role", "attorney") or "attorney"

    tabs = await _get_matter_tabs(tenant_id, user_role)

    if not tabs:
        return HTMLResponse("")

    first_slug = tabs[0]["tab_slug"] if tabs else "matter_overview"

    tab_btns = ""
    for i, tab in enumerate(tabs):
        is_first = i == 0
        active_style = (
            "border-bottom: 2px solid var(--primary, #1B2A4A); "
            "color: var(--primary, #1B2A4A); font-weight: 600;"
        ) if is_first else (
            "border-bottom: 2px solid transparent; "
            "color: var(--muted); font-weight: 400;"
        )
        icon = f"{tab['icon']} " if tab.get("icon") else ""
        tab_btns += (
            f'<button class="matter-tab-btn"'
            f' onclick="switchMatterTab(\'{tab["tab_slug"]}\', this)"'
            f' style="padding: 8px 16px; font-size: 12px; background: none;'
            f'        border: none; border-bottom: 2px solid transparent;'
            f'        cursor: pointer; white-space: nowrap;'
            f'        {active_style}">'
            f'{icon}{tab["display_name"]}'
            f'</button>'
        )

    return HTMLResponse(
        f'<div style="display: flex; gap: 0; border-bottom: 1px solid '
        f'var(--border-color, #e2e8f0); overflow-x: auto;">'
        f'{tab_btns}'
        f'</div>'
    )
