"""Trust accounting — deposits, disbursements, three-way reconciliation."""
from datetime import datetime, date
from decimal import Decimal
from typing import Optional
from core.db.base import TenantSession
from core.audit import write_audit
from modules.billing.models.trust import TrustLedger, TrustTransaction
from modules.billing.models.client import Client


class TrustService:
    def __init__(self, db: TenantSession):
        self.db = db

    async def get_or_create_ledger(self, client_id: int) -> TrustLedger:
        ledger = self.db.query(TrustLedger).filter(TrustLedger.client_id == client_id).first()
        if not ledger:
            ledger = TrustLedger(tenant_id=self.db.tenant_id, client_id=client_id, balance=Decimal("0"))
            self.db.add(ledger)
            self.db.flush()
        return ledger

    async def deposit(self, client_id: int, amount: Decimal, description: str, user_id: int,
                      matter_id=None, reference_number=None, transaction_date=None) -> TrustTransaction:
        if amount <= 0:
            raise ValueError("Deposit amount must be positive")
        ledger = await self.get_or_create_ledger(client_id)
        new_balance = ledger.balance + amount
        ledger.balance = new_balance
        txn = TrustTransaction(tenant_id=self.db.tenant_id, trust_ledger_id=ledger.id, client_id=client_id,
            matter_id=matter_id, transaction_type="deposit", amount=amount, balance_after=new_balance,
            description=description, reference_number=reference_number,
            transaction_date=transaction_date or date.today(), created_by_id=user_id)
        self.db.add(txn)
        write_audit(self.db, tenant_id=self.db.tenant_id, user_id=user_id,
            action="trust.deposit", entity_type="trust_transaction", entity_id=txn.id,
            new_value={"client_id": client_id, "amount": str(amount), "balance_after": str(new_balance)})
        self.db.commit()
        return txn

    async def disburse(self, client_id: int, amount: Decimal, description: str, user_id: int,
                       matter_id=None, payment_id=None, reference_number=None, transaction_date=None) -> TrustTransaction:
        if amount <= 0:
            raise ValueError("Disbursement amount must be positive")
        ledger = await self.get_or_create_ledger(client_id)
        new_balance = ledger.balance - amount
        ledger.balance = new_balance
        txn = TrustTransaction(tenant_id=self.db.tenant_id, trust_ledger_id=ledger.id, client_id=client_id,
            matter_id=matter_id, transaction_type="disbursement", amount=amount, balance_after=new_balance,
            description=description, reference_number=reference_number, payment_id=payment_id,
            transaction_date=transaction_date or date.today(), created_by_id=user_id)
        self.db.add(txn)
        write_audit(self.db, tenant_id=self.db.tenant_id, user_id=user_id,
            action="trust.disburse", entity_type="trust_transaction", entity_id=txn.id,
            new_value={"client_id": client_id, "amount": str(amount), "balance_after": str(new_balance)})
        if new_balance < 0:
            write_audit(self.db, tenant_id=self.db.tenant_id, user_id=0,
                action="trust.negative_balance_alert", entity_type="trust_ledger", entity_id=ledger.id,
                new_value={"client_id": client_id, "balance": str(new_balance)})
        self.db.commit()
        return txn

    async def get_client_ledger(self, client_id: int) -> dict:
        ledger = await self.get_or_create_ledger(client_id)
        txns = self.db.query(TrustTransaction).filter(TrustTransaction.client_id == client_id).order_by(TrustTransaction.transaction_date.desc()).all()
        return {"client_id": client_id, "balance": float(ledger.balance), "transactions": txns}

    async def three_way_reconciliation(self, bank_balance: Decimal) -> dict:
        from sqlalchemy import func
        total_client = self.db.query(func.sum(TrustLedger.balance)).scalar() or Decimal("0")
        discrepancy = bank_balance - total_client
        ledgers = self.db.query(TrustLedger).all()
        details = []
        negative = []
        for l in ledgers:
            client = self.db.query(Client).filter(Client.id == l.client_id).first()
            d = {"client_id": l.client_id, "client_name": client.client_name if client else "Unknown", "balance": float(l.balance)}
            details.append(d)
            if l.balance < 0:
                negative.append(d)
            l.last_reconciled_at = datetime.utcnow()
        self.db.commit()
        return {"bank_balance": float(bank_balance), "total_client_balances": float(total_client),
            "discrepancy": float(discrepancy), "is_reconciled": abs(discrepancy) < Decimal("0.01"),
            "negative_balance_alerts": negative, "client_details": details}
