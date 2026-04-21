from datetime import datetime
from sqlalchemy import Column, BigInteger, String, Text, DateTime, ForeignKey, Numeric, Integer
from core.db.base import Base

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
