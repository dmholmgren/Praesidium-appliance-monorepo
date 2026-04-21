"""
modules/billing/api/views.py
Billing module HTML page routes.
All routes registered via register_billing_module() in __init__.py.
"""
import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from modules.dashboard.services.auth_helper import get_current_user
from modules.dashboard.services.nav_context import get_nav_context

logger  = logging.getLogger(__name__)
views   = APIRouter(tags=["billing-views"])
templates = Jinja2Templates(directory="modules/billing/templates")


def _ctx(request, nav, **kwargs):
    return {"request": request, "nav": nav, "branding": getattr(request.state, "branding", None), **kwargs}


@views.get("/billing/matters", response_class=HTMLResponse)
async def matters_page(request: Request, user=Depends(get_current_user)):
    nav = await get_nav_context(request, page="billing_matters")
    return templates.TemplateResponse(request, "billing/matters.html", _ctx(request, nav))


@views.get("/billing/matters/{matter_id}", response_class=HTMLResponse)
async def matter_detail_page(request: Request, matter_id: str, user=Depends(get_current_user)):
    nav = await get_nav_context(request, page="billing_matters")
    return templates.TemplateResponse(request, "billing/matter_detail.html",
                                      _ctx(request, nav, matter_id=matter_id))


@views.get("/billing/slips/new", response_class=HTMLResponse)
async def slip_entry_page(request: Request, matter_id: str = "", user=Depends(get_current_user)):
    nav = await get_nav_context(request, page="billing_slips")
    return templates.TemplateResponse(request, "billing/slip_entry.html",
                                      _ctx(request, nav, prefill_matter=matter_id))


@views.get("/billing/prebills", response_class=HTMLResponse)
async def prebills_page(request: Request, user=Depends(get_current_user)):
    nav = await get_nav_context(request, page="billing_prebills")
    return templates.TemplateResponse(request, "billing/prebills.html", _ctx(request, nav))


@views.get("/billing/prebills/{matter_id}", response_class=HTMLResponse)
async def prebill_detail_page(request: Request, matter_id: str, user=Depends(get_current_user)):
    nav = await get_nav_context(request, page="billing_prebills")
    return templates.TemplateResponse(request, "billing/prebill_detail.html",
                                      _ctx(request, nav, matter_id=matter_id))


@views.get("/billing/invoices", response_class=HTMLResponse)
async def invoices_page(request: Request, user=Depends(get_current_user)):
    nav = await get_nav_context(request, page="billing_invoices")
    return templates.TemplateResponse(request, "billing/invoices.html", _ctx(request, nav))


@views.get("/billing/invoices/{invoice_id}", response_class=HTMLResponse)
async def invoice_detail_page(request: Request, invoice_id: str, user=Depends(get_current_user)):
    nav = await get_nav_context(request, page="billing_invoices")
    return templates.TemplateResponse(request, "billing/invoice_detail.html",
                                      _ctx(request, nav, invoice_id=invoice_id))


@views.get("/billing/payments", response_class=HTMLResponse)
async def payments_page(request: Request, user=Depends(get_current_user)):
    nav = await get_nav_context(request, page="billing_payments")
    return templates.TemplateResponse(request, "billing/payments.html", _ctx(request, nav))


@views.get("/billing/payments/new", response_class=HTMLResponse)
async def payment_entry_page(request: Request, user=Depends(get_current_user)):
    nav = await get_nav_context(request, page="billing_payments")
    return templates.TemplateResponse(request, "billing/payment_entry.html", _ctx(request, nav))


@views.get("/billing/payments/{payment_id}/allocate", response_class=HTMLResponse)
async def payment_allocate_page(request: Request, payment_id: str, user=Depends(get_current_user)):
    nav = await get_nav_context(request, page="billing_payments")
    return templates.TemplateResponse(request, "billing/payment_allocate.html",
                                      _ctx(request, nav, payment_id=payment_id))
