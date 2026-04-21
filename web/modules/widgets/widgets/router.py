# modules/widgets/router.py
"""
Generic widget render route.
GET /widgets/{widget_slug}?matter_id=...&date_from=...&date_to=...

One route handles all widget renders. No per-widget routes. No per-page handlers.
Placeholder and launcher types return 204 — they are not rendered inline.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.widgets.service import WidgetDataSourceError, resolve_data_source

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/widgets", tags=["widgets"])

# Permission level hierarchy — index = minimum access rank
_PERMISSION_RANK = {
    "attorney": 0,
    "partner": 1,
    "admin": 2,
    "superadmin": 3,
}

# User role -> rank mapping (matches roles used across the platform)
_ROLE_RANK = {
    "attorney": 0,
    "partner": 1,
    "admin": 2,
    "superadmin": 3,
}


def _user_meets_permission(user_role: str, required_level: str) -> bool:
    user_rank = _ROLE_RANK.get(user_role, 0)
    required_rank = _PERMISSION_RANK.get(required_level, 0)
    return user_rank >= required_rank


@router.get("/{widget_slug}", response_class=HTMLResponse)
async def render_widget(
    request: Request,
    widget_slug: str,
    matter_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """
    Generic widget render endpoint.

    Looks up widget_registry row by slug, enforces permissions and feature flags,
    resolves data source, and returns a rendered HTMX-friendly HTML partial.

    Returns:
        200 + HTML fragment  — data_panel rendered successfully
        204 No Content       — placeholder or launcher (not rendered inline)
        403 Forbidden        — user lacks required permission level
        404 Not Found        — widget_slug not in registry
        503 Service Unavail  — data source resolution or call failed
    """
    # --- Extract session context ---
    tenant_id: str = request.session.get("tenant_id", "")
    user_id: int = request.session.get("user_id", 0)
    user_role: str = request.session.get("role", "attorney")
    tenant_id = tenant_id.strip()

    # --- Look up widget row ---
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT
                    widget_slug,
                    widget_name,
                    widget_type,
                    data_source,
                    render_template,
                    permission_level,
                    feature_flag,
                    target_route,
                    config_schema
                FROM widget_registry
                WHERE widget_slug = :slug
                  AND (tenant_id IS NULL OR TRIM(tenant_id) = :tenant_id)
                LIMIT 1
                """
            ),
            {"slug": widget_slug, "tenant_id": tenant_id},
        )
        row = result.mappings().first()

    if row is None:
        logger.warning("Widget not found: %s", widget_slug)
        return Response(status_code=404)

    widget = dict(row)

    # --- Permission check ---
    required_level = widget.get("permission_level") or "attorney"
    if not _user_meets_permission(user_role, required_level):
        logger.warning(
            "Widget permission denied: slug=%s user_role=%s required=%s",
            widget_slug, user_role, required_level,
        )
        return Response(status_code=403)

    # --- Feature flag check ---
    feature_flag = widget.get("feature_flag")
    if feature_flag:
        async with AsyncSessionLocal() as session:
            ff_result = await session.execute(
                text(
                    """
                    SELECT enabled FROM feature_flags
                    WHERE TRIM(tenant_id) = :tenant_id
                      AND flag_name = :flag_name
                    LIMIT 1
                    """
                ),
                {"tenant_id": tenant_id, "flag_name": feature_flag},
            )
            ff_row = ff_result.mappings().first()
        if ff_row is None or not ff_row["enabled"]:
            logger.debug(
                "Widget gated by disabled feature flag: %s / %s",
                widget_slug, feature_flag,
            )
            return Response(status_code=204)

    # --- Type dispatch ---
    widget_type = widget.get("widget_type", "placeholder")

    if widget_type in ("placeholder", "launcher", "report"):
        # Placeholders: not yet built.
        # Launchers: full-page navigation, not rendered inline.
        # Report: reserved for report_registry — not yet wired.
        return Response(status_code=204)

    if widget_type != "data_panel":
        logger.error(
            "Unknown widget_type '%s' for slug '%s'", widget_type, widget_slug
        )
        return Response(status_code=204)

    # --- data_panel: resolve data source ---
    data_source = widget.get("data_source")
    render_template = widget.get("render_template")

    if not data_source or not render_template:
        logger.error(
            "data_panel missing data_source or render_template: %s", widget_slug
        )
        return Response(status_code=503)

    try:
        context_data = await resolve_data_source(
            data_source=data_source,
            tenant_id=tenant_id,
            user_id=user_id,
            matter_id=matter_id,
            date_from=date_from,
            date_to=date_to,
        )
    except WidgetDataSourceError as exc:
        logger.error("Widget data source error [%s]: %s", widget_slug, exc)
        return Response(status_code=503)

    # --- Render partial template ---
    templates = request.app.state.templates
    context = {
        "request": request,
        "widget": widget,
        "matter_id": matter_id,
        **context_data,
    }

    return templates.TemplateResponse(render_template, context)
