"""
eDiscovery module ORM models.

Tables: ediscovery_collections, ediscovery_documents
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    JSON,
)

from core.db.base import Base


class EDiscoveryCollection(Base):
    __tablename__ = "ediscovery_collections"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    matter_id = Column(BigInteger, ForeignKey("matters.id"), nullable=False)
    collection_name = Column(String(255), nullable=False)
    storage_path = Column(String(2000))
    total_docs = Column(BigInteger, default=0)
    processed_docs = Column(BigInteger, default=0)
    reviewed_docs = Column(BigInteger, default=0)
    status = Column(
        Enum("collecting", "processing", "review_ready", "review_complete", "produced",
             name="ediscovery_status_enum"),
        default="collecting",
    )
    issue_map = Column(JSON)  # extracted from pleadings
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant_matter", "tenant_id", "matter_id"),
    )


class EDiscoveryDocument(Base):
    __tablename__ = "ediscovery_documents"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    collection_id = Column(BigInteger, ForeignKey("ediscovery_collections.id"), nullable=False)
    doc_hash = Column(String(64), nullable=False)  # SHA-256
    file_name = Column(String(500), nullable=False)
    file_path = Column(String(2000), nullable=False)
    file_size = Column(BigInteger)
    mime_type = Column(String(255))
    extracted_text = Column(Text)
    email_subject = Column(String(1000))
    email_from = Column(String(500))
    email_to = Column(Text)
    email_date = Column(DateTime)
    email_thread_id = Column(String(255))
    is_duplicate = Column(Boolean, default=False)
    duplicate_of_id = Column(BigInteger)
    tar_score = Column(Numeric(5, 4))  # 0.0000 to 1.0000
    review_status = Column(
        Enum("unreviewed", "responsive", "non_responsive", "privileged",
             "hot", "needs_redaction", name="ediscovery_review_enum"),
        default="unreviewed",
    )
    reviewed_by = Column(BigInteger, ForeignKey("users.id"))
    reviewed_at = Column(DateTime)
    bates_start = Column(String(50))
    bates_end = Column(String(50))
    produced_in = Column(String(255))  # production set name
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant_collection", "tenant_id", "collection_id"),
        Index("idx_tenant_hash", "tenant_id", "doc_hash"),
        Index("idx_tenant_review", "tenant_id", "review_status"),
    )
