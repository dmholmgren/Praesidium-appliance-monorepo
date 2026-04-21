from datetime import datetime, date
from sqlalchemy import Column, BigInteger, String, Text, DateTime, Date, Enum, ForeignKey, Numeric
from sqlalchemy.orm import relationship
from core.db.base import Base

class TrustLedger(Base):
    __tablename__ = "trust_ledger"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    client_id = Column(BigInteger, ForeignKey("clients.id"), nullable=False, unique=True)
    balance = Column(Numeric(14,2), nullable=False, default=0)
    last_reconciled_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    client = relationship("Client")
    transactions = relationship("TrustTransaction", back_populates="ledger", lazy="dynamic")

class TrustTransaction(Base):
    __tablename__ = "trust_transactions"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    trust_ledger_id = Column(BigInteger, ForeignKey("trust_ledger.id"), nullable=False)
    client_id = Column(BigInteger, ForeignKey("clients.id"), nullable=False)
    matter_id = Column(BigInteger, ForeignKey("matters.id"), nullable=True)
    transaction_type = Column(Enum("deposit","disbursement","interest","adjustment", name="trust_txn_type_enum"), nullable=False)
    amount = Column(Numeric(14,2), nullable=False)
    balance_after = Column(Numeric(14,2), nullable=False)
    description = Column(Text, nullable=False)
    reference_number = Column(String(255), nullable=True)
    payment_id = Column(BigInteger, ForeignKey("payments.id"), nullable=True)
    transaction_date = Column(Date, nullable=False, default=date.today)
    created_by_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    ledger = relationship("TrustLedger", back_populates="transactions")
