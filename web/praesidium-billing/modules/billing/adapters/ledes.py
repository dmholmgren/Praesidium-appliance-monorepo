"""LEDES 1998B format exporter."""
from io import StringIO
from core.db.base import TenantSession
from modules.billing.models.invoice import Invoice, InvoiceLineItem
from modules.billing.models.matter import Matter
from modules.billing.models.client import Client

class LEDESExporter:
    HEADER = "LEDES1998B[]"
    COLUMNS = "INVOICE_DATE|INVOICE_NUMBER|CLIENT_ID|LAW_FIRM_MATTER_ID|INVOICE_TOTAL|BILLING_START_DATE|BILLING_END_DATE|INVOICE_DESCRIPTION|LINE_ITEM_NUMBER|EXP/FEE/INV_ADJ_TYPE|LINE_ITEM_NUMBER_OF_UNITS|LINE_ITEM_ADJUSTMENT_AMOUNT|LINE_ITEM_TOTAL|LINE_ITEM_DATE|LINE_ITEM_TASK_CODE|LINE_ITEM_EXPENSE_CODE|LINE_ITEM_ACTIVITY_CODE|TIMEKEEPER_ID|LINE_ITEM_DESCRIPTION|LAW_FIRM_ID|LINE_ITEM_UNIT_COST|TIMEKEEPER_NAME|TIMEKEEPER_CLASSIFICATION[]"

    def __init__(self, db: TenantSession, law_firm_id: str = ""):
        self.db = db
        self.law_firm_id = law_firm_id

    def _esc(self, val) -> str:
        return str(val).replace("|"," ").replace("\n"," ") if val else ""

    async def export_invoice(self, invoice_id: int) -> str:
        invoice = self.db.query(Invoice).filter(Invoice.id == invoice_id).first()
        if not invoice:
            raise ValueError("Invoice not found")
        client = self.db.query(Client).filter(Client.id == invoice.client_id).first()
        output = StringIO()
        output.write(self.HEADER + "\n" + self.COLUMNS + "\n")
        line_num = 0
        for inv_matter in invoice.matters:
            matter = self.db.query(Matter).filter(Matter.id == inv_matter.matter_id).first()
            items = [li for li in invoice.line_items if li.matter_id == inv_matter.matter_id]
            for li in sorted(items, key=lambda x: x.sort_order):
                line_num += 1
                fee_type = "F" if li.line_type in ("time","flat_fee") else "E"
                output.write(f"{invoice.invoice_date.strftime('%Y%m%d')}|{self._esc(invoice.invoice_number)}|{self._esc(client.client_name if client else '')}|{self._esc(matter.matter_number if matter else '')}|{invoice.total_amount:.2f}|{invoice.invoice_date.strftime('%Y%m%d')}|{invoice.invoice_date.strftime('%Y%m%d')}||{line_num}|{fee_type}|{li.hours:.2f if li.hours else '1.00'}|0.00|{li.amount:.2f}|{li.line_date.strftime('%Y%m%d')}|{self._esc(li.utbms_task_code or '')}|||{self._esc(li.timekeeper_name or '')}|{self._esc(li.description[:250])}|{self._esc(self.law_firm_id)}|{li.rate:.2f if li.rate else '0.00'}|{self._esc(li.timekeeper_name or '')}|PARTNER[]\n")
        return output.getvalue()
