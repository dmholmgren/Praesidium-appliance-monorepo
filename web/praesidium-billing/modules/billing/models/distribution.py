from datetime import datetime, date
from sqlalchemy import Column, BigInteger, String, Text, DateTime, Date, ForeignKey, Numeric
from core.db.base import Base

class Distribution(Base):
    __tablename__ = "distributions"
    __table_args__ = {"extend_existing": True}
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    payment_id = Column(BigInteger, ForeignKey("payments.id"), nullable=False, index=True)
    invoice_id = Column(BigInteger, ForeignKey("invoices.id"), nullable=False, index=True)
    matter_id = Column(BigInteger, ForeignKey("matters.id"), nullable=False, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    credit_type = Column(String(50), nullable=False)
    hours = Column(Numeric(6,2), nullable=True)
    rate = Column(Numeric(10,2), nullable=True)
    gross_amount = Column(Numeric(14,2), nullable=False)
    overhead_deduction = Column(Numeric(14,2), nullable=False, default=0)
    net_amount = Column(Numeric(14,2), nullable=False)
    distribution_period = Column(String(7), nullable=False)
    run_date = Column(Date, nullable=False, default=date.today)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
