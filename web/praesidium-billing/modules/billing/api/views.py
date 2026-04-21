"""HTMX view routes — serves billing templates with branding context."""
from datetime import date, timedelta
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from core.db.base import TenantSession
from core.auth.middleware import get_current_user
from modules.billing.brand_helper import get_brand
from modules.billing.services.client_service import ClientService
from modules.billing.services.time_entry_service import TimeEntryService

views = APIRouter(tags=["billing-views"])
templates = Jinja2Templates(directory="modules/billing/templates")

def _ctx(request, **kwargs):
    return {"request": request, "brand": get_brand(request), **kwargs}

@views.get("/billing/clients", response_class=HTMLResponse)
async def clients_page(request: Request):
    db = request.state.db
    result = await ClientService(db).list_clients()
    return templates.TemplateResponse("billing/clients.html", _ctx(request, clients=result["items"]))

@views.get("/billing/timesheet", response_class=HTMLResponse)
async def timesheet_page(request: Request, user=Depends(get_current_user)):
    db = request.state.db
    svc = TimeEntryService(db)
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    entries = await svc.list_entries(user_id=user.id, date_from=week_start, date_to=today, limit=200)
    summary = await svc.get_timesheet_summary(user.id, week_start, today)
    return templates.TemplateResponse("billing/timesheet.html", _ctx(request, entries=entries["items"],
        date_from=str(week_start), date_to=str(today), today_hours=0,
        week_hours=summary["period_total_hours"], week_amount=f"{summary['period_total_amount']:.2f}"))

@views.get("/billing/trust", response_class=HTMLResponse)
async def trust_page(request: Request):
    db = request.state.db
    from modules.billing.models.trust import TrustLedger
    from modules.billing.models.client import Client
    ledgers = db.query(TrustLedger).all()
    data = []
    total = 0
    for l in ledgers:
        client = db.query(Client).filter(Client.id == l.client_id).first()
        data.append({"client_id": l.client_id, "client_name": client.client_name if client else "Unknown", "balance": float(l.balance), "last_reconciled_at": str(l.last_reconciled_at) if l.last_reconciled_at else None})
        total += float(l.balance)
    clients = db.query(Client).filter(Client.is_active == True).all()
    return templates.TemplateResponse("billing/trust.html", _ctx(request, ledgers=data, total_trust_balance=total, clients=clients))

@views.get("/billing/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    db = request.state.db
    from modules.billing.models.client import Client
    clients = db.query(Client).filter(Client.is_active == True).all()
    return templates.TemplateResponse("billing/reports.html", _ctx(request, clients=clients, attorneys=[], active_category=None))
