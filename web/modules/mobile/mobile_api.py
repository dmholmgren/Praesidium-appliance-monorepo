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
    from core.db.session import AsyncSessionLocal
    from sqlalchemy import text

    tenant_id = claims.tenant_id
    user_id = claims.user_id
    entry_id = str(uuid4())
    work_date = entry.work_date or date.today().isoformat()
    now = datetime.utcnow().isoformat()
    rate = 550.0
    amount = round(entry.hours * rate, 2)

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO time_entries (
                    id, tenant_id, matter_id, user_id,
                    narrative, hours, rate, amount,
                    work_date, status, source, created_at, updated_at
                ) VALUES (
                    CAST(:id AS uuid), CAST(:tenant_id AS char(36)),
                    CAST(:matter_id AS uuid), CAST(:user_id AS uuid),
                    :narrative, :hours, :rate, :amount,
                    CAST(:work_date AS date), :status, 'mobile', NOW(), NOW()
                )
            """),
            {
                "id": entry_id, "tenant_id": tenant_id,
                "matter_id": entry.matter_id, "user_id": user_id,
                "narrative": entry.narrative, "hours": entry.hours,
                "rate": rate, "amount": amount,
                "work_date": work_date, "status": entry.status or "draft",
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
    from core.db.session import AsyncSessionLocal
    from sqlalchemy import text

    tenant_id = claims.tenant_id
    user_id = claims.user_id
    query = """
        SELECT te.id, te.matter_id, te.narrative, te.hours, te.rate, te.amount,
               te.work_date, te.status, te.source, te.created_at,
               m.name AS matter_name, c.name AS client_name
        FROM time_entries te
        LEFT JOIN matters m ON te.matter_id = m.id AND TRIM(m.tenant_id) = TRIM(:tenant_id)
        LEFT JOIN clients c ON m.client_id = c.id AND TRIM(c.tenant_id) = TRIM(:tenant_id)
        WHERE TRIM(te.tenant_id) = TRIM(:tenant_id)
          AND te.user_id = CAST(:user_id AS uuid)
    """
    params = {"tenant_id": tenant_id, "user_id": user_id, "limit": limit}
    if matter_id:
        query += " AND te.matter_id = CAST(:matter_id AS uuid)"
        params["matter_id"] = matter_id
    if status:
        query += " AND te.status = :status"
        params["status"] = status
    query += " ORDER BY te.work_date DESC, te.created_at DESC LIMIT :limit"

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
    from core.db.session import AsyncSessionLocal
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
    request.state.mobile_model_override = "claude-opus-4-20250514"

    from modules.dashboard.routes.ai_chat_route import user_ai_chat
    return await user_ai_chat(request)
