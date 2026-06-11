"""
Widget Registry — Generic Render Route
Step 2 of Widget Registry Build Sequence

GET /widgets/{widget_slug}
  Scope parameters (all optional, passed as query params):
    tenant_id   — resolved from request.state if not provided
    user_id     — resolved from request.state.current_user if not provided
    matter_id   — UUID string, optional
    attorney_id — UUID string, optional (Attorney View tab)
    date_from   — ISO date string, optional
    date_to     — ISO date string, optional

Dispatch logic:
  data_panel  → look up data_source, call service fn, render render_template
  launcher    → render generic launcher_card.html (no service call)
  placeholder → render widget_placeholder.html (graceful, no crash)
  report      → reserved, renders widget_placeholder.html for now

Never raises 500 to the calling page — all errors degrade to an error partial.
The calling page (HTMX swap target) never sees an unhandled exception.
"""

import importlib
import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/widgets", tags=["widget-registry"])

# ---------------------------------------------------------------------------
# Template environment — covers all widget partials across modules
# Search path order: core widgets dir first, then module-level widget dirs
# ---------------------------------------------------------------------------

_TEMPLATE_DIRS = [
    "/app/modules/widgets/templates",   # core widget partials (launcher_card, placeholder, error)
    "/app/modules/dms/templates",       # dms widget partials
    "/app/modules/billing/templates",   # billing widget partials
    "/app/modules/dashboard/templates",  # dashboard widget partials
    "/app/modules/ediscovery/templates", # ediscovery widget partials
    "/app/modules/intelligence/templates", # intelligence widget partials
    "/app/core/templates",              # base, macros
]

_widget_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIRS),
    autoescape=select_autoescape(["html"]),
)


def _render(template_name: str, context: dict) -> str:
    """Render a widget partial. Returns HTML string. Never raises."""
    try:
        tmpl = _widget_env.get_template(template_name)
        return tmpl.render(context)
    except Exception as exc:
        logger.error("Widget template render error [%s]: %s", template_name, exc)
        return _render_error(f"Template error: {template_name}")


def _render_error(message: str) -> str:
    """Minimal inline error partial — never crashes the host page."""
    return (
        f'<div style="padding:10px 12px;background:#fef2f2;border:1px solid #fecaca;'
        f'border-radius:6px;font-size:11px;color:#991b1b;">'
        f'⚠ Widget error: {message}</div>'
    )


# ---------------------------------------------------------------------------
# Scope builder — assembles the canonical scope dict from request + params
# ---------------------------------------------------------------------------

def _build_scope(
    request: Request,
    matter_id: Optional[str],
    attorney_id: Optional[str],
    user_id: Optional[int],
    date_from: Optional[str],
    date_to: Optional[str],
    client_id: Optional[str] = None,
) -> dict:
    """
    Build the scope dict passed to every data_source function.
    tenant_id and user_id are always resolved from request.state first;
    explicit query params can override user_id (e.g. Attorney View tab).
    tenant_id is NEVER overridable from query params — always from session.
    """
    tenant_id = (getattr(request.state, "tenant_id", "") or "").strip()

    # user_id: param overrides state (Attorney View uses param to scope to another attorney)
    resolved_user_id = user_id
    if resolved_user_id is None:
        current_user = getattr(request.state, "current_user", None)
        resolved_user_id = getattr(current_user, "id", None)

    return {
        "tenant_id": tenant_id,
        "user_id": resolved_user_id,
        "matter_id": matter_id,
        "attorney_id": attorney_id,
        "date_from": date_from,
        "date_to": date_to,
        "client_id": client_id,
        "request": request,
    }


# ---------------------------------------------------------------------------
# Data source resolver — imports module.fn from dot-notation string
# Cached in-process after first resolution per slug
# ---------------------------------------------------------------------------

_fn_cache: dict = {}


def _resolve_data_source(data_source: str):
    """
    Resolve 'modules.dms.services.widget_service.get_recent_documents'
    to the actual callable. Cached after first call.
    Returns None if the function cannot be resolved (logs the error).
    """
    if data_source in _fn_cache:
        return _fn_cache[data_source]

    try:
        module_path, fn_name = data_source.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        fn = getattr(mod, fn_name)
        _fn_cache[data_source] = fn
        logger.debug("Resolved data_source: %s", data_source)
        return fn
    except Exception as exc:
        logger.error("Cannot resolve data_source [%s]: %s", data_source, exc)
        _fn_cache[data_source] = None
        return None


# ---------------------------------------------------------------------------
# Widget registry lookup
# ---------------------------------------------------------------------------

async def _lookup_widget(widget_slug: str, tenant_id: str) -> Optional[dict]:
    """
    Look up widget row. Checks tenant-custom rows first (tenant_id match),
    falls back to platform-standard rows (tenant_id IS NULL).
    Returns dict or None.
    """
    from core.db.base import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT
                widget_slug, widget_name, widget_type,
                data_source, render_template, target_route,
                default_size, permission_level, feature_flag,
                config_schema
            FROM widget_registry
            WHERE widget_slug = :slug
              AND (
                    (tenant_id IS NOT NULL AND trim(tenant_id) = trim(:tid))
                    OR tenant_id IS NULL
                  )
            ORDER BY
                CASE WHEN tenant_id IS NOT NULL THEN 0 ELSE 1 END
            LIMIT 1
        """), {"slug": widget_slug, "tid": tenant_id})
        row = r.mappings().fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Permission check — minimum role gate
# ---------------------------------------------------------------------------

async def _check_permission(widget: dict, request: Request) -> bool:
    """
    Returns True if the current user is permitted to view this widget.

    Uses PermissionService — replaces the legacy _ROLE_RANK ladder which
    didn't recognize paralegal/staff/read_only/client/deal_room_guest.

    Scope semantics:
      - PermissionService.can(user, "widgets", "view") returns whether the
        user can view ANY widgets at all.
      - If scope_filters["widget_slug"] is None, all widgets are visible.
      - If it's a list, this widget's slug must be in it.
      - If it's an empty list, deny all (treat empty as deny per the
        scope_filters contract).

    The widget_registry.permission_level column is no longer enforcement;
    the admin UI keeps it as display-only metadata indicating which role
    the widget was originally designed for.
    """
    current_user = getattr(request.state, "current_user", None)
    if current_user is None:
        return False

    # Lazy import to avoid circular dependency at module load.
    from core.auth.permissions import PermissionService

    perm = await PermissionService.can(current_user, "widgets", "view")
    if not perm.allowed:
        return False

    slug_filter = perm.scope_filters.get("widget_slug")
    if slug_filter is None:
        return True  # unrestricted
    if not slug_filter:
        return False  # empty list = deny all
    return widget.get("widget_slug") in slug_filter


# ---------------------------------------------------------------------------
# Main render route
# ---------------------------------------------------------------------------

@router.get("/{widget_slug}", response_class=HTMLResponse)
async def render_widget(
    request: Request,
    widget_slug: str,
    matter_id: Optional[str] = None,
    attorney_id: Optional[str] = None,
    user_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    client_id: Optional[str] = None,
    config: Optional[str] = None,
):
    """
    Generic widget render endpoint.
    Called via HTMX hx-get="/widgets/{widget_slug}?matter_id=...&..."
    Returns an HTML partial — never a full page.
    """
    tenant_id = (getattr(request.state, "tenant_id", "") or "").strip()

    # --- 1. Registry lookup ---
    widget = await _lookup_widget(widget_slug, tenant_id)
    if not widget:
        logger.warning("Widget not found in registry: %s", widget_slug)
        return HTMLResponse(_render_error(f"Unknown widget: {widget_slug}"))

    widget_type = widget.get("widget_type") or "placeholder"

    # --- 2. Permission gate ---
    if not await _check_permission(widget, request):
        return HTMLResponse(
            '<div style="padding:10px 12px;font-size:11px;color:var(--muted);">'
            'Insufficient permissions.</div>'
        )

    # --- 3. Feature flag gate ---
    feature_flag = widget.get("feature_flag")
    if feature_flag:
        # Feature flag check — reuse existing flag resolution pattern
        try:
            from core.db.base import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                r = await session.execute(sa_text("""
                    SELECT 1 FROM tenant_feature_flags
                    WHERE trim(tenant_id) = trim(:tid)
                      AND flag_name = :flag
                      AND enabled = TRUE
                    LIMIT 1
                """), {"tid": tenant_id, "flag": feature_flag})
                if not r.fetchone():
                    return HTMLResponse(
                        '<div style="padding:10px 12px;font-size:11px;color:var(--muted);">'
                        'Feature not enabled.</div>'
                    )
        except Exception as exc:
            logger.warning("Feature flag check failed for [%s]: %s", feature_flag, exc)

    # --- 4. Dispatch by widget_type ---

    scope = _build_scope(request, matter_id, attorney_id, user_id, date_from, date_to, client_id)

    if widget_type == "launcher":
        return HTMLResponse(_render("widgets/launcher_card.html", {
            "widget": widget,
            "scope": scope,
        }))

    if widget_type == "placeholder":
        return HTMLResponse(_render("widgets/widget_placeholder.html", {
            "widget": widget,
            "scope": scope,
        }))

    if widget_type in ("data_panel", "report"):
        data_source = widget.get("data_source")
        render_template = widget.get("render_template")

        if not data_source and render_template:
            import json as _j
            cfg = {}
            try: cfg = _j.loads(request.query_params.get("config","{}"))
            except: pass
            skip = {"config","matter_id","attorney_id","user_id","date_from","date_to"}
            for k,v in request.query_params.items():
                if k not in skip and k not in cfg:
                    cfg[k] = v
            return HTMLResponse(_render(render_template, {"widget": widget, "scope": scope, "config": cfg}))
        if not data_source:
            return HTMLResponse(_render_error(f"Widget {widget_slug} has no data_source"))
        if not render_template:
            return HTMLResponse(_render_error(f"Widget {widget_slug} has no render_template"))

        fn = _resolve_data_source(data_source)
        if fn is None:
            return HTMLResponse(_render_error(f"Cannot load data source for: {widget_slug}"))

        # Call the data source function — it receives the full scope dict
        # and returns a dict of template context variables
        try:
            import asyncio
            if asyncio.iscoroutinefunction(fn):
                data = await fn(scope)
            else:
                data = fn(scope)
        except Exception as exc:
            logger.error("Data source error [%s / %s]: %s", widget_slug, data_source, exc)
            return HTMLResponse(_render_error(f"Data error: {widget_slug}"))

        config_param = {}
        if config:
            try:
                import json as _j
                config_param = _j.loads(config)
            except Exception:
                pass
        # Also absorb loose query params (e.g. context=pi) into config
        _skip = {"config","matter_id","attorney_id","user_id","date_from","date_to","client_id"}
        for _k, _v in request.query_params.items():
            if _k not in _skip and _k not in config_param:
                config_param[_k] = _v

        context = {
            "widget": widget,
            "scope": scope,
            "config": config_param,
            # Promote scope keys to top-level for template convenience
            "client_id":   scope.get("client_id"),
            "matter_id":   scope.get("matter_id"),
            "attorney_id": scope.get("attorney_id"),
            "tenant_id":   scope.get("tenant_id"),
            **(data or {}),
        }
        return HTMLResponse(_render(render_template, context))

    # Fallback — unknown widget_type
    logger.warning("Unknown widget_type [%s] for slug [%s]", widget_type, widget_slug)
    return HTMLResponse(_render("widgets/widget_placeholder.html", {
        "widget": widget, "scope": scope,
    }))
