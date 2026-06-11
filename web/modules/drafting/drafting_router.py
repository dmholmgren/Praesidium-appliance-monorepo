"""
modules/drafting/drafting_router.py — v2
=========================================

Appliance rewrite — Document Drafting UI.

v2 changes: matter-search returns clickable div items (not datalist options).

Patent Pending - Series 2/3 - D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.drafting")

router = APIRouter(prefix="/drafting", tags=["drafting"])

templates = Jinja2Templates(directory=[
    "core/templates",
    "modules/billing/templates",
])


# --------------------------------------------------------------------------
# Auth / context helpers
# --------------------------------------------------------------------------
def _strip(v: Optional[str]) -> str:
    return (v or "").strip()


async def _require_user(request: Request) -> Dict[str, Any]:
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(401, "Not authenticated")
    tid = getattr(request.state, "tenant_id", None) or getattr(user, "tenant_id", None)
    if not tid:
        raise HTTPException(401, "Tenant not resolved")
    return {
        "user_id": int(user.id),
        "tenant_id": _strip(tid),
        "role": getattr(user, "role", None) or "staff",
    }


def _ctx(request: Request, sess: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    from modules.billing.brand_helper import get_brand
    user = getattr(request.state, "current_user", None)
    return {
        "request": request,
        "brand": get_brand(request),
        "page": "drafting",
        "bill_tab": "",
        "user": user,
        "current_user": user,
        **kwargs,
    }


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------
async def _search_matters(tenant_id: str, q: str = "", limit: int = 20) -> List[Any]:
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT m.id, m.matter_number, m.matter_name, m.status,
                       c.client_name
                FROM matters m
                LEFT JOIN clients c ON c.id = m.client_id
                WHERE TRIM(m.tenant_id) = :tid
                  AND (m.matter_number ILIKE :p OR m.matter_name ILIKE :p
                       OR c.client_name ILIKE :p)
                ORDER BY m.matter_name
                LIMIT :lim
            """),
            {"tid": tenant_id, "p": f"%{q.strip()}%", "lim": limit},
        )
        return r.fetchall()


async def _get_matter(matter_id: str, tenant_id: str):
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT m.id, m.matter_number, m.matter_name, m.status,
                       m.matter_type, m.practice_area,
                       c.client_name
                FROM matters m
                LEFT JOIN clients c ON c.id = m.client_id
                WHERE m.id = CAST(:mid AS uuid)
                  AND TRIM(m.tenant_id) = :tid
            """),
            {"mid": matter_id, "tid": tenant_id},
        )
        return r.fetchone()


async def _get_recent_sessions(
    tenant_id: str, matter_id: Optional[str] = None, limit: int = 10
) -> List[Any]:
    filters = ["TRIM(ds.tenant_id) = :tid"]
    params: Dict[str, Any] = {"tid": tenant_id, "lim": limit}
    if matter_id:
        filters.append("ds.matter_id = CAST(:mid AS uuid)")
        params["mid"] = matter_id
    where = " AND ".join(filters)
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text(f"""
                SELECT ds.id, ds.title, ds.document_type, ds.status,
                       ds.created_at, ds.updated_at,
                       m.matter_name, m.matter_number
                FROM drafting_sessions ds
                LEFT JOIN matters m ON m.id = ds.matter_id
                WHERE {where}
                ORDER BY ds.updated_at DESC NULLS LAST
                LIMIT :lim
            """),
            params,
        )
        return r.fetchall()


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def drafting_home(request: Request):
    sess = await _require_user(request)
    tid = sess["tenant_id"]
    recent = await _get_recent_sessions(tid, limit=5)

    from core.services.nav_context import get_nav_context
    nav_ctx = await get_nav_context(request)
    brand = getattr(request.state, "branding", None)
    return templates.TemplateResponse(
        request,
        "drafting/drafting_home_react.html",
        {"request": request, "brand": brand, "page": "drafting",
         "current_user": getattr(request.state, "current_user", None),
         **nav_ctx})
    # OLD HTMX PATH (preserved):
    # return templates.TemplateResponse(
    #     request,
    #     "drafting/drafting_home.html",
    #     _ctx(request, sess,
    #          recent_sessions=recent,
    #          document_types=[
#                 ("motion", "Motion"),
#                 ("brief", "Brief"),
#                 ("contract", "Contract"),
#                 ("discovery_request", "Discovery Request"),
#                 ("discovery_response", "Discovery Response"),
#                 ("pleading", "Pleading"),
#                 ("letter", "Letter"),
#                 ("disclosure", "Disclosure"),
#                 ("agreement", "Agreement"),
#                 ("other", "Other"),
#             ]),
    #)


# --------------------------------------------------------------------------
# HTMX API endpoints
# --------------------------------------------------------------------------
@router.get("/api/matter-search", response_class=HTMLResponse)
async def matter_search(request: Request, q: str = ""):
    """Returns clickable div items for the matter picker dropdown."""
    sess = await _require_user(request)
    if len(q.strip()) < 2:
        return HTMLResponse("")
    rows = await _search_matters(sess["tenant_id"], q)
    if not rows:
        return HTMLResponse(
            '<div style="padding:10px 12px; font-size:12px; color:var(--muted);">'
            'No matters found</div>'
        )
    parts = []
    for r in rows:
        label = f"{r.matter_number or ''} {r.matter_name}".strip()
        client = r.client_name or ""
        status = r.status or ""
        # Escape quotes in data attributes
        safe_label = label.replace('"', '&quot;')
        safe_client = client.replace('"', '&quot;')
        parts.append(
            f'<div class="picker-item" '
            f'data-id="{r.id}" data-label="{safe_label}" data-client="{safe_client}">'
            f'<div class="picker-item-name">{label}</div>'
            f'<div class="picker-item-meta">'
            f'{client}{" &middot; " if client and status else ""}{status}'
            f'</div></div>'
        )
    return HTMLResponse("\n".join(parts))


@router.get("/api/exemplar-matters", response_class=HTMLResponse)
async def exemplar_matters_partial(
    request: Request,
    exemplar_q: str = "",
    exemplar_exclude: str = "",
):
    sess = await _require_user(request)
    tid = sess["tenant_id"]
    q = exemplar_q
    exclude = exemplar_exclude
    filters = ["TRIM(m.tenant_id) = :tid"]
    params: Dict[str, Any] = {"tid": tid, "lim": 20}
    if exclude:
        filters.append("m.id != CAST(:exc AS uuid)")
        params["exc"] = exclude
    if q and len(q.strip()) >= 2:
        filters.append(
            "(m.matter_number ILIKE :p OR m.matter_name ILIKE :p"
            " OR cl.client_name ILIKE :p)"
        )
        params["p"] = f"%{q.strip()}%"
    where = " AND ".join(filters)
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text(f"""
                SELECT m.id, m.matter_number, m.matter_name, m.status,
                       cl.client_name,
                       (SELECT COUNT(*) FROM dms_documents d
                        WHERE d.matter_id = m.id
                          AND TRIM(d.tenant_id) = TRIM(m.tenant_id)) AS doc_count
                FROM matters m
                LEFT JOIN clients cl ON cl.id = m.client_id
                WHERE {where}
                ORDER BY m.matter_name
                LIMIT :lim
            """),
            params,
        )
        rows = r.fetchall()

    if not rows:
        return HTMLResponse(
            '<div style="font-size:12px; color:var(--muted); padding:12px;">'
            'No matching matters found.</div>'
        )

    parts = []
    for r in rows:
        label = f"{r.matter_number or ''} {r.matter_name}".strip()
        client = r.client_name or ""
        doc_count = r.doc_count or 0
        parts.append(f"""
        <label style="display:flex; align-items:center; gap:10px; padding:8px 12px;
                      border:1px solid var(--border); border-radius:6px; cursor:pointer;
                      font-size:13px; transition:background 100ms;"
               onmouseover="this.style.background='var(--bg)'"
               onmouseout="this.style.background='transparent'">
          <input type="checkbox" name="exemplar_matter_ids" value="{r.id}"
                 onchange="toggleExemplar(this)">
          <div style="flex:1; min-width:0;">
            <div style="font-weight:500; overflow:hidden; text-overflow:ellipsis;
                        white-space:nowrap;">{label}</div>
            <div style="font-size:11px; color:var(--muted);">
              {client}{"  &middot;  " if client else ""}{doc_count} doc{"s" if doc_count != 1 else ""}
            </div>
          </div>
          <span class="badge badge-gray" style="font-size:10px;">{r.status or "&#8212;"}</span>
        </label>""")
    return HTMLResponse("\n".join(parts))


@router.post("/api/upload-goby", response_class=HTMLResponse)
async def upload_goby(request: Request, files: List[UploadFile] = File(...)):
    sess = await _require_user(request)
    parts = []
    for f in files:
        content = await f.read()
        size_bytes = len(content)
        if size_bytes > 50 * 1024 * 1024:
            parts.append(f"""
            <div style="display:flex; align-items:center; gap:10px; padding:8px 12px;
                        border:1px solid #FCA5A5; border-radius:6px; font-size:12px;
                        color:#991B1B; background:#FEF2F2;">
              &#9888; {f.filename} exceeds 50 MB limit
            </div>""")
            continue

        if size_bytes < 1024:
            size_str = f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            size_str = f"{size_bytes / 1024:.1f} KB"
        else:
            size_str = f"{size_bytes / (1024*1024):.1f} MB"

        ext = (f.filename or "").rsplit(".", 1)[-1].lower() if f.filename else ""
        icon_map = {"pdf": "&#128213;", "doc": "&#128216;", "docx": "&#128216;",
                    "xls": "&#128215;", "xlsx": "&#128215;", "txt": "&#128221;",
                    "rtf": "&#128221;"}
        icon = icon_map.get(ext, "&#128196;")
        file_id = str(uuid.uuid4())[:8]

        parts.append(f"""
        <div id="goby-{file_id}"
             style="display:flex; align-items:center; gap:10px; padding:8px 12px;
                    border:1px solid var(--border); border-radius:6px; font-size:13px;
                    background:var(--surface);">
          <span style="font-size:18px;">{icon}</span>
          <div style="flex:1; min-width:0;">
            <div style="font-weight:500; overflow:hidden; text-overflow:ellipsis;
                        white-space:nowrap;">{f.filename}</div>
            <div style="font-size:11px; color:var(--muted);">{size_str}</div>
          </div>
          <button type="button" onclick="this.closest('[id^=goby-]').remove()"
                  style="background:none; border:none; cursor:pointer;
                         font-size:16px; color:var(--muted); padding:4px;"
                  title="Remove">&times;</button>
        </div>""")
    return HTMLResponse("\n".join(parts))


@router.post("/api/chat", response_class=HTMLResponse)
async def chat_message(
    request: Request,
    message: str = Form(""),
    matter_id: str = Form(""),
    document_type: str = Form(""),
    exemplar_matter_ids: str = Form(""),
):
    sess = await _require_user(request)
    tid = sess["tenant_id"]
    msg = _strip(message)
    if not msg:
        return HTMLResponse("")

    context_parts = []
    if matter_id:
        matter = await _get_matter(matter_id, tid)
        if matter:
            context_parts.append(
                f"Matter: {matter.matter_number or ''} {matter.matter_name}".strip()
            )
    if document_type:
        context_parts.append(f"Type: {document_type.replace('_', ' ').title()}")
    if exemplar_matter_ids:
        ids = [x.strip() for x in exemplar_matter_ids.split(",") if x.strip()]
        if ids:
            context_parts.append(f"{len(ids)} exemplar matter{'s' if len(ids) != 1 else ''}")

    ctx = " &middot; ".join(context_parts) if context_parts else "No matter selected"
    ts = datetime.now().strftime("%I:%M %p")

    html = f"""
    <div class="chat-msg chat-msg-user">
      <div class="chat-bubble chat-bubble-user">
        {msg}
        <div class="chat-ts" style="text-align:right;">{ts}</div>
      </div>
    </div>
    <div class="chat-msg chat-msg-system">
      <div class="chat-bubble chat-bubble-system">
        <div style="font-size:10px; color:var(--muted); margin-bottom:6px;">{ctx}</div>
        Drafting AI is ready. This chat surface will connect to the AI pipeline
        with full matter context, exemplar documents, and go-by files when the
        assembly service is invoked.
        <div class="chat-ts">{ts}</div>
      </div>
    </div>
    """
    return HTMLResponse(html)
