"""
modules/dashboard/routes/widget_layout_api.py
=============================================
Widget Layout CRUD — read, save, reset per-user dashboard layouts.
Used by both the UI editor and the AI chat (via MCP tools in future,
via system prompt instructions for now).

GET  /api/v1/layouts/{context_type}/{context_id}  — get effective layout
PUT  /api/v1/layouts/{context_type}/{context_id}  — save user layout
DELETE /api/v1/layouts/{context_type}/{context_id} — reset to default
GET  /api/v1/widgets/available                     — widget picker data
POST /api/v1/layouts/ai-edit                       — AI-driven layout edit

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations
import json, logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.widget_layout")
router = APIRouter(prefix="/api/v1", tags=["widget-layouts"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()
def _uid(r):
    u = getattr(r.state, "current_user", None)
    return int(getattr(u, "id", 0) or 0) if u else 0


@router.get("/layouts/{context_type}/{context_id}")
async def get_layout(request: Request, context_type: str, context_id: str):
    """Get effective layout. Resolution: user override > default > empty."""
    tid = _tid(request)
    uid = _uid(request)
    async with AsyncSessionLocal() as session:
        # User override
        r = await session.execute(sa_text("""
            SELECT widget_layout FROM user_widget_layouts
            WHERE context_type = :ct AND context_id = CAST(:cid AS uuid)
              AND user_id = :uid AND TRIM(tenant_id) = :tid
            ORDER BY updated_at DESC LIMIT 1
        """), {"ct": context_type, "cid": context_id, "uid": uid, "tid": tid})
        row = r.fetchone()
        if row:
            return JSONResponse({"source": "user", "layout": row[0]})

        # Default layout (user_id = 0 or is_default = true)
        r2 = await session.execute(sa_text("""
            SELECT widget_layout FROM user_widget_layouts
            WHERE context_type = :ct AND context_id = CAST(:cid AS uuid)
              AND (user_id = 0 OR is_default = true) AND TRIM(tenant_id) = :tid
            ORDER BY updated_at DESC LIMIT 1
        """), {"ct": context_type, "cid": context_id, "tid": tid})
        row2 = r2.fetchone()
        if row2:
            return JSONResponse({"source": "default", "layout": row2[0]})

    return JSONResponse({"source": "none", "layout": None})


@router.put("/layouts/{context_type}/{context_id}")
async def save_layout(request: Request, context_type: str, context_id: str):
    """Save a user's layout customization."""
    tid = _tid(request)
    uid = _uid(request)
    body = await request.json()
    layout = body.get("layout", {})

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(sa_text("""
                DELETE FROM user_widget_layouts
                WHERE context_type = :ct AND context_id = CAST(:cid AS uuid)
                  AND user_id = :uid AND TRIM(tenant_id) = :tid
            """), {"ct": context_type, "cid": context_id, "uid": uid, "tid": tid})
            await session.execute(sa_text("""
                INSERT INTO user_widget_layouts
                    (id, tenant_id, user_id, context_type, context_id, widget_layout)
                VALUES (gen_random_uuid(), :tid, :uid, :ct, CAST(:cid AS uuid),
                        CAST(:layout AS jsonb))
            """), {"tid": tid, "uid": uid, "ct": context_type, "cid": context_id,
                   "layout": json.dumps(layout)})
    return JSONResponse({"ok": True})


@router.delete("/layouts/{context_type}/{context_id}")
async def reset_layout(request: Request, context_type: str, context_id: str):
    """Reset user layout to default."""
    tid = _tid(request)
    uid = _uid(request)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(sa_text("""
                DELETE FROM user_widget_layouts
                WHERE context_type = :ct AND context_id = CAST(:cid AS uuid)
                  AND user_id = :uid AND TRIM(tenant_id) = :tid
                  AND (is_default IS NULL OR is_default = false)
            """), {"ct": context_type, "cid": context_id, "uid": uid, "tid": tid})
    return JSONResponse({"ok": True})


@router.get("/widgets/available")
async def available_widgets(request: Request, category: str = ""):
    """List widgets available for the picker."""
    tid = _tid(request)
    where = "WHERE TRIM(wr.tenant_id) = :tid AND wr.is_platform_standard = true"
    params = {"tid": tid}
    if category:
        where += " AND wr.category = :cat"
        params["cat"] = category

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(f"""
            SELECT wr.widget_slug, wr.widget_name, wr.category, wr.widget_type,
                   wr.default_size, wr.resizable, wr.icon, wr.component_path,
                   wr.config_schema
            FROM widget_registry wr
            {where}
            ORDER BY wr.category, wr.sort_order, wr.widget_name
        """), params)
        widgets = []
        for row in r.mappings():
            widgets.append(dict(row))
    return JSONResponse(widgets)


@router.post("/layouts/ai-edit")
async def ai_edit_layout(request: Request):
    """AI-driven layout edit. Takes natural language instruction, applies changes.
    
    Body:
      context_type: str
      context_id: str (matter UUID)
      instruction: str (e.g. "hide the drop zone", "put financial terms above property")
    
    Returns the updated layout.
    """
    tid = _tid(request)
    uid = _uid(request)
    body = await request.json()
    context_type = body.get("context_type", "matter_dashboard")
    context_id = body.get("context_id", "")
    instruction = body.get("instruction", "")

    if not instruction:
        return JSONResponse({"error": "instruction required"}, 400)

    # Get current layout
    current_layout = None
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT widget_layout FROM user_widget_layouts
            WHERE context_type = :ct AND context_id = CAST(:cid AS uuid)
              AND user_id = :uid AND TRIM(tenant_id) = :tid
            ORDER BY updated_at DESC LIMIT 1
        """), {"ct": context_type, "cid": context_id, "uid": uid, "tid": tid})
        row = r.fetchone()
        if row:
            current_layout = row[0]

    # Get available widgets
    async with AsyncSessionLocal() as session:
        wr = await session.execute(sa_text("""
            SELECT widget_slug, widget_name, category, icon
            FROM widget_registry
            WHERE TRIM(tenant_id) = :tid AND is_platform_standard = true
            ORDER BY category, sort_order
        """), {"tid": tid})
        available = [dict(r) for r in wr.mappings()]

    # Call Claude to edit the layout
    from modules.intelligence.matter_extract import _get_api_key, _call_claude

    api_key = await _get_api_key(tid)
    if not api_key:
        return JSONResponse({"error": "No API key"}, 400)

    system = f"""You are a dashboard layout editor for a legal practice management platform.
Given a current layout (JSON) and a user instruction, modify the layout.

Available widgets:
{json.dumps([{'slug': w['widget_slug'], 'name': w['widget_name'], 'category': w['category']} for w in available], indent=2)}

The layout is a JSON object with a "widgets" array. Each widget has:
  widget_slug, col_start (1-12), col_span (1-12), row (1-based)

Rules:
- 12-column grid
- col_start + col_span must be <= 13
- Preserve widgets the user doesn't mention
- To hide a widget, remove it from the array
- To add a widget, add it with appropriate position

Return ONLY valid JSON — the complete updated layout object."""

    user_msg = f"Current layout:\n{json.dumps(current_layout, indent=2)}\n\nInstruction: {instruction}"

    result = await _call_claude(api_key, system, user_msg)
    if not result:
        return JSONResponse({"error": "AI edit failed"}, 500)

    # Save the new layout
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(sa_text("""
                DELETE FROM user_widget_layouts
                WHERE context_type = :ct AND context_id = CAST(:cid AS uuid)
                  AND user_id = :uid AND TRIM(tenant_id) = :tid
            """), {"ct": context_type, "cid": context_id, "uid": uid, "tid": tid})
            await session.execute(sa_text("""
                INSERT INTO user_widget_layouts
                    (id, tenant_id, user_id, context_type, context_id, widget_layout)
                VALUES (gen_random_uuid(), :tid, :uid, :ct, CAST(:cid AS uuid),
                        CAST(:layout AS jsonb))
            """), {"tid": tid, "uid": uid, "ct": context_type, "cid": context_id,
                   "layout": json.dumps(result)})

    return JSONResponse({"ok": True, "layout": result, "instruction": instruction})
