"""Dashboard Routes — Page Registry & Dynamic Panel Engine.

Architecture:
  - dashboard_panel_registry DB table defines every panel
  - user_dashboard_layout DB table stores per-user arrangement
  - tenant_panel_defaults DB table stores per-firm defaults
  - Generic panel route dispatches to registered service functions
  - SortableJS drag-and-drop layout with persistent save
  - layout_tabs DB table drives tab bar — no hardcoded tabs

Routes:
  GET  /dashboard/                          — matter list home
  GET  /dashboard/matter/{matter_id}        — matter dashboard shell
  GET  /dashboard/matter/{matter_id}/panel/{panel_slug}  — generic HTMX panel
  POST /dashboard/layout/save               — save user panel order
  GET  /dashboard/gantt/firm                — firm gantt
  GET  /dashboard/gantt/my                  — my matters gantt
"""
from __future__ import annotations

import importlib
import json
import logging
from typing import Optional, Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from core.db.base import AsyncSessionLocal, TenantSession
from core.services.nav_context import get_nav_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
templates = Jinja2Templates(directory=["core/templates", "modules/dashboard/templates"])


# ── Session helpers ───────────────────────────────────────────

def get_db(request: Request) -> TenantSession:
    from core.db.base import get_session_factory, TenantSession as TS
    return TS(get_session_factory()(), request.state.tenant_id)


def get_brand(request: Request):
    return getattr(request.state, "branding", None)


# ── Tab loader — reads from layout_tabs table ─────────────────

async def _get_tabs(layout_slug: str, user_role: str) -> list[dict]:
    """
    Load tabs for a layout surface from layout_tabs table.
    Filters by permission_level and is_visible.
    Returns tabs ordered by display_order.

    Permission hierarchy: attorney < partner < admin < super_admin
    A tab with permission_level='admin' is visible to admin + super_admin only.
    """
    ROLE_RANK = {
        "attorney": 1, "paralegal": 1, "staff": 1,
        "partner": 2, "admin": 3, "super_admin": 4
    }
    user_rank = ROLE_RANK.get(user_role, 1)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT tab_slug, display_name, display_order,
                       permission_level, icon
                FROM layout_tabs
                WHERE layout_slug = :slug
                  AND is_visible = TRUE
                ORDER BY display_order ASC
            """),
            {"slug": layout_slug}
        )
        rows = [dict(r._mapping) for r in result.fetchall()]

    # Filter by permission
    visible = []
    for row in rows:
        required_rank = ROLE_RANK.get(row.get("permission_level", "attorney"), 1)
        if user_rank >= required_rank:
            visible.append(row)

    return visible


# ── Panel registry helpers ────────────────────────────────────

async def _get_panel_registry(tenant_id: str) -> list[dict]:
    """Load all enabled panels from DB, ordered by display_order."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT
                    p.panel_slug, p.display_name, p.description,
                    p.service_module, p.service_function,
                    p.template_name, p.display_order, p.panel_width,
                    p.practice_areas, p.required_permission,
                    p.feature_flag, p.is_enabled, p.is_default
                FROM dashboard_panel_registry p
                WHERE p.is_enabled = true
                ORDER BY p.display_order ASC
            """)
        )
        return [dict(r) for r in result.mappings().fetchall()]


async def _get_user_layout(tenant_id: str, user_id: int,
                            page_slug: str = "matter_dashboard") -> Optional[dict]:
    """Load user's saved panel layout."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT panel_order, hidden_panels
                FROM user_dashboard_layout
                WHERE trim(tenant_id) = trim(:tid)
                  AND user_id = :uid
                  AND page_slug = :slug
            """),
            {"tid": tenant_id, "uid": user_id, "slug": page_slug},
        )
        row = result.fetchone()
        if not row:
            return None
        panel_order = row[0]
        hidden_panels = row[1]
        if isinstance(panel_order, str):
            panel_order = json.loads(panel_order)
        if isinstance(hidden_panels, str):
            hidden_panels = json.loads(hidden_panels)
        return {"panel_order": panel_order or [], "hidden_panels": hidden_panels or []}


async def _get_tenant_defaults(tenant_id: str) -> list[dict]:
    """Load tenant panel defaults."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT panel_slug, is_enabled, display_order, is_locked
                FROM tenant_panel_defaults
                WHERE trim(tenant_id) = trim(:tid)
                ORDER BY display_order ASC
            """),
            {"tid": tenant_id},
        )
        return [dict(r) for r in result.mappings().fetchall()]


async def _resolve_panel_order(
    tenant_id: str,
    user_id: int,
    registry: list[dict],
    matter_practice_area: Optional[str] = None,
) -> list[dict]:
    def panel_applies(p: dict) -> bool:
        areas = p.get("practice_areas")
        if not areas:
            return True
        if isinstance(areas, str):
            try:
                areas = json.loads(areas)
            except Exception:
                return True
        if not areas:
            return True
        if not matter_practice_area:
            return p.get("is_default", True)
        return matter_practice_area in areas

    visible = [p for p in registry if panel_applies(p)]

    tenant_defaults = await _get_tenant_defaults(tenant_id)
    tenant_map = {d["panel_slug"]: d for d in tenant_defaults}
    for p in visible:
        td = tenant_map.get(p["panel_slug"])
        if td:
            p["display_order"] = td["display_order"]
            p["is_locked"] = td["is_locked"]
            if not td["is_enabled"]:
                p["tenant_disabled"] = True

    visible = [p for p in visible if not p.get("tenant_disabled")]

    user_layout = await _get_user_layout(tenant_id, user_id)
    if user_layout and user_layout["panel_order"]:
        order_map = {slug: i for i, slug in enumerate(user_layout["panel_order"])}
        hidden = set(user_layout["hidden_panels"])
        visible = [p for p in visible if p["panel_slug"] not in hidden]
        visible.sort(key=lambda p: order_map.get(p["panel_slug"], p["display_order"]))
    else:
        visible.sort(key=lambda p: p["display_order"])

    return visible


async def _dispatch_panel(panel: dict, tenant_id: str, matter_id: str) -> Any:
    module_path = panel.get("service_module")
    func_name = panel.get("service_function")

    if not module_path or not func_name:
        return None

    try:
        mod = importlib.import_module(module_path)
        func = getattr(mod, func_name)
        return await func(tenant_id, matter_id)
    except Exception as exc:
        logger.error(
            "Panel dispatch failed [%s.%s] matter=%s: %s",
            module_path, func_name, matter_id, exc,
        )
        return None


# ── Dashboard Home ────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    """Practice Intelligence Dashboard — data-driven tab composition."""
    tenant_id = request.state.tenant_id
    brand = get_brand(request)
    user = getattr(request.state, "current_user", None)
    role = getattr(user, "role", "attorney") if user else "attorney"

    # Load tabs from DB
    tabs = await _get_tabs("practice_intelligence", role)

    # Fallback if layout_tabs not yet migrated
    if not tabs:
        tabs = [
            {"tab_slug": "firm_view",     "display_name": "Firm View",     "display_order": 1},
            {"tab_slug": "attorney_view", "display_name": "Attorney View", "display_order": 2},
            {"tab_slug": "matter_view",   "display_name": "Matter View",   "display_order": 3},
            {"tab_slug": "my_view",       "display_name": "My View",       "display_order": 4},
        ]

    # Determine if user can see attorney picker
    is_partner_or_admin = role in ("admin", "super_admin")

    # Load attorney list for Attorney View tab (partner/admin only)
    attorneys = []
    if is_partner_or_admin:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT id::text, full_name
                    FROM users
                    WHERE trim(tenant_id) = trim(:tid)
                      AND is_active = TRUE
                      AND role::text IN ('attorney', 'admin', 'super_admin')
                    ORDER BY full_name
                """),
                {"tid": tenant_id.strip()},
            )
            attorneys = [dict(r._mapping) for r in result.fetchall()]

    # Load dynamic nav for shell.html
    nav_ctx = await get_nav_context(request)

    return templates.TemplateResponse(
        "dashboard/practice_intelligence_react.html",
        {
            "request":            request,
            "brand":              brand,
            "user":               user,
            "current_user":       user,
            "page":               "dashboard",
            "tabs":               tabs,
            "is_partner_or_admin": is_partner_or_admin,
            "attorneys":          attorneys,
            "default_tab":        tabs[0]["tab_slug"] if tabs else "firm_view",
            **nav_ctx,
        },
    )


# ── Matter Dashboard Shell ────────────────────────────────────
# DEPRECATED: replaced by modules.dashboard.routes.matter_detail
# Route kept as dead code — matter_detail router registered first in app.py
# Remove after confirming matter_detail is stable.

# @router.get("/matter/{matter_id}", response_class=HTMLResponse)
async def matter_dashboard_DEPRECATED(request: Request, matter_id: str):
    tenant_id = request.state.tenant_id
    brand = get_brand(request)
    user = getattr(request.state, "current_user", None)
    user_id = getattr(user, "id", 0) if user else 0

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT * FROM matters WHERE trim(tenant_id) = trim(:tid) AND id = :mid"),
            {"tid": tenant_id, "mid": matter_id},
        )
        rows = [dict(r._mapping) for r in result.fetchall()]

    if not rows:
        raise HTTPException(status_code=404, detail="Matter not found")
    matter = rows[0]

    registry = await _get_panel_registry(tenant_id)
    panels = await _resolve_panel_order(
        tenant_id, user_id, registry,
        matter_practice_area=matter.get("practice_area"),
    )

    return templates.TemplateResponse(
        "dashboard/matter.html",
        {
            "request":      request,
            "brand":        brand,
            "matter":       matter,
            "matter_id":    matter_id,
            "panels":       panels,
            "user":         user,
            "current_user": user,
            "page":         "dashboard",
        },
    )


# ── Generic Panel Route ───────────────────────────────────────
# DEPRECATED: replaced by widget grid model
# @router.get("/matter/{matter_id}/panel/{panel_slug}", response_class=HTMLResponse)
async def matter_panel_DEPRECATED(request: Request, matter_id: str, panel_slug: str):
    tenant_id = request.state.tenant_id

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT panel_slug, display_name, service_module, service_function,
                       template_name, panel_width
                FROM dashboard_panel_registry
                WHERE panel_slug = :slug AND is_enabled = true
            """),
            {"slug": panel_slug},
        )
        row = result.mappings().fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"Panel '{panel_slug}' not found")

    panel = dict(row)
    data = await _dispatch_panel(panel, tenant_id, matter_id)

    context = {
        "request":   request,
        "brand":     get_brand(request),
        "matter_id": matter_id,
        "panel":     panel,
    }

    if panel_slug == "summary" and isinstance(data, dict):
        context["summary"] = data.get("summary")
        context["causes_of_action"] = data.get("causes_of_action", [])
    elif panel_slug == "gantt" and isinstance(data, str):
        context["gantt_svg"] = data
    elif panel_slug == "tasks":
        context["tasks"] = data or []
    elif panel_slug == "communications":
        context["communications"] = data or []
    elif panel_slug == "settlements":
        context["settlements"] = data or []
    elif panel_slug == "experts":
        context["experts"] = data or []
    elif panel_slug == "mediations":
        context["mediations"] = data or []
    elif panel_slug == "engagement":
        context["letters"] = data or []
    elif panel_slug == "title_comparison":
        context["comparison"] = data[0] if data else None
    else:
        context["data"] = data

    return templates.TemplateResponse(panel["template_name"], context)


# ── Layout Save ───────────────────────────────────────────────

@router.post("/layout/save")
async def save_layout(request: Request):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    tenant_id = request.state.tenant_id
    body = await request.json()
    page_slug = body.get("page_slug", "matter_dashboard")
    panel_order = body.get("panel_order", [])
    hidden_panels = body.get("hidden_panels", [])

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO user_dashboard_layout
                    (tenant_id, user_id, page_slug, panel_order, hidden_panels, updated_at)
                VALUES (:tid, :uid, :slug, CAST(:order AS jsonb), CAST(:hidden AS jsonb), NOW())
                ON CONFLICT (tenant_id, user_id, page_slug)
                DO UPDATE SET
                    panel_order   = CAST(:order AS jsonb),
                    hidden_panels = CAST(:hidden AS jsonb),
                    updated_at    = NOW()
            """),
            {
                "tid":    tenant_id,
                "uid":    user.id,
                "slug":   page_slug,
                "order":  json.dumps(panel_order),
                "hidden": json.dumps(hidden_panels),
            },
        )
        await session.commit()

    return JSONResponse({"status": "saved"})


# ── Panel Toggle ──────────────────────────────────────────────

@router.post("/layout/toggle/{panel_slug}")
async def toggle_panel(request: Request, panel_slug: str):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    tenant_id = request.state.tenant_id
    layout = await _get_user_layout(tenant_id, user.id) or \
             {"panel_order": [], "hidden_panels": []}
    hidden = set(layout["hidden_panels"])

    if panel_slug in hidden:
        hidden.discard(panel_slug)
        visible = True
    else:
        hidden.add(panel_slug)
        visible = False

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO user_dashboard_layout
                    (tenant_id, user_id, page_slug, panel_order, hidden_panels, updated_at)
                VALUES (:tid, :uid, 'matter_dashboard',
                        CAST(:order AS jsonb), CAST(:hidden AS jsonb), NOW())
                ON CONFLICT (tenant_id, user_id, page_slug)
                DO UPDATE SET
                    hidden_panels = CAST(:hidden AS jsonb),
                    updated_at    = NOW()
            """),
            {
                "tid":    tenant_id,
                "uid":    user.id,
                "order":  json.dumps(layout["panel_order"]),
                "hidden": json.dumps(list(hidden)),
            },
        )
        await session.commit()

    return JSONResponse({"panel_slug": panel_slug, "visible": visible})


# ── Gantt endpoints ───────────────────────────────────────────

@router.get("/gantt/firm", response_class=HTMLResponse)
async def firm_gantt(request: Request):
    tenant_id = request.state.tenant_id
    from modules.dashboard.services.gantt import generate_firm_gantt
    svg = await generate_firm_gantt(tenant_id)
    return HTMLResponse(content=svg)


@router.get("/gantt/my", response_class=HTMLResponse)
async def my_gantt(request: Request):
    tenant_id = request.state.tenant_id
    user = getattr(request.state, "current_user", None)
    user_id = getattr(user, "id", 0) if user else 0
    from modules.dashboard.services.gantt import generate_my_matters_gantt
    svg = await generate_my_matters_gantt(tenant_id, user_id)
    return HTMLResponse(content=svg)


# ── Mutation endpoints ────────────────────────────────────────

@router.post("/intake/start")
async def start_intake(request: Request, db: TenantSession = Depends(get_db)):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from modules.dashboard.services.intake import create_intake_session
    session = await create_intake_session(db, user.id)
    return {"intake_id": session.id, "status": session.status}


@router.post("/matter/{matter_id}/task")
async def create_task_endpoint(request: Request, matter_id: str):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    tenant_id = request.state.tenant_id
    from modules.dashboard.services.task_system import create_task
    body = await request.json()
    task = await create_task(
        tenant_id, title=body["title"],
        description=body.get("description"),
        matter_id=matter_id,
        priority=body.get("priority", "medium"),
        created_by=user.id,
        assigned_to=body.get("assigned_to"),
    )
    return {"task_id": task["id"], "status": task["status"]}


@router.put("/task/{task_id}/status")
async def update_task(request: Request, task_id: int):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    tenant_id = request.state.tenant_id
    from modules.dashboard.services.task_system import update_task_status
    body = await request.json()
    task = await update_task_status(tenant_id, task_id, body["status"], user.id)
    return {"task_id": task["id"], "status": task["status"]}


@router.get("/matter/{matter_id}/coa/{coa_id}/elements",
            response_class=HTMLResponse)
async def get_coa_elements(request: Request, matter_id: str, coa_id: int):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    tenant_id = request.state.tenant_id.strip()

    from modules.dashboard.services.case_summary import get_coa_elements as _get_elements

    async with AsyncSessionLocal() as session:
        coa_row = await session.execute(
            text("""
                SELECT id, title, count_number, status, ai_summary
                FROM causes_of_action
                WHERE id = :coa_id
                  AND matter_id = CAST(:matter_id AS uuid)
                  AND tenant_id = :tid
            """),
            {"coa_id": coa_id, "matter_id": matter_id, "tid": tenant_id},
        )
        coa = coa_row.mappings().fetchone()

    if not coa:
        raise HTTPException(status_code=404, detail="Cause of action not found")

    elements = await _get_elements(tenant_id, coa_id)
    branding = getattr(request.state, "branding", None)

    return templates.TemplateResponse(
        request, "coa_elements_partial.html",
        {
            "coa":       dict(coa),
            "elements":  elements,
            "matter_id": matter_id,
            "branding":  branding,
            "user":      user,
        },
    )


@router.post("/matter/{matter_id}/coa/{element_id}/override")
async def override_coa(request: Request, matter_id: str, element_id: int):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    tenant_id = request.state.tenant_id
    from modules.dashboard.services.case_summary import override_coa_element
    body = await request.json()
    elem = await override_coa_element(
        tenant_id, element_id, body["status"],
        body.get("note", ""), user.id
    )
    return {"element_id": elem["id"], "new_status": elem["attorney_override_status"]}


@router.post("/matter/{matter_id}/communication")
async def log_comm_endpoint(request: Request, matter_id: str):
    user = getattr(request.state, "current_user", None)
    tenant_id = request.state.tenant_id
    from modules.dashboard.services.matter_modules import log_communication
    body = await request.json()
    entry = await log_communication(
        tenant_id, matter_id,
        channel=body["channel"], direction=body["direction"],
        subject=body.get("subject"), summary=body.get("summary"),
        logged_by=user.id if user else None,
    )
    return {"communication_id": entry["id"]}


@router.post("/matter/{matter_id}/settlement")
async def record_settlement(request: Request, matter_id: str):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    tenant_id = request.state.tenant_id
    from modules.dashboard.services.matter_modules import record_settlement_event
    from decimal import Decimal
    body = await request.json()
    s = await record_settlement_event(
        tenant_id, matter_id,
        offer_type=body["offer_type"],
        amount=Decimal(str(body["amount"])) if body.get("amount") else None,
        offered_by=body.get("offered_by"),
        terms_summary=body.get("terms_summary"),
        user_id=user.id,
    )
    return {"settlement_id": s["id"]}


@router.get("/matter/{matter_id}/status-report")
async def generate_status_report(request: Request, matter_id: str):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    tenant_id = request.state.tenant_id
    from modules.dashboard.services.case_summary import generate_status_report as gen_report
    report = await gen_report(tenant_id, matter_id)
    return {"report": report}



@router.get("/communications")
async def communications_center(request: Request):
    """Communications Center — unified email/SMS/PBX."""
    brand = get_brand(request)
    user = getattr(request.state, "current_user", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse("dashboard/comms_center_react.html", {
        "request": request, "brand": brand, "current_user": user,
        "page": "communications", **nav_ctx,
    })
