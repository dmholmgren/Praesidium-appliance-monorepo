"""
modules/reconciliation/views.py
Reconciliation module — serves React shell templates.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from core.services.nav_context import get_nav_context
from modules.billing.brand_helper import get_brand

views = APIRouter(tags=["reconciliation-views"])
templates = Jinja2Templates(directory=["core/templates", "modules/reconciliation/templates"])


def _ctx(request, **kw):
    return {"request": request, "brand": get_brand(request), "page": "reconciliation", **kw}


@views.get("/reconciliation", response_class=HTMLResponse)
@views.get("/reconciliation/", response_class=HTMLResponse)
async def recon_home(request: Request):
    user = getattr(request.state, "current_user", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(
        request,
        "reconciliation/recon_home_react.html",
        {**_ctx(request, user=user, current_user=user), **nav_ctx},
    )


@views.get("/reconciliation/timesheet", response_class=HTMLResponse)
async def recon_timesheet_dashboard(request: Request):
    user = getattr(request.state, "current_user", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(
        request,
        "reconciliation/recon_timesheet_react.html",
        {**_ctx(request, user=user, current_user=user), **nav_ctx},
    )




@views.get("/reconciliation/timesheet/day/{day}", response_class=HTMLResponse)
async def recon_timesheet_day(request: Request, day: str):
    user = getattr(request.state, "current_user", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(
        request,
        "reconciliation/recon_timesheet_day_react.html",
        {**_ctx(request, user=user, current_user=user, day=day), **nav_ctx},
    )


@views.get("/reconciliation/timesheet/session/{session_id}", response_class=HTMLResponse)
async def recon_timesheet_session(request: Request, session_id: str):
    user = getattr(request.state, "current_user", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(
        request,
        "reconciliation/recon_timesheet_review_react.html",
        {**_ctx(request, user=user, current_user=user, session_id=session_id), **nav_ctx},
    )


@views.get("/reconciliation/email", response_class=HTMLResponse)
async def recon_email_dashboard(request: Request):
    user = getattr(request.state, "current_user", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(
        request,
        "reconciliation/recon_email_react.html",
        {**_ctx(request, user=user, current_user=user), **nav_ctx},
    )


@views.get("/reconciliation/tasks", response_class=HTMLResponse)
async def recon_tasks_dashboard(request: Request):
    user = getattr(request.state, "current_user", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(
        request,
        "reconciliation/recon_tasks_react.html",
        {**_ctx(request, user=user, current_user=user), **nav_ctx},
    )
