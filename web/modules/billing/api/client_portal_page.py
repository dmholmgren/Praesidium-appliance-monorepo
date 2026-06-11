"""
Client portal admin page route — v3 (matching views.py template pattern).
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
router = APIRouter(tags=["billing-views"])
templates = Jinja2Templates(directory=[
    "core/templates",
    "modules/billing/templates",
])


def _ctx(request, **kwargs):
    return {"request": request, "brand": get_brand(request), "page": "billing", **kwargs}


@router.get("/billing/clients/{client_id}/portal", response_class=HTMLResponse)
async def client_portal_admin_page(request: Request, client_id: str):
    tid = (getattr(request.state, "tenant_id", "") or "").strip()
    user = getattr(request.state, "current_user", None)
    nav_ctx = await get_nav_context(request)

    client_name = "Client"
    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text("""
            SELECT client_name FROM clients
            WHERE id = CAST(:cid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"cid": client_id, "tid": tid})
        row = r.fetchone()
        if row:
            client_name = row.client_name

    return templates.TemplateResponse(
        request,
        "billing/client_portal_admin.html",
        {**_ctx(request, client_id=client_id, client_name=client_name,
                user=user, current_user=user, page="clients"), **nav_ctx},
    )
