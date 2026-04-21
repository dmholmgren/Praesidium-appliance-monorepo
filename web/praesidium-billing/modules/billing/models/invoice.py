from datetime import datetime, date
from sqlalchemy import Column, BigInteger, String, Text, DateTime, Date, Enum, ForeignKey, Numeric, Integer
from sqlalchemy.orm import relationship
from core.db.base import Base

class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = {"extend_existing": True}
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    invoice_number = Column(String(50), nullable=False)
    client_id = Column(BigInteger, ForeignKey("clients.id"), nullable=False, index=True)
    invoice_date = Column(Date, nullable=False, default=date.today)
    due_date = Column(Date, nullable=False)
    subtotal = Column(Numeric(12,2), nullable=False, default=0)
    tax_amount = Column(Numeric(10,2), nullable=True, default=0)
    total_amount = Column(Numeric(12,2), nullable=False, default=0)
    amount_paid = Column(Numeric(12,2), nullable=True, default=0)
    balance_due = Column(Numeric(12,2), nullable=False, default=0)
    status = Column(Enum("draft","sent","partial","paid","overdue","void", name="invoice_status_enum"), nullable=False, default="draft")
    billing_type = Column(Enum("hourly","flat_fee","contingency","retainer","mixed", name="invoice_billing_type_enum"), nullable=False)
    notes = Column(Text, nullable=True)
    ledes_data = Column(Text, nullable=True)
    pdf_path = Column(String(1000), nullable=True)
    lawpay_invoice_id = Column(String(255), nullable=True)
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    matters = relationship("InvoiceMatter", back_populates="invoice", lazy="selectin")
    line_items = relationship("InvoiceLineItem", back_populates="invoice", lazy="selectin")
    payments = relationship("Payment", back_populates="invoice", lazy="dynamic")

class InvoiceMatter(Base):
    __tablename__ = "invoice_matters"
    __table_args__ = {"extend_existing": True}
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    invoice_id = Column(BigInteger, ForeignKey("invoices.id"), nullable=False, index=True)
    matter_id = Column(BigInteger, ForeignKey("matters.id"), nullable=False, index=True)
    subtotal = Column(Numeric(12,2), nullable=False, default=0)
    invoice = relationship("Invoice", back_populates="matters")
    matter = relationship("Matter", back_populates="invoices")

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
    invoice = relationship("Invoice", back_populates="line_items")
