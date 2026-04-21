"""Invoice service — uses Chat 0 columns: total_amount, balance_due, amount_paid."""
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import Optional, List
from io import BytesIO
from core.db.base import TenantSession
from core.audit import write_audit
from core.services.branding import BrandingService
from core.services.storage import StorageService
from core.services.email import EmailService
from modules.billing.models.invoice import Invoice, InvoiceMatter, InvoiceLineItem
from modules.billing.models.time_entry import TimeEntry
from modules.billing.models.matter import Matter
from modules.billing.models.client import Client


class InvoiceService:
    def __init__(self, db: TenantSession, branding: BrandingService, storage: StorageService, email_service: EmailService):
        self.db = db
        self.branding = branding
        self.storage = storage
        self.email_service = email_service

    def _next_invoice_number(self) -> str:
        year = date.today().year
        prefix = f"INV-{year}-"
        last = self.db.query(Invoice).filter(Invoice.invoice_number.like(f"{prefix}%")).order_by(Invoice.invoice_number.desc()).first()
        seq = int(last.invoice_number.split("-")[-1]) + 1 if last else 1
        return f"{prefix}{seq:05d}"

    async def generate_invoice(self, client_id: int, matter_ids: List[int], period_start: date, period_end: date, user_id: int) -> Invoice:
        client = self.db.query(Client).filter(Client.id == client_id).first()
        if not client:
            raise ValueError("Client not found")
        is_consolidated = len(matter_ids) > 1
        invoice = Invoice(
            tenant_id=self.db.tenant_id, client_id=client_id,
            invoice_number=self._next_invoice_number(),
            invoice_date=date.today(), due_date=date.today() + timedelta(days=30),
            billing_type="mixed" if is_consolidated else "hourly",
        )
        self.db.add(invoice)
        self.db.flush()
        grand_total = Decimal("0")
        for mid in matter_ids:
            matter = self.db.query(Matter).filter(Matter.id == mid).first()
            if not matter:
                continue
            entries = (self.db.query(TimeEntry)
                .filter(TimeEntry.matter_id == mid, TimeEntry.status == "approved",
                    TimeEntry.entry_date >= period_start, TimeEntry.entry_date <= period_end,
                    TimeEntry.invoice_id.is_(None))
                .order_by(TimeEntry.entry_date).all())
            matter_subtotal = Decimal("0")
            for idx, entry in enumerate(entries):
                line_amount = entry.amount if matter.billing_type == "hourly" else Decimal("0")
                line = InvoiceLineItem(
                    tenant_id=self.db.tenant_id, invoice_id=invoice.id, time_entry_id=entry.id,
                    matter_id=mid, line_date=entry.entry_date, description=entry.description,
                    hours=entry.hours, rate=entry.rate, amount=line_amount, line_type="time", sort_order=idx)
                self.db.add(line)
                matter_subtotal += line_amount
                entry.status = "billed"
                entry.invoice_id = invoice.id
            if matter.billing_type == "flat_fee" and matter.flat_fee_amount:
                flat_line = InvoiceLineItem(
                    tenant_id=self.db.tenant_id, invoice_id=invoice.id, matter_id=mid,
                    line_date=date.today(), description=f"Flat fee - {matter.matter_name}",
                    amount=matter.flat_fee_amount, line_type="flat_fee", sort_order=len(entries))
                self.db.add(flat_line)
                matter_subtotal = matter.flat_fee_amount
            inv_matter = InvoiceMatter(tenant_id=self.db.tenant_id, invoice_id=invoice.id, matter_id=mid, subtotal=matter_subtotal)
            self.db.add(inv_matter)
            grand_total += matter_subtotal
        invoice.subtotal = grand_total
        invoice.total_amount = grand_total
        invoice.balance_due = grand_total
        write_audit(self.db, tenant_id=self.db.tenant_id, user_id=user_id,
            action="invoice.generate", entity_type="invoice", entity_id=invoice.id,
            new_value={"client_id": client_id, "total": str(grand_total)})
        self.db.commit()
        return invoice

    async def generate_pdf(self, invoice_id: int) -> bytes:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER, TA_JUSTIFY
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table as RLTable, TableStyle as RLTableStyle
        from reportlab.lib import colors
        invoice = self.db.query(Invoice).filter(Invoice.id == invoice_id).first()
        if not invoice:
            raise ValueError("Invoice not found")
        client = self.db.query(Client).filter(Client.id == invoice.client_id).first()
        brand = self.branding
        buf = BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=inch, bottomMargin=inch)
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(name="FirmName", fontName="Times-Bold", fontSize=16, alignment=TA_CENTER, spaceAfter=6))
        styles.add(ParagraphStyle(name="Body12", fontName="Times-Roman", fontSize=12, alignment=TA_JUSTIFY, spaceBefore=4, spaceAfter=4))
        styles.add(ParagraphStyle(name="RightAlign", fontName="Times-Roman", fontSize=12, alignment=TA_RIGHT))
        elements = []
        elements.append(Paragraph(brand.get("firm_name", ""), styles["FirmName"]))
        elements.append(Spacer(1, 0.3*inch))
        elements.append(Paragraph("INVOICE", styles["FirmName"]))
        elements.append(Spacer(1, 0.2*inch))
        elements.append(Paragraph(f"Invoice #: {invoice.invoice_number}  |  Date: {invoice.invoice_date}  |  Due: {invoice.due_date}", styles["Body12"]))
        elements.append(Spacer(1, 0.15*inch))
        elements.append(Paragraph(f"<b>Bill To:</b> {client.client_name if client else ''}", styles["Body12"]))
        if client and client.address1:
            elements.append(Paragraph(f"{client.address1}, {client.city or ''} {client.state or ''} {client.zip_code or ''}", styles["Body12"]))
        elements.append(Spacer(1, 0.2*inch))
        for inv_matter in invoice.matters:
            matter = self.db.query(Matter).filter(Matter.id == inv_matter.matter_id).first()
            if matter:
                elements.append(Paragraph(f"<b>Matter: {matter.matter_number} - {matter.matter_name}</b>", styles["Body12"]))
            lines = [li for li in invoice.line_items if li.matter_id == inv_matter.matter_id]
            if lines:
                tdata = [["Date", "Description", "Hours", "Rate", "Amount"]]
                for li in sorted(lines, key=lambda x: x.sort_order):
                    tdata.append([str(li.line_date), li.description[:80], f"{li.hours:.2f}" if li.hours else "", f"${li.rate:.2f}" if li.rate else "", f"${li.amount:.2f}"])
                tdata.append(["", "", "", "Subtotal:", f"${inv_matter.subtotal:.2f}"])
                t = RLTable(tdata, colWidths=[0.9*inch, 3.2*inch, 0.7*inch, 0.8*inch, 1.0*inch])
                t.setStyle(RLTableStyle([("FONTNAME",(0,0),(-1,0),"Times-Bold"),("FONTNAME",(0,1),(-1,-1),"Times-Roman"),("FONTSIZE",(0,0),(-1,-1),10),("LINEBELOW",(0,0),(-1,0),1,colors.black),("ALIGN",(2,0),(-1,-1),"RIGHT"),("BOTTOMPADDING",(0,0),(-1,-1),3)]))
                elements.append(t)
                elements.append(Spacer(1, 0.15*inch))
        elements.append(Spacer(1, 0.2*inch))
        elements.append(Paragraph(f"<b>Total Due: ${invoice.total_amount:.2f}</b>", styles["RightAlign"]))
        doc.build(elements)
        pdf_bytes = buf.getvalue()
        buf.close()
        storage_path = f"billing/invoices/{invoice.invoice_number}.pdf"
        await self.storage.put(storage_path, pdf_bytes, content_type="application/pdf")
        invoice.pdf_path = storage_path
        self.db.commit()
        return pdf_bytes

    async def send_invoice(self, invoice_id: int, user_id: int) -> bool:
        invoice = self.db.query(Invoice).filter(Invoice.id == invoice_id).first()
        if not invoice:
            return False
        client = self.db.query(Client).filter(Client.id == invoice.client_id).first()
        if not client or not client.email:
            return False
        pdf_bytes = await self.generate_pdf(invoice_id)
        brand = self.branding
        body = f"Dear {client.client_name},\n\nPlease find attached invoice {invoice.invoice_number}.\n\nTotal Due: ${invoice.total_amount:.2f}\nDue Date: {invoice.due_date}\n\nThank you,\n{brand.get('firm_name', '')}"
        await self.email_service.send(to=client.email, subject=f"Invoice {invoice.invoice_number}", body=body,
            attachments=[{"filename": f"{invoice.invoice_number}.pdf", "content": pdf_bytes, "content_type": "application/pdf"}])
        invoice.status = "sent"
        invoice.sent_at = datetime.utcnow()
        write_audit(self.db, tenant_id=self.db.tenant_id, user_id=user_id,
            action="invoice.send", entity_type="invoice", entity_id=invoice.id, new_value={"sent_to": client.email})
        self.db.commit()
        return True

    async def void_invoice(self, invoice_id: int, user_id: int, reason: str = "") -> bool:
        invoice = self.db.query(Invoice).filter(Invoice.id == invoice_id).first()
        if not invoice:
            return False
        invoice.status = "void"
        for entry in self.db.query(TimeEntry).filter(TimeEntry.invoice_id == invoice_id).all():
            entry.status = "approved"
            entry.invoice_id = None
        write_audit(self.db, tenant_id=self.db.tenant_id, user_id=user_id,
            action="invoice.void", entity_type="invoice", entity_id=invoice_id, new_value={"reason": reason})
        self.db.commit()
        return True
