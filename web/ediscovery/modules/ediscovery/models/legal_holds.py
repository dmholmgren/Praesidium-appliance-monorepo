"""Legal hold and custodian tracking models."""

import enum
from sqlalchemy import (
    Column, String, Text, Boolean, Date, DateTime,
    Enum, ForeignKey, Index, func,
)
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.orm import relationship
from core.database import Base


class HoldStatus(str, enum.Enum):
    active = "active"
    modified = "modified"
    released = "released"


class AcknowledgmentStatus(str, enum.Enum):
    pending = "pending"
    acknowledged = "acknowledged"
    reminded = "reminded"
    escalated = "escalated"


class LegalHold(Base):
    """Litigation hold for a matter — tracks scope, custodians, and timeline."""
    __tablename__ = "legal_holds"

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    matter_id = Column(BIGINT(unsigned=True), ForeignKey("matters.id"), nullable=False)
    hold_name = Column(String(255), nullable=False)
    hold_scope = Column(Text, nullable=False)  # what must be preserved
    status = Column(Enum(HoldStatus), nullable=False, default=HoldStatus.active)
    issued_date = Column(Date, nullable=False)
    issued_by = Column(BIGINT(unsigned=True), ForeignKey("users.id"), nullable=False)
    modified_date = Column(Date)
    modified_reason = Column(Text)
    released_date = Column(Date)
    released_by = Column(BIGINT(unsigned=True), ForeignKey("users.id"))
    released_reason = Column(Text)
    notice_document_path = Column(String(2000))  # path to generated hold notice
    created_at = Column(DateTime(fsp=3), nullable=False, default=func.now())

    custodians = relationship("HoldCustodian", back_populates="hold")

    __table_args__ = (
        Index("idx_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_tenant_status", "tenant_id", "status"),
    )


class HoldCustodian(Base):
    """Individual custodian on a legal hold — acknowledgment tracking."""
    __tablename__ = "hold_custodians"

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    hold_id = Column(BIGINT(unsigned=True), ForeignKey("legal_holds.id"), nullable=False)
    custodian_name = Column(String(255), nullable=False)
    custodian_email = Column(String(255))
    custodian_role = Column(String(255))  # "Employee", "Former Employee", etc.
    status = Column(
        Enum(AcknowledgmentStatus),
        nullable=False,
        default=AcknowledgmentStatus.pending,
    )
    notified_at = Column(DateTime(fsp=3))
    acknowledged_at = Column(DateTime(fsp=3))
    last_reminder_at = Column(DateTime(fsp=3))
    reminder_count = Column(BIGINT(unsigned=True), default=0)
    escalated_at = Column(DateTime(fsp=3))
    escalated_to = Column(BIGINT(unsigned=True), ForeignKey("users.id"))
    created_at = Column(DateTime(fsp=3), nullable=False, default=func.now())

    hold = relationship("LegalHold", back_populates="custodians")

    __table_args__ = (
        Index("idx_tenant_hold", "tenant_id", "hold_id"),
        Index("idx_tenant_status", "tenant_id", "status"),
    )
