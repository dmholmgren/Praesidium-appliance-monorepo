"""
Billing module ORM models.

Tables: time_entries, invoices, invoice_matters, payments, distributions
"""

from datetime import datetime, date

from sqlalchemy import (
    UUID,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    JSON,
)

from core.db.base import Base


class TimeEntry(Base):
    __tablename__ = "time_entries"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    matter_id = Column(UUID(as_uuid=True), ForeignKey("matters.id"), nullable=False)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    entry_date = Column(Date, nullable=False)
    hours = Column(Numeric(5, 2), nullable=False)
    rate = Column(Numeric(10, 2), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    description = Column(Text, nullable=False)
    source = Column(
        Enum("manual", "email", "phone", "document", "calendar", "dictation",
             "manictime", "ai_suggested", name="time_source_enum"),
        nullable=False,
        default="manual",
    )
    status = Column(
        Enum("draft", "ai_suggested", "reviewed", "approved", "billed", "written_off",
             name="time_status_enum"),
        nullable=False,
        default="draft",
    )
    ai_original_description = Column(Text)
    ai_confidence = Column(Numeric(3, 2))
    utbms_code = Column(String(20))
    reviewed_by = Column(BigInteger, ForeignKey("users.id"))
    reviewed_at = Column(DateTime)
    invoice_id = Column(UUID(as_uuid=True), ForeignKey("invoices.id"))
    source_metadata = Column(JSON)  # email ID, phone CDR, document path, etc.
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_tenant_user", "tenant_id", "user_id"),
        Index("idx_tenant_date", "tenant_id", "entry_date"),
        Index("idx_tenant_status", "tenant_id", "status"),
        Index("idx_tenant_invoice", "tenant_id", "invoice_id"),
    )


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    invoice_number = Column(String(50), nullable=False)
    client_id = Column(BigInteger, ForeignKey("clients.id"), nullable=False)
    invoice_date = Column(Date, nullable=False)
    due_date = Column(Date, nullable=False)
    subtotal = Column(Numeric(12, 2), nullable=False, default=0)
    tax_amount = Column(Numeric(10, 2), default=0)
    total_amount = Column(Numeric(12, 2), nullable=False, default=0)
    amount_paid = Column(Numeric(12, 2), default=0)
    balance_due = Column(Numeric(12, 2), nullable=False, default=0)
    status = Column(
        Enum("draft", "sent", "partial", "paid", "overdue", "void",
             name="invoice_status_enum"),
        nullable=False,
        default="draft",
    )
    billing_type = Column(
        Enum("hourly", "flat_fee", "contingency", "retainer", "mixed",
             name="invoice_billing_type_enum"),
        nullable=False,
    )
    notes = Column(Text)
    ledes_data = Column(JSON)  # LEDES 1998B export data
    pdf_path = Column(String(1000))
    lawpay_invoice_id = Column(String(255))
    sent_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant", "tenant_id"),
        Index("idx_tenant_client", "tenant_id", "client_id"),
        Index("idx_tenant_number", "tenant_id", "invoice_number", unique=True),
        Index("idx_tenant_status", "tenant_id", "status"),
    )


class InvoiceMatter(Base):
    __tablename__ = "invoice_matters"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    invoice_id = Column(UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False)
    matter_id = Column(UUID(as_uuid=True), ForeignKey("matters.id"), nullable=False)
    subtotal = Column(Numeric(12, 2), nullable=False, default=0)

    __table_args__ = (
        Index("idx_tenant_invoice", "tenant_id", "invoice_id"),
        Index("idx_tenant_matter", "tenant_id", "matter_id"),
    )


class Payment(Base):
    __tablename__ = "payments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    invoice_id = Column(UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False)
    payment_date = Column(Date, nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    method = Column(
        Enum("check", "wire", "ach", "credit_card", "lawpay", "cash", "trust",
             name="payment_method_enum"),
        nullable=False,
    )
    reference_number = Column(String(255))
    lawpay_payment_id = Column(String(255))
    notes = Column(Text)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant_invoice", "tenant_id", "invoice_id"),
        Index("idx_tenant_date", "tenant_id", "payment_date"),
    )


class Distribution(Base):
    __tablename__ = "distributions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    period = Column(String(7), nullable=False)  # YYYY-MM
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    collected_amount = Column(Numeric(12, 2), nullable=False, default=0)
    origination_credit = Column(Numeric(12, 2), default=0)
    distribution_amount = Column(Numeric(12, 2), nullable=False, default=0)
    formula = Column(String(50))  # "100/75/25", "equitable"
    status = Column(
        Enum("draft", "approved", "distributed", name="distribution_status_enum"),
        nullable=False,
        default="draft",
    )
    approved_by = Column(BigInteger, ForeignKey("users.id"))
    approved_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant_period", "tenant_id", "period"),
        Index("idx_tenant_user", "tenant_id", "user_id"),
    )
