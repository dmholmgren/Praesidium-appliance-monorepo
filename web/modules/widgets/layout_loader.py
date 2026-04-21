"""
Widget Registry — Layout Loader
Step 3 of Widget Registry Build Sequence

GET /layouts/{layout_slug}
  Reads the layout_registry row for the given slug + tab_slug,
  renders the widget grid with HTMX swap targets for each slot.
  Each slot fires hx-get="/widgets/{widget_slug}?..." on load.

Query params:
    tab_slug    — tab context (default: 'default')
    matter_id   — passed through to widget scope
    attorney_id — passed through to widget scope
    date_from   — passed through to widget scope
    date_to     — passed through to widget scope

Resolution order for layout row:
    1. User-custom row (tenant_id + user_id match, layout_slug + tab_slug match)
    2. Tenant-default row (tenant_id match, user_id IS NULL, is_default = TRUE)
    3. Platform-default row (tenant_id IS NULL, is_default = TRUE)

Returns an HTML partial — the grid shell with HTMX-loaded widget slots.
Never returns a full page. Embed via hx-get="/layouts/{slug}" into any surface.
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/layouts", tags=["layout-registry"])

# ---------------------------------------------------------------------------
# Template environment — same search path as widget_routes
# ---------------------------------------------------------------------------

_TEMPLATE_DIRS = [
    "/app/modules/widgets/templates",
    "/app/modules/dms/templates",
    "/app/modules/billing/templates",
    "/app/modules/dashboard/templates",
    "/app/modules/ediscovery/templates",
    "/app/modules/intelligence/templates",
    "/app/core/templates",
]

_layout_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIRS),
    autoescape=select_autoescape(["html"]),
)


def _render(template_name: str, context: dict) -> str:
    try:
        tmpl = _layout_env.get_template(template_name)
        return tmpl.render(context)
    except Exception as exc:
        logger.error("Layout template render error [%s]: %s", template_name, exc)
        return (
            f'<div style="padding:12px;background:#fef2f2;border:1px solid #fecaca;'
            f'border-radius:6px;font-size:11px;color:#991b1b;">'
            f'⚠ Layout error: {template_name} — {exc}</div>'
        )


# ---------------------------------------------------------------------------
# Layout registry lookup
# ---------------------------------------------------------------------------

async def _lookup_layout(
    layout_slug: str,
    tab_slug: str,
    tenant_id: str,
    user_id: Optional[int],
) -> Optional[dict]:
    """
    Resolve the best layout row for this tenant/user/slug/tab combination.
    Priority: user-custom > tenant-default > platform-default.
    """
    from core.db.base import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT
                id::text,
                layout_slug,
                tab_slug,
                widget_positions,
                is_default,
                tenant_id,
                user_id
            FROM layout_registry
            WHERE layout_slug = :slug
              AND tab_slug    = :tab
              AND (
                    -- User-custom
                    (tenant_id IS NOT NULL AND trim(tenant_id) = trim(:tid)
                     AND user_id = :uid)
                    OR
                    -- Tenant-default
                    (tenant_id IS NOT NULL AND trim(tenant_id) = trim(:tid)
                     AND user_id IS NULL AND is_default = TRUE)
                    OR
                    -- Platform-default (sentinel string OR NULL)
                    (trim(tenant_id) = 'platform-default' AND is_default = TRUE)
                    OR
                    (tenant_id IS NULL AND is_default = TRUE)
                  )
            ORDER BY
                CASE
                    WHEN tenant_id IS NOT NULL AND user_id IS NOT NULL THEN 0
                    WHEN tenant_id IS NOT NULL AND user_id IS NULL     THEN 1
                    ELSE 2
                END
            LIMIT 1
        """), {
            "slug": layout_slug,
            "tab":  tab_slug,
            "tid":  tenant_id,
            "uid":  user_id,
        })
        row = r.mappings().fetchone()
        if not row:
            return None

        result = dict(row)
        # widget_positions arrives as a string or already parsed depending on driver
        wp = result.get("widget_positions")
        if isinstance(wp, str):
            try:
                result["widget_positions"] = json.loads(wp)
            except Exception:
                result["widget_positions"] = []
        elif wp is None:
            result["widget_positions"] = []
        return result


# ---------------------------------------------------------------------------
# Size → CSS grid span mapping
# ---------------------------------------------------------------------------

_SIZE_SPANS = {
    "small":      {"col_span": 1, "min_height": "340px"},
    "medium":     {"col_span": 2, "min_height": "340px"},
    "large":      {"col_span": 2, "min_height": "420px"},
    "full-width": {"col_span": 3, "min_height": "280px"},
}

_DEFAULT_SPAN = {"col_span": 1, "min_height": "180px"}


def _enrich_positions(widget_positions: list) -> list:
    """
    Add CSS sizing metadata to each widget_position entry.
    Sorts by row then col for deterministic grid order.
    """
    enriched = []
    for pos in sorted(widget_positions, key=lambda p: (p.get("row", 1), p.get("col", 1))):
        size = pos.get("size_override") or "medium"
        spans = _SIZE_SPANS.get(size, _DEFAULT_SPAN)
        enriched.append({
            **pos,
            "col_span": spans["col_span"],
            "min_height": spans["min_height"],
            "size": size,
            "config_override": pos.get("config_override") or {},
        })
    return enriched


# ---------------------------------------------------------------------------
# Build the scope query string to pass through to each widget
# ---------------------------------------------------------------------------

def _scope_qs(matter_id, attorney_id, date_from, date_to) -> str:
    """Build a query string fragment for widget HTMX calls."""
    parts = []
    if matter_id:
        parts.append(f"matter_id={matter_id}")
    if attorney_id:
        parts.append(f"attorney_id={attorney_id}")
    if date_from:
        parts.append(f"date_from={date_from}")
    if date_to:
        parts.append(f"date_to={date_to}")
    return ("&" + "&".join(parts)) if parts else ""


# ---------------------------------------------------------------------------
# Layout loader route
# ---------------------------------------------------------------------------

@router.get("/{layout_slug}", response_class=HTMLResponse)
async def load_layout(
    request: Request,
    layout_slug: str,
    tab_slug: str = "default",
    matter_id: Optional[str] = None,
    attorney_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """
    Generic layout loader.
    Returns the widget grid shell — each slot fires hx-get on load to pull its widget.
    Embed into any page surface with:
        <div hx-get="/layouts/dms_matter?matter_id={{ matter_id }}"
             hx-trigger="load" hx-swap="innerHTML"></div>
    """
    tenant_id = (getattr(request.state, "tenant_id", "") or "").strip()
    current_user = getattr(request.state, "current_user", None)
    user_id = getattr(current_user, "id", None)

    layout = await _lookup_layout(layout_slug, tab_slug, tenant_id, user_id)

    if not layout:
        logger.warning("Layout not found: slug=%s tab=%s tenant=%s", layout_slug, tab_slug, tenant_id)
        return HTMLResponse(
            '<div style="padding:12px;font-size:11px;color:var(--muted);">'
            f'No layout defined for {layout_slug}/{tab_slug}.</div>'
        )

    positions = _enrich_positions(layout.get("widget_positions") or [])
    scope_qs = _scope_qs(matter_id, attorney_id, date_from, date_to)

    return HTMLResponse(_render("layouts/widget_grid.html", {
        "layout":      layout,
        "layout_slug": layout_slug,
        "tab_slug":    tab_slug,
        "positions":   positions,
        "scope_qs":    scope_qs,
        "matter_id":   matter_id,
        "attorney_id": attorney_id,
    }))
