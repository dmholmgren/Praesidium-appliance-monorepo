"""
Court module ORM models.

Tables: deadlines
"""

from datetime import datetime, date

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
)

from core.db.base import Base


class Deadline(Base):
    __tablename__ = "deadlines"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    matter_id = Column(BigInteger, ForeignKey("matters.id"), nullable=False)
    deadline_date = Column(Date, nullable=False)
    deadline_type = Column(String(100), nullable=False)  # trial|response|discovery_cutoff|sol|etc.
    description = Column(Text)
    rule_basis = Column(String(500))  # e.g. "FRCP 12(a)(1)(A)"
    anchor_date = Column(Date)
    anchor_description = Column(String(500))
    is_confirmed = Column(Boolean, default=False)
    confirmed_by = Column(BigInteger, ForeignKey("users.id"))
    confirmed_at = Column(DateTime)
    calendar_event_id = Column(String(500))  # Exchange/Graph/Google event ID
    is_sol = Column(Boolean, default=False)
    source_doc_id = Column(BigInteger, ForeignKey("documents.id"))
    priority = Column(
        Enum("critical", "high", "normal", name="deadline_priority_enum"),
        default="normal",
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_tenant_date", "tenant_id", "deadline_date"),
    )
