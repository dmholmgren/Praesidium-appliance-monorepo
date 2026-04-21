"""
DMS (Document Management System) module ORM models.

Tables: documents, document_time_tracking
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)

from core.db.base import Base


class Document(Base):
    __tablename__ = "documents"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    matter_id = Column(BigInteger, ForeignKey("matters.id"), nullable=False)
    doc_type = Column(String(100), nullable=False)  # pleading, correspondence, discovery, etc.
    title = Column(String(500), nullable=False)
    storage_path = Column(String(2000), nullable=False)  # StorageService canonical path
    file_name = Column(String(500), nullable=False)
    file_size = Column(BigInteger)
    mime_type = Column(String(255))
    version_number = Column(Integer, default=1)
    parent_doc_id = Column(BigInteger)  # NULL for version 1
    checksum = Column(String(64))  # SHA-256
    ocr_text = Column(Text)  # extracted text
    ocr_status = Column(
        Enum("pending", "processing", "complete", "failed", name="ocr_status_enum"),
        default="pending",
    )
    indexed_at = Column(DateTime)
    created_by = Column(BigInteger, ForeignKey("users.id"))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_tenant_type", "tenant_id", "doc_type"),
    )


class DocumentTimeTracking(Base):
    __tablename__ = "document_time_tracking"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    document_id = Column(BigInteger, ForeignKey("documents.id"), nullable=False)
    matter_id = Column(BigInteger, ForeignKey("matters.id"), nullable=False)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    session_start = Column(DateTime, nullable=False)
    session_end = Column(DateTime)
    duration_seconds = Column(Integer)
    activity_type = Column(
        Enum("viewing", "editing", name="doc_activity_type_enum"),
        nullable=False,
    )
    keystrokes = Column(Integer, default=0)
    draft_entry_id = Column(BigInteger)  # linked time_entry if created

    __table_args__ = (
        Index("idx_tenant_doc", "tenant_id", "document_id"),
        Index("idx_tenant_user", "tenant_id", "user_id"),
    )
