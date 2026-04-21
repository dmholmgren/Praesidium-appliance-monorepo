from datetime import datetime, date
from sqlalchemy import Column, BigInteger, String, Text, DateTime, Date, Enum, ForeignKey, Numeric, Integer
from sqlalchemy.orm import relationship
from core.db.base import Base

class TimeEntry(Base):
    __tablename__ = "time_entries"
    __table_args__ = {"extend_existing": True}
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    matter_id = Column(BigInteger, ForeignKey("matters.id"), nullable=False, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    entry_date = Column(Date, nullable=False, default=date.today)
    hours = Column(Numeric(5,2), nullable=False)
    rate = Column(Numeric(10,2), nullable=False)
    amount = Column(Numeric(10,2), nullable=False)
    description = Column(Text, nullable=False)
    source = Column(Enum("manual","email","phone","document","calendar","dictation","manictime","ai_suggested", name="time_source_enum"), nullable=False, default="manual")
    status = Column(Enum("draft","ai_suggested","reviewed","approved","billed","written_off", name="time_entry_status_enum"), nullable=False, default="draft")
    ai_original_description = Column(Text, nullable=True)
    ai_confidence = Column(Numeric(3,2), nullable=True)
    utbms_code = Column(String(20), nullable=True)
    reviewed_by = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    invoice_id = Column(BigInteger, ForeignKey("invoices.id"), nullable=True)
    source_metadata = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    matter = relationship("Matter", back_populates="time_entries")
    sources = relationship("TimeEntrySource", back_populates="time_entry", lazy="selectin")

class TimeEntrySource(Base):
    __tablename__ = "time_entry_sources"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    time_entry_id = Column(BigInteger, ForeignKey("time_entries.id"), nullable=False, index=True)
    source_type = Column(String(50), nullable=False)
    source_id = Column(String(255), nullable=False)
    source_data = Column(Text, nullable=True)
    captured_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    duration_seconds = Column(Integer, nullable=True)
    matter_confidence = Column(Numeric(5,4), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    time_entry = relationship("TimeEntry", back_populates="sources")
