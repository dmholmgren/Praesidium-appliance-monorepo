from datetime import datetime, date
from sqlalchemy import Column, BigInteger, String, Text, DateTime, Date, Enum, ForeignKey, Numeric
from sqlalchemy.orm import relationship
from core.db.base import Base

class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = {"extend_existing": True}
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    invoice_id = Column(BigInteger, ForeignKey("invoices.id"), nullable=False, index=True)
    payment_date = Column(Date, nullable=False, default=date.today)
    amount = Column(Numeric(12,2), nullable=False)
    method = Column(Enum("check","wire","ach","credit_card","lawpay","cash","trust", name="payment_method_enum"), nullable=False)
    reference_number = Column(String(255), nullable=True)
    lawpay_payment_id = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    invoice = relationship("Invoice", back_populates="payments")
