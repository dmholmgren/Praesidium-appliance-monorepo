from datetime import datetime
from sqlalchemy import Column, BigInteger, String, Text, DateTime, Enum, ForeignKey, Numeric, Boolean
from core.db.base import Base

class BillingQCResult(Base):
    __tablename__ = "billing_qc_results"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    time_entry_id = Column(BigInteger, ForeignKey("time_entries.id"), nullable=False, index=True)
    qc_run_id = Column(String(36), nullable=False, index=True)
    check_type = Column(String(50), nullable=False)
    severity = Column(Enum("info","warning","error", name="qc_severity_enum"), nullable=False, default="warning")
    confidence = Column(Numeric(5,4), nullable=False)
    message = Column(Text, nullable=False)
    suggested_fix = Column(Text, nullable=True)
    is_resolved = Column(Boolean, nullable=False, default=False)
    resolved_by_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    resolution_action = Column(String(20), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
