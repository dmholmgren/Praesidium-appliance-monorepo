"""
Praesidium Mobile API — Mobile API Layer 1002
Patent Pending — 64/015,486, 64/020,027, 64/033,333

Lightweight endpoints optimized for mobile client consumption.
Reuses existing desktop JWT auth (require_desktop_user).
"""

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime
from uuid import uuid4
import json as _json
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mobile", tags=["mobile"])

try:
    from modules.desktop.checkout_router import require_desktop_user
except ImportError:
    async def require_desktop_user():
        raise HTTPException(401, "Desktop auth module not available")


class TimeEntryCreate(BaseModel):
    matter_id: str
    narrative: str
    hours: float
    work_date: Optional[str] = None
    status: Optional[str] = "draft"

class TimeEntryResponse(BaseModel):
    id: str
    matter_id: str
    narrative: str
    hours: float
    work_date: str
    status: str
    created_at: str


@router.post("/time-entries", response_model=TimeEntryResponse)
async def create_time_entry(
    entry: TimeEntryCreate,
    request: Request,
    claims=Depends(require_desktop_user),
):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text

    tenant_id = claims.tenant_id
    user_id = claims.user_id
    entry_id = str(uuid4())
    work_date = entry.work_date or date.today().isoformat()
    now = datetime.utcnow().isoformat()
    if not await _matter_belongs(entry.matter_id, tenant_id):
        raise HTTPException(404, "matter not found")
    rate, amount = await _resolve_rate_amount(tenant_id, user_id, entry.hours)

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO time_entries (
                    id, tenant_id, matter_id, user_id,
                    description, hours, rate, amount,
                    date, status, source, created_at, updated_at
                ) VALUES (
                    CAST(:id AS uuid), CAST(:tenant_id AS char(36)),
                    CAST(:matter_id AS uuid), :user_id,
                    :narrative, :hours, :rate, :amount,
                    CAST(:work_date AS date), :status, 'mobile', NOW(), NOW()
                )
            """),
            {
                "id": entry_id, "tenant_id": tenant_id,
                "matter_id": entry.matter_id, "user_id": user_id,
                "narrative": entry.narrative, "hours": entry.hours,
                "rate": rate, "amount": amount,
                "work_date": date.fromisoformat(work_date), "status": entry.status or "draft",
            },
        )
        await session.commit()

    return TimeEntryResponse(
        id=entry_id, matter_id=entry.matter_id,
        narrative=entry.narrative, hours=entry.hours,
        work_date=work_date, status=entry.status or "draft",
        created_at=now,
    )


@router.get("/time-entries")
async def list_time_entries(
    request: Request,
    claims=Depends(require_desktop_user),
    matter_id: Optional[str] = None,
    status: Optional[str] = "draft",
    limit: int = 50,
):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text

    tenant_id = claims.tenant_id
    user_id = claims.user_id
    query = """
        SELECT te.id, te.matter_id, te.description AS narrative, te.hours, te.rate, te.amount,
               te.date AS work_date, te.status, te.source, te.created_at,
               m.matter_name AS matter_name, c.client_name AS client_name
        FROM time_entries te
        LEFT JOIN matters m ON te.matter_id = m.id AND TRIM(m.tenant_id) = TRIM(:tenant_id)
        LEFT JOIN clients c ON m.client_id = c.id AND TRIM(c.tenant_id) = TRIM(:tenant_id)
        WHERE TRIM(te.tenant_id) = TRIM(:tenant_id)
          AND te.user_id = :user_id
    """
    params = {"tenant_id": tenant_id, "user_id": user_id, "limit": limit}
    if matter_id:
        query += " AND te.matter_id = CAST(:matter_id AS uuid)"
        params["matter_id"] = matter_id
    if status:
        query += " AND te.status = :status"
        params["status"] = status
    query += " ORDER BY te.date DESC, te.created_at DESC LIMIT :limit"

    async with AsyncSessionLocal() as session:
        result = await session.execute(text(query), params)
        rows = result.mappings().all()

    return {"entries": [
        {
            "id": str(r["id"]),
            "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
            "matter_name": r["matter_name"], "client_name": r["client_name"],
            "narrative": r["narrative"],
            "hours": float(r["hours"]) if r["hours"] else 0,
            "rate": float(r["rate"]) if r["rate"] else 0,
            "amount": float(r["amount"]) if r["amount"] else 0,
            "work_date": str(r["work_date"]) if r["work_date"] else None,
            "status": r["status"], "source": r["source"],
            "created_at": str(r["created_at"]) if r["created_at"] else None,
        }
        for r in rows
    ]}


@router.get("/dashboard")
async def mobile_dashboard(
    request: Request,
    claims=Depends(require_desktop_user),
):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text

    tenant_id = claims.tenant_id
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text("SELECT COUNT(*) AS cnt FROM matters WHERE TRIM(tenant_id) = TRIM(:tid)"),
            {"tid": tenant_id},
        )
        matter_count = r.scalar() or 0
        r = await session.execute(
            text("""
                SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS wip,
                       COALESCE(SUM(hours), 0) AS hours
                FROM time_entries WHERE TRIM(tenant_id) = TRIM(:tid) AND status = 'draft'
            """),
            {"tid": tenant_id},
        )
        row = r.mappings().first()

    return {
        "matter_count": matter_count,
        "draft_time_entries": row["cnt"] if row else 0,
        "wip": float(row["wip"]) if row else 0,
        "hours": float(row["hours"]) if row else 0,
    }


# ── PWA routes ───────────────────────────────────────────────────────

pwa_router = APIRouter(tags=["mobile-pwa"])

@pwa_router.get("/mobile/", response_class=HTMLResponse)
@pwa_router.get("/mobile", response_class=HTMLResponse)
async def serve_mobile_pwa(request: Request):
    from fastapi.templating import Jinja2Templates
    import os
    for td in ["/app/core/templates/mobile", "/app/core/templates"]:
        if os.path.exists(os.path.join(td, "mobile_pwa.html")):
            templates = Jinja2Templates(directory=td)
            return templates.TemplateResponse("mobile_pwa.html", {
                "request": request,
                "tenant_id": getattr(request.state, "tenant_id", ""),
            })
    return HTMLResponse("<h2>Mobile client not deployed</h2>")

@pwa_router.get("/sw.js")
async def serve_service_worker():
    import os
    if os.path.exists("/app/static/sw.js"):
        return FileResponse("/app/static/sw.js", media_type="application/javascript",
                          headers={"Service-Worker-Allowed": "/"})
    raise HTTPException(404, "Service worker not found")


# ── Mobile AI Chat Proxy (JWT-authenticated) ─────────────────────

@router.post("/ai-chat")
async def mobile_ai_chat(
    request: Request,
    claims=Depends(require_desktop_user),
):
    from core.db.base import AsyncSessionLocal
    from core.models.user import User
    from sqlalchemy import select
    from starlette.responses import StreamingResponse

    tenant_id = claims.tenant_id
    user_id = claims.user_id

    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.id == int(user_id), User.is_active == True)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

    if not user:
        err_payload = _json.dumps({"type": "error", "text": "User not found"})
        async def err():
            yield "data: " + err_payload + "\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    request.state.current_user = user
    request.state.tenant_id = tenant_id

    # Override model to Opus for mobile
    request.state.mobile_model_override = "claude-opus-4-8"  # Opus 4.8 (latest); old dated id 404s on this org key

    from modules.dashboard.routes.ai_chat_route import user_ai_chat
    return await user_ai_chat(request)


# ── Mobile nav tabs (registry-driven, JWT-authed) ─────────────────────
# Mirrors /api/v1/nav-tabs resolution, but authenticates via the mobile
# JWT (require_desktop_user) instead of the session cookie — the global
# auth middleware skips /api/v1/mobile. Reads the same layout_tabs
# registry so adding a mobile tab stays one INSERT, not a code change.
# Court Pocket Edition v2 — step 1 (mobile shell + mobile_root tab bar).
_MOBILE_ROLE_RANK = {
    "superadmin": 100, "admin": 90, "partner": 80, "attorney": 70,
    "associate": 60, "paralegal": 50, "staff": 40, "read_only": 20,
    "client": 10, "deal_room_guest": 5,
}


def _mobile_role_ok(user_role, required_role):
    if not required_role:
        return True
    if not user_role:
        return False
    return _MOBILE_ROLE_RANK.get(user_role, 0) >= _MOBILE_ROLE_RANK.get(required_role, 0)


@router.get("/nav-tabs/{layout_slug}")
async def mobile_nav_tabs(layout_slug: str, claims=Depends(require_desktop_user)):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text

    tenant_id = (claims.tenant_id or "").strip()
    user_role = getattr(claims, "role", None)
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("""
            SELECT tab_slug, display_name, display_order, icon, icon_svg,
                   permission_level, required_role, target_type, target_ref,
                   separator_before, badge_source, tenant_id
            FROM layout_tabs
            WHERE layout_slug = :slug AND is_active = TRUE AND is_visible = TRUE
              AND ((tenant_id IS NOT NULL AND trim(tenant_id) = :tid) OR tenant_id IS NULL)
              AND matter_types IS NULL
            ORDER BY display_order ASC
        """), {"slug": layout_slug, "tid": tenant_id})
        rows = [dict(r) for r in result.mappings().fetchall()]

    # Tenant override precedence: a tenant-specific row beats the platform default.
    by_slug = {}
    for row in rows:
        slug = row["tab_slug"]
        if row["tenant_id"] is not None:
            by_slug[slug] = row
        elif slug not in by_slug:
            by_slug[slug] = row
    tabs = sorted(by_slug.values(), key=lambda x: x["display_order"])
    tabs = [t for t in tabs if _mobile_role_ok(user_role, t.get("required_role"))]
    tabs = [t for t in tabs if _mobile_role_ok(user_role, t.get("permission_level"))]

    return {"layout_slug": layout_slug, "tabs": [{
        "tab_slug": t["tab_slug"], "display_name": t["display_name"],
        "display_order": t["display_order"], "icon": t["icon"],
        "icon_svg": t.get("icon_svg") or "", "target_type": t["target_type"],
        "target_ref": t.get("target_ref"),
        "separator_before": t.get("separator_before", False),
        "badge_source": t.get("badge_source"), "badge_count": None,
    } for t in tabs]}


# ── Mobile Tasks (JWT-authed wrappers over the live /api/tasks CRUD) ───
# Court Pocket Edition v2 — step 2. Presentation-only: bind the mobile JWT
# user into request.state, then delegate to the existing task_project_api
# handlers so task logic has ONE source of truth.
async def _mobile_bind_user(request, claims):
    from core.db.base import AsyncSessionLocal
    from core.models.user import User
    from sqlalchemy import select
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(User).where(User.id == int(claims.user_id), User.is_active == True)
        )
        user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(401, "User not found")
    request.state.current_user = user
    request.state.tenant_id = claims.tenant_id
    return user


@router.get("/tasks")
async def mobile_list_tasks(request: Request, scope: str = "open",
                            claims=Depends(require_desktop_user)):
    from modules.dashboard.routes.task_project_api import my_tasks, list_tasks
    user = await _mobile_bind_user(request, claims)
    if scope == "all":
        return await list_tasks(request, assignee_id=int(user.id), limit=200)
    return await my_tasks(request)


@router.post("/tasks")
async def mobile_create_task(request: Request, claims=Depends(require_desktop_user)):
    from modules.dashboard.routes.task_project_api import create_task
    await _mobile_bind_user(request, claims)
    body = await request.json()
    # Default the new task to the creator so it lands in their "my tasks" list,
    # and stamp provenance. Re-inject the body so create_task sees the changes.
    if not body.get("assignee_user_id"):
        body["assignee_user_id"] = int(claims.user_id)
    if not body.get("assigned_to"):
        body["assigned_to"] = [int(claims.user_id)]  # task_assignments drives my_tasks
    if not body.get("source"):
        body["source"] = "mobile"
    request._body = _json.dumps(body).encode()
    return await create_task(request)


@router.put("/tasks/{task_id}")
async def mobile_update_task(task_id: int, request: Request,
                             claims=Depends(require_desktop_user)):
    from modules.dashboard.routes.task_project_api import update_task
    await _mobile_bind_user(request, claims)
    return await update_task(request, task_id)


@router.post("/tasks/{task_id}/complete")
async def mobile_complete_task(task_id: int, request: Request,
                               claims=Depends(require_desktop_user)):
    from modules.dashboard.routes.task_project_api import complete_task
    await _mobile_bind_user(request, claims)
    return await complete_task(request, task_id)


# ── Mobile OnlyOffice config (JWT-authed) — step 5 ────────────────────
# Wraps the cookie-authed oo-config so the mobile PWA can open documents in
# OnlyOffice. Converts an absolute storage_path into the matter-relative path
# the OO resolver expects.
@router.get("/oo-config")
async def mobile_oo_config(request: Request, matter_id: str, path: str = "",
                           mode: str = "edit", claims=Depends(require_desktop_user)):
    import os as _os
    from modules.dms.services.onlyoffice_route import oo_editor_config, _resolve_root
    await _mobile_bind_user(request, claims)
    rel = path
    if path.startswith("/"):
        try:
            root, _ = await _resolve_root((claims.tenant_id or "").strip(), matter_id)
            if root:
                rel = _os.path.relpath(path, root)
        except Exception:
            rel = path
    resp = await oo_editor_config(request, matter_id, path=rel, mode=mode)
    # Re-sign with type='mobile' for the touch-optimized OnlyOffice editor
    # (the OO JWT covers the whole config, so type must be set before signing).
    import json as _j
    from fastapi.responses import JSONResponse as _JR
    from modules.dms.services.onlyoffice_route import _sign_jwt as _sign
    try:
        data = _j.loads(bytes(resp.body))
        cfg = data.get("config") or {}
        cfg["type"] = "mobile"
        cfg.pop("token", None)
        cfg["token"] = _sign(cfg)
        data["config"] = cfg
        return _JR(data)
    except Exception:
        return resp


# ── Mobile Expenses / Reimbursements — step 6 ─────────────────────────
# Both write to the `expenses` table. A reimbursement is tagged via
# external_ref='reimbursement' and is_billable=False (owed back to the
# person, not billed to the client); a plain expense is billable. (Sensible
# default for plan §9-Q3 — distinct type column can be migrated in later.)
class ExpenseCreate(BaseModel):
    matter_id: str
    amount: float
    category: Optional[str] = "Other"
    description: Optional[str] = ""
    vendor: Optional[str] = None
    expense_date: Optional[str] = None
    kind: Optional[str] = "expense"
    is_billable: Optional[bool] = None          # 'expense' | 'reimbursement'
    notes: Optional[str] = None


@router.post("/expenses")
async def mobile_create_expense(entry: ExpenseCreate, request: Request,
                                claims=Depends(require_desktop_user)):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    tenant_id = claims.tenant_id
    if not await _matter_belongs(entry.matter_id, tenant_id):
        raise HTTPException(404, "matter not found")
    exp_date = entry.expense_date or date.today().isoformat()
    is_reimb = (entry.kind or "expense").lower() == "reimbursement"
    eid = str(uuid4())
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            INSERT INTO expenses (id, tenant_id, matter_id, expense_date, category,
                description, vendor, amount, is_billable, source, external_ref,
                notes, entered_by, created_at, updated_at)
            VALUES (CAST(:id AS uuid), CAST(:tid AS char(36)), CAST(:mid AS uuid),
                CAST(:dt AS date), :cat, :descr, :vendor, :amt, :billable, 'mobile',
                :ext, :notes, :uid, NOW(), NOW())
        """), {
            "id": eid, "tid": tenant_id, "mid": entry.matter_id, "dt": date.fromisoformat(exp_date),
            "cat": entry.category or "Other",
            "descr": entry.description or ("Reimbursement" if is_reimb else "Expense"),
            "vendor": entry.vendor, "amt": entry.amount,
            "billable": (False if is_reimb else (True if entry.is_billable is None else bool(entry.is_billable))), "ext": ("reimbursement" if is_reimb else None),
            "notes": entry.notes, "uid": int(claims.user_id),
        })
        await session.commit()
    return {"id": eid, "kind": "reimbursement" if is_reimb else "expense",
            "amount": entry.amount, "expense_date": exp_date}


@router.get("/expenses")
async def mobile_list_expenses(request: Request, claims=Depends(require_desktop_user),
                               limit: int = 50):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    tenant_id = claims.tenant_id
    async with AsyncSessionLocal() as session:
        r = await session.execute(text("""
            SELECT e.id, e.matter_id, e.expense_date, e.category, e.description,
                   e.vendor, e.amount, e.is_billable, e.external_ref, e.source, e.created_at,
                   m.matter_name, c.client_name
            FROM expenses e
            LEFT JOIN matters m ON e.matter_id = m.id AND TRIM(m.tenant_id) = TRIM(:tid)
            LEFT JOIN clients c ON m.client_id = c.id AND TRIM(c.tenant_id) = TRIM(:tid)
            WHERE TRIM(e.tenant_id) = TRIM(:tid) AND e.entered_by = :uid
            ORDER BY e.expense_date DESC, e.created_at DESC LIMIT :limit
        """), {"tid": tenant_id, "uid": int(claims.user_id), "limit": limit})
        rows = r.mappings().all()
    return {"expenses": [{
        "id": str(x["id"]), "matter_id": str(x["matter_id"]) if x["matter_id"] else None,
        "matter_name": x["matter_name"], "client_name": x["client_name"],
        "expense_date": str(x["expense_date"]) if x["expense_date"] else None,
        "category": x["category"], "description": x["description"], "vendor": x["vendor"],
        "amount": float(x["amount"]) if x["amount"] is not None else 0,
        "kind": "reimbursement" if (x["external_ref"] == "reimbursement") else "expense",
        "is_billable": x["is_billable"],
        "created_at": str(x["created_at"]) if x["created_at"] else None,
    } for x in rows]}


# ═══════════════════════════════════════════════════════════════════════
# QUICK CAPTURE · PUSH · REMINDERS · MOBILE FILL-ME-IN — Court Pocket v2
# Field-first: snap now (zero questions), finish later from the alerts.
# ═══════════════════════════════════════════════════════════════════════
import os as _os
import json as _json2
from fastapi import UploadFile, File, Form
from fastapi.responses import JSONResponse, FileResponse

_VAPID_PATH = "/mnt/praesidium/vapid_keys.json"


def _load_vapid():
    with open(_VAPID_PATH) as f:
        d = _json2.load(f)
    return d["private_pem"], d["public_app_server_key"]


# ── Reminder ticker (in-process; fires due reminders as web-push) ──────
_ticker_started = False

def _ensure_reminder_ticker():
    global _ticker_started
    if _ticker_started:
        return
    _ticker_started = True
    import asyncio
    try:
        asyncio.ensure_future(_reminder_loop())
    except Exception:
        _ticker_started = False


async def _reminder_loop():
    import asyncio
    while True:
        try:
            await _fire_due_reminders()
        except Exception as exc:
            logger.warning("reminder loop: %s", exc)
        await asyncio.sleep(60)


async def _send_push_to_user(tid, uid, payload: dict):
    from fastapi.concurrency import run_in_threadpool
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    try:
        from pywebpush import webpush, WebPushException
    except Exception as exc:
        logger.warning("pywebpush unavailable: %s", exc)
        return 0
    priv, _ = _load_vapid()
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            "SELECT id, endpoint, p256dh, auth FROM push_subscriptions "
            "WHERE TRIM(tenant_id)=TRIM(:tid) AND user_id=:uid"),
            {"tid": tid, "uid": uid})).mappings().all()
    sent, dead = 0, []
    for r in rows:
        sub = {"endpoint": r["endpoint"], "keys": {"p256dh": r["p256dh"], "auth": r["auth"]}}
        def _do():
            webpush(sub, _json2.dumps(payload), vapid_private_key=priv,
                    vapid_claims={"sub": "mailto:support@hjmmlegal.com"})
        try:
            await run_in_threadpool(_do)
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                dead.append(r["id"])
        except Exception as e:
            logger.warning("push send error: %s", e)
    if dead:
        async with AsyncSessionLocal() as s:
            await s.execute(text("DELETE FROM push_subscriptions WHERE id = ANY(:ids)"), {"ids": dead})
            await s.commit()
    return sent


async def _fire_due_reminders():
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            "UPDATE mobile_reminders SET status='sending' WHERE id IN ("
            "  SELECT id FROM mobile_reminders WHERE status='scheduled' AND remind_at <= now() "
            "  ORDER BY remind_at LIMIT 50 FOR UPDATE SKIP LOCKED) "
            "RETURNING id, tenant_id, user_id, title, body"))).mappings().all()
        await s.commit()
    for r in rows:
        await _send_push_to_user(r["tenant_id"], r["user_id"], {
            "title": "⏰ " + (r["title"] or "Reminder"),
            "body": r["body"] or "Tap to open Praesidium",
            "url": "/mobile/",
        })
        async with AsyncSessionLocal() as s:
            await s.execute(text("UPDATE mobile_reminders SET status='sent', sent_at=now() WHERE id=:id"), {"id": r["id"]})
            await s.commit()


# ── Push subscription ─────────────────────────────────────────────────
@router.get("/push/vapid-public-key")
async def mobile_vapid_key(claims=Depends(require_desktop_user)):
    _, pub = _load_vapid()
    return {"publicKey": pub}


@router.post("/push/subscribe")
async def mobile_push_subscribe(request: Request, claims=Depends(require_desktop_user)):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    body = await request.json()
    sub = body.get("subscription") or body
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "invalid subscription")
    async with AsyncSessionLocal() as s:
        await s.execute(text("""
            INSERT INTO push_subscriptions (tenant_id, user_id, endpoint, p256dh, auth, ua, created_at)
            VALUES (:tid, :uid, :ep, :p, :a, :ua, now())
            ON CONFLICT (endpoint) DO UPDATE SET p256dh=:p, auth=:a, user_id=:uid, last_used_at=now()
        """), {"tid": claims.tenant_id, "uid": int(claims.user_id), "ep": endpoint,
               "p": keys["p256dh"], "a": keys["auth"],
               "ua": str(request.headers.get("user-agent", ""))[:300]})
        await s.commit()
    return {"ok": True}


@router.post("/push/test")
async def mobile_push_test(claims=Depends(require_desktop_user)):
    n = await _send_push_to_user(claims.tenant_id, int(claims.user_id),
        {"title": "Praesidium", "body": "Push notifications are on ✅", "url": "/mobile/"})
    return {"sent": n}


# ── Quick Capture (snap-and-go; finish later) ─────────────────────────
@router.post("/captures")
async def mobile_create_capture(request: Request, file: UploadFile = File(None),
                                kind: str = Form("receipt"), note: str = Form(None),
                                claims=Depends(require_desktop_user)):
    import shutil
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    tid = claims.tenant_id
    cid = str(uuid4())
    image_path = None
    mime = None
    if file is not None:
        _ALLOWED = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".pdf"}
        _MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".heic": "image/heic", ".heif": "image/heif", ".webp": "image/webp",
                 ".gif": "image/gif", ".pdf": "application/pdf"}
        _M2E = {"image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic",
                "image/heif": ".heif", "image/webp": ".webp", "image/gif": ".gif",
                "application/pdf": ".pdf"}
        ext = (_os.path.splitext(file.filename or "")[1] or "").lower()
        if ext not in _ALLOWED:
            ext = _M2E.get((file.content_type or "").lower(), "")
        if ext not in _ALLOWED:
            raise HTTPException(415, "unsupported file type")
        base = f"/mnt/praesidium/{tid.strip()}/captures"
        _os.makedirs(base, exist_ok=True)
        image_path = _os.path.join(base, cid + ext)
        written, _MAX = 0, 20 * 1024 * 1024
        with open(image_path, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX:
                    out.close()
                    try: _os.remove(image_path)
                    except Exception: pass
                    raise HTTPException(413, "file too large (max 20MB)")
                out.write(chunk)
        mime = _MIME.get(ext, "application/octet-stream")
    async with AsyncSessionLocal() as s:
        await s.execute(text("""
            INSERT INTO mobile_captures (id, tenant_id, user_id, kind, status, image_path, image_mime, note, created_at)
            VALUES (CAST(:id AS uuid), :tid, :uid, :kind, 'pending', :ip, :mime, :note, now())
        """), {"id": cid, "tid": tid, "uid": int(claims.user_id), "kind": kind,
               "ip": image_path, "mime": mime, "note": note})
        await s.commit()
    return {"id": cid, "kind": kind, "status": "pending", "has_image": bool(image_path)}


@router.get("/captures")
async def mobile_list_captures(request: Request, status: str = "pending",
                               claims=Depends(require_desktop_user)):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text("""
            SELECT id, kind, status, image_path, note, amount, category, vendor, created_at, title, pages
            FROM mobile_captures
            WHERE TRIM(tenant_id)=TRIM(:tid) AND user_id=:uid AND status=:st
            ORDER BY created_at DESC LIMIT 100
        """), {"tid": claims.tenant_id, "uid": int(claims.user_id), "st": status})).mappings().all()
    return {"captures": [{
        "id": str(r["id"]), "kind": r["kind"], "status": r["status"],
        "has_image": bool(r["image_path"]),
        "image_url": f"/api/v1/mobile/captures/{r['id']}/image" if r["image_path"] else None,
        "note": r["note"], "amount": float(r["amount"]) if r["amount"] is not None else None,
        "category": r["category"], "vendor": r["vendor"], "title": r["title"],
        "page_count": (1 + (len(r["pages"]) if isinstance(r["pages"], list) else 0)),
        "created_at": str(r["created_at"]) if r["created_at"] else None,
    } for r in rows]}


@router.get("/captures/{capture_id}/image")
async def mobile_capture_image(capture_id: str, claims=Depends(require_desktop_user)):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as s:
        r = (await s.execute(text("""
            SELECT image_path, image_mime FROM mobile_captures
            WHERE id=CAST(:id AS uuid) AND TRIM(tenant_id)=TRIM(:tid) AND user_id=:uid
        """), {"id": capture_id, "tid": claims.tenant_id, "uid": int(claims.user_id)})).mappings().first()
    if not r or not r["image_path"] or not _os.path.isfile(r["image_path"]):
        raise HTTPException(404, "image not found")
    return FileResponse(r["image_path"], media_type=r["image_mime"] or "image/jpeg",
        headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"})


@router.post("/captures/{capture_id}/complete")
async def mobile_complete_capture(capture_id: str, request: Request,
                                  claims=Depends(require_desktop_user)):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    body = await request.json()
    matter_id = body.get("matter_id")
    if not matter_id:
        raise HTTPException(400, "matter_id required")
    if not await _matter_belongs(matter_id, claims.tenant_id):
        raise HTTPException(404, "matter not found")
    kind = (body.get("kind") or "expense").lower()
    is_reimb = kind == "reimbursement"
    billable = False if is_reimb else (True if body.get("is_billable") is None else bool(body.get("is_billable")))
    amount = float(body.get("amount") or 0)
    exp_date = body.get("expense_date") or date.today().isoformat()
    eid = str(uuid4())
    async with AsyncSessionLocal() as s:
        await s.execute(text("""
            INSERT INTO expenses (id, tenant_id, matter_id, expense_date, category, description,
                vendor, amount, is_billable, source, external_ref, notes, entered_by, created_at, updated_at)
            VALUES (CAST(:id AS uuid), CAST(:tid AS char(36)), CAST(:mid AS uuid), CAST(:dt AS date),
                :cat, :descr, :vendor, :amt, :billable, 'mobile', :ext, :notes, :uid, now(), now())
        """), {"id": eid, "tid": claims.tenant_id, "mid": matter_id,
               "dt": date.fromisoformat(exp_date), "cat": body.get("category") or "Other",
               "descr": body.get("description") or ("Reimbursement" if is_reimb else "Expense"),
               "vendor": body.get("vendor"), "amt": amount, "billable": billable,
               "ext": ("reimbursement" if is_reimb else None),
               "notes": f"mobile capture {capture_id}", "uid": int(claims.user_id)})
        await s.execute(text("""
            UPDATE mobile_captures SET status='completed', completed_at=now(),
                matter_id=CAST(:mid AS uuid), amount=:amt, category=:cat, vendor=:vendor,
                description=:descr, promoted_kind='expense', promoted_id=CAST(:eid AS uuid)
            WHERE id=CAST(:cid AS uuid) AND TRIM(tenant_id)=TRIM(:tid) AND user_id=:uid
        """), {"mid": matter_id, "amt": amount, "cat": body.get("category"),
               "vendor": body.get("vendor"), "descr": body.get("description"),
               "eid": eid, "cid": capture_id, "tid": claims.tenant_id, "uid": int(claims.user_id)})
        await s.commit()
    return {"id": eid, "kind": "reimbursement" if is_reimb else "expense", "amount": amount}


@router.delete("/captures/{capture_id}")
async def mobile_dismiss_capture(capture_id: str, claims=Depends(require_desktop_user)):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as s:
        await s.execute(text("""
            UPDATE mobile_captures SET status='dismissed'
            WHERE id=CAST(:id AS uuid) AND TRIM(tenant_id)=TRIM(:tid) AND user_id=:uid
        """), {"id": capture_id, "tid": claims.tenant_id, "uid": int(claims.user_id)})
        await s.commit()
    return {"ok": True}


# ── Quick Reminder (timed task + web-push at remind_at) ────────────────
@router.post("/reminders")
async def mobile_create_reminder(request: Request, claims=Depends(require_desktop_user)):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    from modules.dashboard.routes.task_project_api import create_task
    body = await request.json()
    title = (body.get("title") or "").strip()
    remind_at = body.get("remind_at")
    if not title or not remind_at:
        raise HTTPException(400, "title and remind_at required")
    if body.get("matter_id") and not await _matter_belongs(body.get("matter_id"), claims.tenant_id):
        raise HTTPException(404, "matter not found")
    # Also create a task so it lives in the Tasks list.
    await _mobile_bind_user(request, claims)
    task_payload = {"title": title, "priority": body.get("priority", "medium"),
                    "due_date": remind_at, "assigned_to": [int(claims.user_id)],
                    "source": "mobile_reminder"}
    if body.get("matter_id"):
        task_payload["matter_id"] = body["matter_id"]
    request._body = _json2.dumps(task_payload).encode()
    task_resp = await create_task(request)
    task_id = None
    try:
        task_id = task_resp.get("id") if isinstance(task_resp, dict) else None
    except Exception:
        task_id = None
    from datetime import datetime as _dt
    ra_obj = _dt.fromisoformat(remind_at.replace("Z", "+00:00"))
    rid = str(uuid4())
    async with AsyncSessionLocal() as s:
        await s.execute(text("""
            INSERT INTO mobile_reminders (id, tenant_id, user_id, title, body, remind_at, matter_id, task_id, status, created_at)
            VALUES (CAST(:id AS uuid), :tid, :uid, :title, :body, :ra,
                    CASE WHEN :mid='' THEN NULL ELSE CAST(:mid AS uuid) END, :task_id, 'scheduled', now())
        """), {"id": rid, "tid": claims.tenant_id, "uid": int(claims.user_id),
               "title": title, "body": body.get("body"), "ra": ra_obj,
               "mid": body.get("matter_id") or "", "task_id": task_id})
        await s.commit()
    return {"id": rid, "title": title, "remind_at": remind_at, "task_id": task_id}


# ── Mobile Fill-Me-In feed (fast; includes capture alerts) ────────────
@router.get("/fill-me-in")
async def mobile_fill_me_in(request: Request, claims=Depends(require_desktop_user)):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    _ensure_reminder_ticker()
    tid, uid = claims.tenant_id, int(claims.user_id)
    async with AsyncSessionLocal() as s:
        pend = (await s.execute(text("""
            SELECT id, kind, created_at FROM mobile_captures
            WHERE TRIM(tenant_id)=TRIM(:tid) AND user_id=:uid AND status='pending'
            ORDER BY created_at DESC LIMIT 20
        """), {"tid": tid, "uid": uid})).mappings().all()
        tasks = (await s.execute(text("""
            SELECT COUNT(*) FILTER (WHERE t.due_date < now()) AS overdue,
                   COUNT(*) FILTER (WHERE t.due_date::date = now()::date) AS today,
                   COUNT(*) AS open_total
            FROM tasks t JOIN task_assignments ta ON t.id=ta.task_id AND TRIM(t.tenant_id)=TRIM(ta.tenant_id)
            WHERE TRIM(t.tenant_id)=TRIM(:tid) AND ta.user_id=:uid
              AND t.status NOT IN ('complete','cancelled','deleted')
        """), {"tid": tid, "uid": uid})).mappings().first()
        rem = (await s.execute(text("""
            SELECT title, remind_at FROM mobile_reminders
            WHERE TRIM(tenant_id)=TRIM(:tid) AND user_id=:uid AND status='scheduled'
              AND remind_at >= now() ORDER BY remind_at LIMIT 5
        """), {"tid": tid, "uid": uid})).mappings().all()
    return {
        "captures_pending": {"count": len(pend), "items": [
            {"id": str(r["id"]), "kind": r["kind"],
             "created_at": str(r["created_at"]) if r["created_at"] else None} for r in pend]},
        "tasks": {"overdue": tasks["overdue"] if tasks else 0,
                  "today": tasks["today"] if tasks else 0,
                  "open_total": tasks["open_total"] if tasks else 0},
        "reminders": [{"title": r["title"], "remind_at": str(r["remind_at"])} for r in rem],
    }


# ── Review-fix helpers (tenant guard + rate resolution) ───────────────
async def _matter_belongs(matter_id, tid):
    if not matter_id:
        return True
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    try:
        async with AsyncSessionLocal() as s:
            r = await s.execute(text(
                "SELECT 1 FROM matters WHERE id=CAST(:mid AS uuid) AND TRIM(tenant_id)=TRIM(:tid)"),
                {"mid": str(matter_id), "tid": tid})
            return r.first() is not None
    except Exception:
        return False


async def _resolve_rate_amount(tid, uid, hours):
    """Bill at the timekeeper's default rate; leave NULL for desktop re-rating if unknown."""
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    rate = None
    try:
        async with AsyncSessionLocal() as s:
            r = await s.execute(text(
                "SELECT default_hourly_rate FROM users WHERE id=:uid AND TRIM(tenant_id)=TRIM(:tid)"),
                {"uid": int(uid), "tid": tid})
            row = r.first()
            if row and row[0] is not None:
                rate = float(row[0])
    except Exception:
        rate = None
    amount = round(float(hours) * rate, 2) if rate is not None else None
    return rate, amount


# ── PWA manifest (step 8 polish) ──────────────────────────────────────
@pwa_router.get("/manifest.json")
async def serve_manifest():
    from fastapi.responses import JSONResponse
    return JSONResponse({
        "name": "Praesidium",
        "short_name": "Praesidium",
        "description": "Praesidium Legal Intelligence — Mobile",
        "start_url": "/mobile/",
        "scope": "/mobile/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#F5F5F7",
        "theme_color": "#0D1F3C",
        "icons": [
            {"src": "/static/img/praesidium-icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/img/praesidium-icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/img/praesidium-icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }, media_type="application/manifest+json")


# ── Mobile Fill-Me-In AI briefing (JWT-authed; delegates to desktop summary) ──
@router.post("/fill-me-in/summary")
async def mobile_fill_me_in_summary(request: Request, claims=Depends(require_desktop_user)):
    from modules.dashboard.routes.fill_me_in_api import fill_me_in_summary
    await _mobile_bind_user(request, claims)
    return await fill_me_in_summary(request)


# ═══════════════════════════════════════════════════════════════════════
# SCAN v2 — never gate on metadata: file immediately (drafts if unclassified),
# remind to classify later, description optional.
# ═══════════════════════════════════════════════════════════════════════
def _build_scan_artifact(dest_dir, kind, page_paths, title):
    """Build the filed artifact in dest_dir. Document pages -> one PDF; photo -> image."""
    import os as _o, shutil
    _o.makedirs(dest_dir, exist_ok=True)
    paths = [p for p in (page_paths or []) if p and _o.path.isfile(p)]
    if not paths:
        return None
    base = (title or "").strip() or ("Scan " + (kind or "photo"))

    def _uniq(name):
        b, e = _o.path.splitext(name); cand = name; i = 1
        while _o.path.exists(_o.path.join(dest_dir, cand)):
            cand = f"{b} ({i}){e}"; i += 1
        return cand

    if kind == "document":
        try:
            from PIL import Image
            imgs = [Image.open(p).convert("RGB") for p in paths]
            out = _o.path.join(dest_dir, _uniq(base if base.lower().endswith(".pdf") else base + ".pdf"))
            imgs[0].save(out, save_all=True, append_images=imgs[1:])
            return out
        except Exception as exc:
            logger.warning("scan PDF build failed, copying image: %s", exc)
    ext = _o.path.splitext(paths[0])[1] or ".jpg"
    out = _o.path.join(dest_dir, _uniq(f"{base}{ext}"))
    shutil.copy2(paths[0], out)
    return out


async def _matter_scans_dir(tid, matter_id):
    import os as _o
    from modules.dms.services.onlyoffice_route import _resolve_root
    try:
        root, _ = await _resolve_root((tid or "").strip(), matter_id)
    except Exception:
        root = None
    return _o.path.join(root, "00-Mobile Scans") if root else None


async def _file_capture_to_matter(tid, matter_id, kind, page_paths, title):
    dest = await _matter_scans_dir(tid, matter_id)
    if not dest:
        return None
    return _build_scan_artifact(dest, kind, page_paths, title)


async def _create_classify_reminder(tid, uid, cid, kind, title):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    from datetime import datetime, timedelta, timezone
    label = (title or "").strip() or (kind or "scan")
    ra = datetime.now(timezone.utc) + timedelta(hours=3)
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(text("""
                INSERT INTO mobile_reminders (id, tenant_id, user_id, title, body, remind_at, capture_id, status, created_at)
                VALUES (CAST(:id AS uuid), :tid, :uid, :title, :body, :ra, CAST(:cid AS uuid), 'scheduled', now())
            """), {"id": str(uuid4()), "tid": tid, "uid": uid, "title": f"Classify scan: {label}",
                   "body": "Tap to assign it to a matter.", "ra": ra, "cid": cid})
            await s.commit()
        return True
    except Exception as exc:
        logger.warning("classify reminder create failed: %s", exc)
        return False


@router.post("/captures/scan")
async def mobile_capture_scan(request: Request, files: list[UploadFile] = File(default=[]),
                              kind: str = Form("document"), matter_id: str = Form(None),
                              title: str = Form(None), description: str = Form(None),
                              claims=Depends(require_desktop_user)):
    import os as _o2
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    tid = claims.tenant_id
    if not files:
        raise HTTPException(400, "no files")
    _ALLOWED = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".pdf"}
    base_dir = f"/mnt/praesidium/{tid.strip()}/captures"
    _o2.makedirs(base_dir, exist_ok=True)
    cid = str(uuid4())
    saved = []
    for idx, f in enumerate(files):
        ext = (_o2.path.splitext(f.filename or "")[1] or "").lower()
        if ext not in _ALLOWED:
            ext = ".jpg"
        fp = _o2.path.join(base_dir, f"{cid}_{idx}{ext}")
        written, _MAX = 0, 25 * 1024 * 1024
        with open(fp, "wb") as out:
            while True:
                chunk = f.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX:
                    out.close()
                    try: _o2.remove(fp)
                    except Exception: pass
                    raise HTTPException(413, "file too large (max 25MB/page)")
                out.write(chunk)
        saved.append(fp)

    mid = (matter_id or "").strip() or None
    if mid and not await _matter_belongs(mid, tid):
        raise HTTPException(404, "matter not found")

    artifact, status = None, "draft"
    if mid:
        dest = await _matter_scans_dir(tid, mid)
        if dest:
            artifact = _build_scan_artifact(dest, kind, saved, title)
            if artifact:
                status = "filed"
    if status != "filed":
        # Unclassified (or filing failed): file into DMS drafts immediately — never gate.
        artifact = _build_scan_artifact(f"/mnt/praesidium/{tid.strip()}/00-Mobile Drafts", kind, saved, title) or saved[0]
        status = "draft"
        mid = None

    async with AsyncSessionLocal() as s:
        await s.execute(text("""
            INSERT INTO mobile_captures (id, tenant_id, user_id, kind, status, image_path,
                pages, matter_id, title, description, artifact_path, created_at, completed_at)
            VALUES (CAST(:id AS uuid), :tid, :uid, :kind, CAST(:st AS varchar), :ip, CAST(:pages AS jsonb),
                CAST(:mid AS uuid), :title, :descr, :art, now(),
                CASE WHEN CAST(:st AS varchar)='filed' THEN now() ELSE NULL END)
        """), {"id": cid, "tid": tid, "uid": int(claims.user_id), "kind": kind, "st": status,
               "ip": saved[0], "pages": _json2.dumps(saved[1:]), "mid": mid, "title": title,
               "descr": description, "art": artifact})
        await s.commit()

    reminded = False
    if status == "draft":
        reminded = await _create_classify_reminder(tid, int(claims.user_id), cid, kind, title)
    return {"id": cid, "kind": kind, "status": status, "pages": len(saved),
            "filed": status == "filed", "reminder": reminded}


@router.post("/captures/{capture_id}/classify")
async def mobile_classify_capture(capture_id: str, request: Request, claims=Depends(require_desktop_user)):
    import os as _o3, shutil
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    body = await request.json()
    matter_id = (body.get("matter_id") or "").strip()
    if not matter_id:
        raise HTTPException(400, "matter_id required")
    if not await _matter_belongs(matter_id, claims.tenant_id):
        raise HTTPException(404, "matter not found")
    descr = body.get("description")
    async with AsyncSessionLocal() as s:
        r = (await s.execute(text("""
            SELECT kind, image_path, pages, title, artifact_path FROM mobile_captures
            WHERE id=CAST(:id AS uuid) AND TRIM(tenant_id)=TRIM(:tid) AND user_id=:uid
              AND status IN ('pending', 'draft')
        """), {"id": capture_id, "tid": claims.tenant_id, "uid": int(claims.user_id)})).mappings().first()
    if not r:
        raise HTTPException(404, "capture not found")
    dest = await _matter_scans_dir(claims.tenant_id, matter_id)
    filed = None
    if dest:
        _o3.makedirs(dest, exist_ok=True)
        art = r["artifact_path"]
        if art and _o3.path.isfile(art):
            b, e = _o3.path.splitext(_o3.path.basename(art)); cand = b + e; i = 1
            while _o3.path.exists(_o3.path.join(dest, cand)):
                cand = f"{b} ({i}){e}"; i += 1
            try:
                shutil.move(art, _o3.path.join(dest, cand)); filed = _o3.path.join(dest, cand)
            except Exception:
                filed = None
        if not filed:
            pages = r["pages"] or []
            if isinstance(pages, str):
                try: pages = _json2.loads(pages)
                except Exception: pages = []
            kind = r["kind"] if r["kind"] in ("document", "photo") else "photo"
            filed = _build_scan_artifact(dest, kind, [r["image_path"]] + list(pages), r["title"])
    async with AsyncSessionLocal() as s:
        await s.execute(text("""
            UPDATE mobile_captures SET status='filed', matter_id=CAST(:mid AS uuid),
                artifact_path=:art, description=COALESCE(:descr, description), completed_at=now()
            WHERE id=CAST(:id AS uuid) AND TRIM(tenant_id)=TRIM(:tid)
        """), {"mid": matter_id, "art": filed, "descr": descr, "id": capture_id, "tid": claims.tenant_id})
        await s.execute(text("""
            UPDATE mobile_reminders SET status='done'
            WHERE capture_id=CAST(:id AS uuid) AND TRIM(tenant_id)=TRIM(:tid) AND status='scheduled'
        """), {"id": capture_id, "tid": claims.tenant_id})
        await s.commit()
    return {"id": capture_id, "status": "filed", "filed": bool(filed)}
