"""Billing API — all endpoints, BigInteger IDs, Chat 0 column names."""
import json, os
from datetime import date
from decimal import Decimal
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Request, Body
from fastapi.responses import Response
from pydantic import BaseModel
from core.db.base import TenantSession

from core.services.branding import BrandingService
from core.services.storage import StorageService
from core.services.email import EmailService
from modules.billing.services.client_service import ClientService
from modules.billing.services.matter_service import MatterService
from modules.billing.services.rate_service import RateService
from modules.billing.services.time_entry_service import TimeEntryService
from modules.billing.services.invoice_service import InvoiceService
from modules.billing.services.payment_service import PaymentService
from modules.billing.services.trust_service import TrustService
from modules.billing.services.report_service import ReportService

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])

def get_db(request: Request):
    from core.db.base import get_session_factory, TenantSession as TS
    return TS(get_session_factory()(), request.state.tenant_id)
def get_branding(request: Request) -> BrandingService:
    return request.state.branding

class ClientCreate(BaseModel):
    client_name: str
    client_number: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address1: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None

class MatterCreate(BaseModel):
    client_id: int
    matter_name: str
    matter_type: str = "general"
    billing_type: str = "hourly"
    matter_number: Optional[str] = None

class TimeEntryCreate(BaseModel):
    matter_id: int
    description: str
    hours: float
    entry_date: Optional[str] = None
    utbms_code: Optional[str] = None
    rate: Optional[float] = None

class InvoiceGenerate(BaseModel):
    client_id: int
    matter_ids: List[int]
    period_start: str
    period_end: str

class PaymentRecord(BaseModel):
    invoice_id: int
    amount: float
    method: str
    reference_number: Optional[str] = None
    payment_date: Optional[str] = None

class TrustDeposit(BaseModel):
    client_id: int
    amount: float
    description: str
    matter_id: Optional[int] = None
    reference_number: Optional[str] = None

class RateSet(BaseModel):
    scope: str
    hourly_rate: float
    user_id: Optional[int] = None
    client_id: Optional[int] = None
    matter_id: Optional[int] = None
    effective_date: Optional[str] = None
    reason: Optional[str] = None

# Clients
@router.get("/clients")
async def list_clients(search: Optional[str] = None, db: TenantSession = Depends(get_db)):
    return await ClientService(db).list_clients(search=search)

@router.get("/clients/{client_id}")
async def get_client(client_id: int, db: TenantSession = Depends(get_db)):
    c = await ClientService(db).get_client(client_id)
    if not c: raise HTTPException(404, "Client not found")
    return c

@router.post("/clients")
async def create_client(data: ClientCreate, db: TenantSession = Depends(get_db), user=Depends(lambda r: r.state.current_user)):
    return await ClientService(db).create_client(data.model_dump(exclude_none=True), user.id)

# Matters
@router.get("/matters")
async def list_matters(client_id: Optional[int] = None, status: Optional[str] = None, search: Optional[str] = None, db: TenantSession = Depends(get_db)):
    return await MatterService(db).list_matters(client_id=client_id, status=status, search=search)

@router.post("/matters")
async def create_matter(data: MatterCreate, db: TenantSession = Depends(get_db), user=Depends(lambda r: r.state.current_user)):
    return await MatterService(db).create_matter(data.model_dump(exclude_none=True), user.id)

@router.post("/matters/quick-create")
async def quick_create(client_id: int = Body(...), name: str = Body(...), db: TenantSession = Depends(get_db), user=Depends(lambda r: r.state.current_user)):
    return await MatterService(db).quick_create_matter(client_id, name, user.id)

# Rates
@router.post("/rates")
async def set_rate(data: RateSet, db: TenantSession = Depends(get_db), user=Depends(lambda r: r.state.current_user)):
    return await RateService(db).set_rate(scope=data.scope, hourly_rate=Decimal(str(data.hourly_rate)), changed_by_id=user.id, user_id=data.user_id, client_id=data.client_id, matter_id=data.matter_id, effective_date=date.fromisoformat(data.effective_date) if data.effective_date else None, reason=data.reason)

@router.get("/rates/resolve")
async def resolve_rate(user_id: int, client_id: Optional[int] = None, matter_id: Optional[int] = None, db: TenantSession = Depends(get_db)):
    rate = await RateService(db).resolve_rate(user_id, client_id, matter_id)
    return {"rate": float(rate) if rate else None}

# Time entries
@router.get("/time-entries")
async def list_entries(user_id: Optional[int] = None, matter_id: Optional[int] = None, status: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, db: TenantSession = Depends(get_db)):
    return await TimeEntryService(db).list_entries(user_id=user_id, matter_id=matter_id, status=status, date_from=date.fromisoformat(date_from) if date_from else None, date_to=date.fromisoformat(date_to) if date_to else None)

@router.post("/time-entries")
async def create_entry(data: TimeEntryCreate, db: TenantSession = Depends(get_db), user=Depends(lambda r: r.state.current_user)):
    d = data.model_dump(exclude_none=True)
    if data.entry_date: d["entry_date"] = date.fromisoformat(data.entry_date)
    return await TimeEntryService(db).create_entry(d, user.id)

@router.post("/time-entries/bulk-approve")
async def bulk_approve(entry_ids: List[int] = Body(...), db: TenantSession = Depends(get_db), user=Depends(lambda r: r.state.current_user)):
    return {"approved_count": await TimeEntryService(db).bulk_approve(entry_ids, user.id)}

@router.post("/time-entries/{entry_id}/reject")
async def reject_entry(entry_id: int, reason: str = Body("", embed=True), db: TenantSession = Depends(get_db), user=Depends(lambda r: r.state.current_user)):
    e = await TimeEntryService(db).reject_entry(entry_id, user.id, reason)
    if not e: raise HTTPException(404)
    return e

@router.get("/timesheet-summary")
async def timesheet_summary(user_id: int, date_from: str, date_to: str, db: TenantSession = Depends(get_db)):
    return await TimeEntryService(db).get_timesheet_summary(user_id, date.fromisoformat(date_from), date.fromisoformat(date_to))

# QC
@router.post("/qc/run")
async def trigger_qc(entry_ids: Optional[List[int]] = Body(None), db: TenantSession = Depends(get_db), user=Depends(lambda r: r.state.current_user)):
    from rq import Queue; from redis import Redis
    q = Queue("billing_qc", connection=Redis.from_url(os.environ.get("REDIS_URL","redis://localhost:6379")))
    job = q.enqueue("modules.billing.jobs.run_billing_qc", db.tenant_id, entry_ids, user.id)
    return {"job_id": job.id, "status": "enqueued"}

# Reconciliation
@router.post("/reconciliation/run")
async def trigger_recon(date_from: str = Body(...), date_to: str = Body(...), db: TenantSession = Depends(get_db), user=Depends(lambda r: r.state.current_user)):
    from rq import Queue; from redis import Redis
    q = Queue("reconciliation", connection=Redis.from_url(os.environ.get("REDIS_URL","redis://localhost:6379")))
    job = q.enqueue("modules.billing.jobs.run_reconciliation", db.tenant_id, date_from, date_to)
    return {"job_id": job.id, "status": "enqueued"}

# Invoices
@router.post("/invoices/generate")
async def generate_invoice(data: InvoiceGenerate, db: TenantSession = Depends(get_db), branding: BrandingService = Depends(get_branding), user=Depends(lambda r: r.state.current_user)):
    svc = InvoiceService(db, branding, StorageService(), EmailService())
    return await svc.generate_invoice(data.client_id, data.matter_ids, date.fromisoformat(data.period_start), date.fromisoformat(data.period_end), user.id)

@router.get("/invoices/{invoice_id}/pdf")
async def get_pdf(invoice_id: int, db: TenantSession = Depends(get_db), branding: BrandingService = Depends(get_branding)):
    pdf = await InvoiceService(db, branding, StorageService(), EmailService()).generate_pdf(invoice_id)
    return Response(content=pdf, media_type="application/pdf")

@router.post("/invoices/{invoice_id}/send")
async def send_invoice(invoice_id: int, db: TenantSession = Depends(get_db), branding: BrandingService = Depends(get_branding), user=Depends(lambda r: r.state.current_user)):
    if not await InvoiceService(db, branding, StorageService(), EmailService()).send_invoice(invoice_id, user.id):
        raise HTTPException(400, "Failed to send")
    return {"status": "sent"}

@router.get("/invoices/{invoice_id}/ledes")
async def ledes_export(invoice_id: int, db: TenantSession = Depends(get_db), branding: BrandingService = Depends(get_branding)):
    from modules.billing.adapters.ledes import LEDESExporter
    content = await LEDESExporter(db, law_firm_id=branding.get("firm_name","")).export_invoice(invoice_id)
    return Response(content=content, media_type="text/plain", headers={"Content-Disposition": f"attachment; filename=ledes_{invoice_id}.txt"})

# Payments + LawPay
@router.post("/payments")
async def record_payment(data: PaymentRecord, db: TenantSession = Depends(get_db), user=Depends(lambda r: r.state.current_user)):
    return await PaymentService(db).record_payment(data.invoice_id, Decimal(str(data.amount)), data.method, user.id, reference_number=data.reference_number, payment_date=date.fromisoformat(data.payment_date) if data.payment_date else None)

@router.post("/invoices/{invoice_id}/payment-link")
async def payment_link(invoice_id: int, db: TenantSession = Depends(get_db), user=Depends(lambda r: r.state.current_user)):
    from modules.billing.adapters.lawpay import LawPayAdapter
    from core.models.billing import Invoice
    from core.models.client import Client
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv: raise HTTPException(404)
    client = db.query(Client).filter(Client.id == inv.client_id).first()
    adapter = LawPayAdapter(os.environ.get("LAWPAY_API_URL",""), os.environ.get("LAWPAY_API_KEY",""), os.environ.get("LAWPAY_SECRET_KEY",""))
    result = await adapter.create_payment_link(inv.invoice_number, int(inv.balance_due * 100), client.client_name if client else "", client.email if client else "", f"Invoice {inv.invoice_number}")
    inv.lawpay_invoice_id = result.get("charge_id","")
    db.commit()
    await adapter.close()
    return result

@router.post("/webhooks/lawpay")
async def lawpay_webhook(request: Request, db: TenantSession = Depends(get_db)):
    payload = json.loads(await request.body())
    payment = await PaymentService(db).process_lawpay_webhook(payload)
    return {"status": "ok"}

# Trust
@router.post("/trust/deposit")
async def trust_deposit(data: TrustDeposit, db: TenantSession = Depends(get_db), user=Depends(lambda r: r.state.current_user)):
    return await TrustService(db).deposit(data.client_id, Decimal(str(data.amount)), data.description, user.id, matter_id=data.matter_id, reference_number=data.reference_number)

@router.post("/trust/disburse")
async def trust_disburse(data: TrustDeposit, db: TenantSession = Depends(get_db), user=Depends(lambda r: r.state.current_user)):
    return await TrustService(db).disburse(data.client_id, Decimal(str(data.amount)), data.description, user.id, matter_id=data.matter_id)

@router.get("/trust/{client_id}/ledger")
async def trust_ledger(client_id: int, db: TenantSession = Depends(get_db)):
    return await TrustService(db).get_client_ledger(client_id)

@router.post("/trust/reconcile")
async def trust_reconcile(bank_balance: float = Body(..., embed=True), db: TenantSession = Depends(get_db)):
    return await TrustService(db).three_way_reconciliation(Decimal(str(bank_balance)))

# Reports
@router.get("/reports")
async def list_reports(category: Optional[str] = None, db: TenantSession = Depends(get_db)):
    return await ReportService(db).list_reports(category)

@router.post("/reports/{slug}/run")
async def run_report(slug: str, filters: dict = Body(...), db: TenantSession = Depends(get_db)):
    return await ReportService(db).run_report(slug, filters)

@router.post("/reports/{slug}/excel")
async def export_excel(slug: str, filters: dict = Body(...), db: TenantSession = Depends(get_db)):
    data = await ReportService(db).export_to_excel(slug, filters)
    return Response(content=data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename={slug}.xlsx"})

@router.post("/reports/seed")
async def seed_reports(db: TenantSession = Depends(get_db)):
    await ReportService(db).seed_system_reports()
    return {"status": "seeded"}

# QBO Export
@router.post("/qbo/export/monthly")
async def trigger_qbo(year: int = Body(...), month: int = Body(...), db: TenantSession = Depends(get_db)):
    from rq import Queue; from redis import Redis
    q = Queue("qbo_export", connection=Redis.from_url(os.environ.get("REDIS_URL","redis://localhost:6379")))
    job = q.enqueue("modules.billing.jobs.run_monthly_qbo_export", db.tenant_id, year, month)
    return {"job_id": job.id, "status": "enqueued"}
