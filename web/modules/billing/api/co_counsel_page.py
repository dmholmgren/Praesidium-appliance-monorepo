"""
Co-counsel admin page route (2026-06-14) — matches client_portal_page.py pattern.
Mounted from the matter workspace; renders the co-counsel management React app.
"""
import logging
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.billing.brand_helper import get_brand
from core.services.nav_context import get_nav_context

logger = logging.getLogger(__name__)
router = APIRouter(tags=["co-counsel-views"])
templates = Jinja2Templates(directory=[
    "core/templates",
    "modules/billing/templates",
])


def _ctx(request, **kwargs):
    return {"request": request, "brand": get_brand(request), "page": "matters", **kwargs}


@router.get("/matters/{matter_id}/co-counsel", response_class=HTMLResponse)
async def co_counsel_admin_page(request: Request, matter_id: str):
    tid = (getattr(request.state, "tenant_id", "") or "").strip()
    user = getattr(request.state, "current_user", None)
    nav_ctx = await get_nav_context(request)

    matter_name = "Matter"
    matter_number = ""
    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text("""
            SELECT matter_name, matter_number FROM matters
            WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"mid": matter_id, "tid": tid})
        row = r.fetchone()
        if row:
            matter_name = row.matter_name
            matter_number = row.matter_number or ""

    return templates.TemplateResponse(
        request,
        "billing/co_counsel_admin.html",
        {**_ctx(request, matter_id=matter_id, matter_name=matter_name,
                matter_number=matter_number, user=user, current_user=user), **nav_ctx},
    )
