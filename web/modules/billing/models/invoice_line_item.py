from datetime import datetime, date
from sqlalchemy import Column, BigInteger, String, Text, DateTime, Date, Enum, ForeignKey, Numeric, Integer
from core.db.base import Base

class InvoiceLineItem(Base):
    __tablename__ = "invoice_line_items"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    invoice_id = Column(BigInteger, ForeignKey("invoices.id"), nullable=False)
    time_entry_id = Column(BigInteger, ForeignKey("time_entries.id"), nullable=True)
    matter_id = Column(BigInteger, ForeignKey("matters.id"), nullable=False)
    line_date = Column(Date, nullable=False)
    description = Column(Text, nullable=False)
    timekeeper_name = Column(String(255), nullable=True)
    hours = Column(Numeric(6,2), nullable=True)
    rate = Column(Numeric(10,2), nullable=True)
    amount = Column(Numeric(12,2), nullable=False)
    utbms_task_code = Column(String(20), nullable=True)
    line_type = Column(Enum("time","expense","flat_fee","retainer_draw","adjustment", name="line_type_enum"), nullable=False, default="time")
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
