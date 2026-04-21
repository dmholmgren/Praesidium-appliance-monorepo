"""QuickBooks Handoff Export — IIF + CSV, no API creds needed."""
import csv
from datetime import date
from io import StringIO
from calendar import monthrange
from core.db.base import TenantSession
from core.models.billing import Invoice
from modules.billing.models.invoice_line_item import InvoiceLineItem
from core.models.billing import Payment
from core.models.client import Client
from modules.billing.models.trust import TrustTransaction

class QBOExportAdapter:
    def __init__(self, db: TenantSession, chart_mapping: dict):
        self.db = db
        self.chart_mapping = chart_mapping

    async def export_invoices_csv(self, date_from: date, date_to: date) -> str:
        invoices = self.db.query(Invoice).filter(Invoice.invoice_date >= date_from, Invoice.invoice_date <= date_to, Invoice.status.notin_(["draft","void"])).all()
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["InvoiceNo","Customer","InvoiceDate","DueDate","ItemDescription","ItemQty","ItemRate","ItemAmount"])
        for inv in invoices:
            client = self.db.query(Client).filter(Client.id == inv.client_id).first()
            for li in inv.line_items:
                writer.writerow([inv.invoice_number, client.client_name if client else "", inv.invoice_date.strftime("%m/%d/%Y"), inv.due_date.strftime("%m/%d/%Y"), li.description[:250], float(li.hours) if li.hours else 1, float(li.rate) if li.rate else float(li.amount), float(li.amount)])
        return output.getvalue()

    async def export_payments_csv(self, date_from: date, date_to: date) -> str:
        payments = self.db.query(Payment).filter(Payment.payment_date >= date_from, Payment.payment_date <= date_to).all()
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["PaymentDate","Customer","InvoiceNo","Amount","Method","RefNumber"])
        for pmt in payments:
            inv = self.db.query(Invoice).filter(Invoice.id == pmt.invoice_id).first()
            client = self.db.query(Client).filter(Client.id == inv.client_id).first() if inv else None
            writer.writerow([pmt.payment_date.strftime("%m/%d/%Y"), client.client_name if client else "", inv.invoice_number if inv else "", float(pmt.amount), pmt.method, pmt.reference_number or ""])
        return output.getvalue()

    async def export_monthly_bundle(self, year: int, month: int) -> dict:
        d_from = date(year, month, 1)
        _, last_day = monthrange(year, month)
        d_to = date(year, month, last_day)
        return {"period": f"{year}-{month:02d}", "invoices_csv": await self.export_invoices_csv(d_from, d_to), "payments_csv": await self.export_payments_csv(d_from, d_to)}
