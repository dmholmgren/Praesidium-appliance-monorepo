from datetime import datetime, date
from sqlalchemy import Column, BigInteger, String, Text, Boolean, DateTime, Date, Enum, ForeignKey, Numeric
from sqlalchemy.orm import relationship
from core.db.base import Base

class Matter(Base):
    __tablename__ = "matters"
    __table_args__ = {"extend_existing": True}
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    client_id = Column(BigInteger, ForeignKey("clients.id"), nullable=False, index=True)
    matter_number = Column(String(50), nullable=False)
    matter_name = Column(String(500), nullable=False)
    matter_type = Column(Enum("litigation","transactional","regulatory","advisory","bankruptcy","immigration","family","criminal","estate_planning","real_estate","ip","employment","general", name="matter_type_enum"), nullable=False, default="general")
    status = Column(Enum("active","closed","inactive", name="matter_status_enum"), nullable=True, default="active")
    responsible_attorney_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    originating_attorney_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    billing_type = Column(Enum("hourly","flat_fee","contingency","retainer", name="matter_billing_type_enum"), nullable=False)
    hourly_rate = Column(Numeric(10,2), nullable=True)
    flat_fee_amount = Column(Numeric(10,2), nullable=True)
    contingency_pct = Column(Numeric(5,2), nullable=True)
    court = Column(String(255), nullable=True)
    cause_number = Column(String(100), nullable=True)
    jurisdiction = Column(String(100), nullable=True)
    judge = Column(String(255), nullable=True)
    sol_date = Column(Date, nullable=True)
    open_date = Column(Date, nullable=False, default=date.today)
    close_date = Column(Date, nullable=True)
    folder_path = Column(String(1000), nullable=True)
    legacy_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    client = relationship("Client", back_populates="matters")
    timekeepers = relationship("MatterTimekeeper", back_populates="matter", lazy="selectin")
    time_entries = relationship("TimeEntry", back_populates="matter", lazy="dynamic")
    invoices = relationship("InvoiceMatter", back_populates="matter", lazy="dynamic")

class MatterTimekeeper(Base):
    __tablename__ = "matter_timekeepers"
    __table_args__ = {"extend_existing": True}
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    matter_id = Column(BigInteger, ForeignKey("matters.id"), nullable=False, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(Enum("originating","responsible","working","supervising", name="tk_role_enum"), nullable=False, default="working")
    is_active = Column(Boolean, nullable=False, default=True)
    assigned_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    matter = relationship("Matter", back_populates="timekeepers")
