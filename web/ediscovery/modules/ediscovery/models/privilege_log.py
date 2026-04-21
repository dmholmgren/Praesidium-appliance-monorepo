"""Privilege log entry model — auto-generated from review coding."""

import enum
from sqlalchemy import (
    Column, String, Text, Date, DateTime, Enum, ForeignKey, Index, func,
)
from sqlalchemy.dialects.mysql import BIGINT
from core.database import Base


class PrivilegeBasis(str, enum.Enum):
    attorney_client = "attorney_client"
    work_product = "work_product"
    joint_defense = "joint_defense"
    common_interest = "common_interest"
    other = "other"


class PrivilegeLogEntry(Base):
    """One row per privileged document — standard privilege log format."""
    __tablename__ = "privilege_log_entries"

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    collection_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("ediscovery_collections.id"),
        nullable=False,
    )
    document_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("ediscovery_documents.id"),
        nullable=False,
    )
    log_number = Column(BIGINT(unsigned=True))
    doc_date = Column(Date)
    author = Column(String(500))
    recipients = Column(Text)  # to + cc combined
    description = Column(Text, nullable=False)
    privilege_basis = Column(Enum(PrivilegeBasis), nullable=False)
    privilege_detail = Column(Text)  # additional explanation if needed
    bates_begin = Column(String(50))
    bates_end = Column(String(50))
    created_at = Column(DateTime(fsp=3), nullable=False, default=func.now())
    created_by = Column(BIGINT(unsigned=True), ForeignKey("users.id"))

    __table_args__ = (
        Index("idx_tenant_collection", "tenant_id", "collection_id"),
        Index("idx_tenant_document", "tenant_id", "document_id"),
    )
