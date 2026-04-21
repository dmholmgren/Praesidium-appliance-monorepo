"""Payment service — uses Chat 0 columns: method, balance_due, amount_paid, lawpay_payment_id."""
from datetime import datetime, date
from decimal import Decimal
from typing import Optional
from core.db.base import TenantSession
from core.audit import write_audit
from modules.billing.models.payment import Payment
from modules.billing.models.invoice import Invoice


class PaymentService:
    def __init__(self, db: TenantSession):
        self.db = db

    async def record_payment(self, invoice_id: int, amount: Decimal, method: str, user_id: int,
                             reference_number=None, lawpay_payment_id=None, payment_date=None, notes=None) -> Payment:
        invoice = self.db.query(Invoice).filter(Invoice.id == invoice_id).first()
        if not invoice:
            raise ValueError("Invoice not found")
        payment = Payment(tenant_id=self.db.tenant_id, invoice_id=invoice_id, amount=amount,
            payment_date=payment_date or date.today(), method=method,
            reference_number=reference_number, lawpay_payment_id=lawpay_payment_id, notes=notes)
        self.db.add(payment)
        invoice.amount_paid = (invoice.amount_paid or Decimal("0")) + amount
        invoice.balance_due = max(Decimal("0"), invoice.balance_due - amount)
        invoice.status = "paid" if invoice.balance_due <= 0 else "partial"
        invoice.updated_at = datetime.utcnow()
        write_audit(self.db, tenant_id=self.db.tenant_id, user_id=user_id,
            action="payment.record", entity_type="payment", entity_id=payment.id,
            new_value={"invoice_id": invoice_id, "amount": str(amount), "method": method})
        self.db.commit()
        return payment

    async def process_lawpay_webhook(self, payload: dict) -> Optional[Payment]:
        txn_id = payload.get("transaction_id") or payload.get("id")
        if not txn_id:
            return None
        existing = self.db.query(Payment).filter(Payment.lawpay_payment_id == txn_id).first()
        if existing:
            return existing
        invoice_number = payload.get("custom_id") or payload.get("reference")
        invoice = self.db.query(Invoice).filter(Invoice.invoice_number == invoice_number).first()
        if not invoice:
            return None
        amount = Decimal(str(payload.get("amount", 0))) / Decimal("100")
        method = "lawpay"
        return await self.record_payment(invoice_id=invoice.id, amount=amount, method=method,
            user_id=0, lawpay_payment_id=txn_id)

    async def write_off(self, invoice_id: int, user_id: int, reason: str) -> bool:
        invoice = self.db.query(Invoice).filter(Invoice.id == invoice_id).first()
        if not invoice:
            return False
        write_off_amount = invoice.balance_due
        invoice.balance_due = Decimal("0")
        invoice.status = "void"
        write_audit(self.db, tenant_id=self.db.tenant_id, user_id=user_id,
            action="invoice.write_off", entity_type="invoice", entity_id=invoice_id,
            new_value={"amount": str(write_off_amount), "reason": reason})
        self.db.commit()
        return True
