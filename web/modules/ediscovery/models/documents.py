"""EdiscoveryDocument model — one row per ingested file."""

import enum
from datetime import datetime
from sqlalchemy import (
    Column, BigInteger, String, Text, Boolean, Date, DateTime,
    Enum, DECIMAL, JSON, ForeignKey, Index, func,
)
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import relationship
from core.db.base import Base


class ReviewTier(str, enum.Enum):
    hot = "hot"
    warm = "warm"
    cold = "cold"
    junk = "junk"
    unscored = "unscored"


class ReviewStatus(str, enum.Enum):
    unreviewed = "unreviewed"
    reviewed = "reviewed"
    qc_reviewed = "qc_reviewed"


class EdiscoveryDocument(Base):
    """
    One row per file ingested into an eDiscovery collection.

    file_path:      Relative path within originals/unpacked/ — the immutable
                    preserved copy. Joined with collection.storage_path to get
                    absolute path.  e.g. "originals/unpacked/email_001.msg"

    original_path:  Path within originals/unpacked/ — same as file_path.
                    Kept explicit for clarity: this is the forensic original.

    working_path:   Path to extracted/OCR'd text in working/ directory.
                    e.g. "working/email_001.txt"

    dms_document_id: Optional FK to DMS documents table. Set when file was
                     ingested from a DMS source path and matched to an
                     existing DMS record. Enables cross-navigation between
                     DMS and eDiscovery without physical duplication.
    """
    __tablename__ = "ediscovery_documents"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id = Column(String(36), nullable=False)
    collection_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("ediscovery_collections.id"),
        nullable=False,
    )

    # --- File identity ---
    file_path = Column(String(2000), nullable=False)  # relative to collection storage_path (IMAGE for productions)
    original_path = Column(String(2000))  # within originals/unpacked/
    working_path = Column(String(2000))  # within working/ — extracted text path
    native_path = Column(String(2000))   # relative path to native file (MP3, DOCX, MSG, etc.)
    text_path = Column(String(2000))     # relative path to companion .txt (Relativity TEXT/)
    file_name = Column(String(500))
    file_size = Column(BIGINT(unsigned=True))
    file_hash = Column(String(64), nullable=False)  # SHA-256
    mime_type = Column(String(100))
    doc_type = Column(String(100))  # pdf, email, word, image, spreadsheet, etc.

    # --- DMS cross-reference ---
    dms_document_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("documents.id"),
        nullable=True,
    )

    # --- Custodian and metadata ---
    custodian = Column(String(255))
    doc_date = Column(Date)
    extracted_text = Column(Text)  # full extracted text for search/scoring
    page_count = Column(BIGINT(unsigned=True))

    # --- Email-specific metadata ---
    email_from = Column(String(500))
    email_to = Column(Text)  # may be long, comma-separated
    email_cc = Column(Text)
    email_subject = Column(String(1000))
    email_date = Column(DateTime())
    email_thread_id = Column(String(255))
    email_message_id = Column(String(500))
    email_in_reply_to = Column(String(500))
    email_references = Column(Text)

    # --- Deduplication ---
    is_duplicate = Column(Boolean, default=False)
    dupe_of_id = Column(BIGINT(unsigned=True), ForeignKey("ediscovery_documents.id"))
    is_near_duplicate = Column(Boolean, default=False)
    near_dupe_of_id = Column(BIGINT(unsigned=True), ForeignKey("ediscovery_documents.id"))
    near_dupe_score = Column(DECIMAL(5, 4))  # cosine similarity score

    # --- Embedding vector (stored as JSON array of floats) ---
    embedding = Column(JSON)

    # --- Scoring and review ---
    relevance_score = Column(DECIMAL(5, 4))  # 0.0000 to 1.0000
    relevance_breakdown = Column(JSON)  # per-element scores
    review_tier = Column(
        String(50),
        nullable=False,
        default="unscored",
    )
    review_status = Column(
        String(50),
        nullable=False,
        default="unreviewed",
    )
    coding = Column(JSON)  # responsive, privileged, issue tags, confidentiality
    coding_notes = Column(Text)
    reviewed_by = Column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_at = Column(DateTime())

    # --- Production ---
    bates_begin = Column(String(50))
    bates_end = Column(String(50))
    produced_in = Column(String(100))  # production set name

    # --- Timestamps ---
    ingested_at = Column(DateTime(), nullable=False, default=func.now())
    created_at = Column(DateTime(), nullable=False, default=func.now())

    # Relationships
    collection = relationship("EdiscoveryCollection", back_populates="documents")
    dms_document = relationship("Document", foreign_keys=[dms_document_id])
    reviewed_by_user = relationship("User", foreign_keys=[reviewed_by])
    duplicate_of = relationship(
        "EdiscoveryDocument",
        remote_side="EdiscoveryDocument.id",
        foreign_keys=[dupe_of_id],
    )

    __table_args__ = (
        Index("idx_tenant_collection", "tenant_id", "collection_id"),
        Index("idx_tenant_tier", "tenant_id", "review_tier"),
        Index("idx_hash", "file_hash"),
        Index("idx_tenant_thread", "tenant_id", "email_thread_id"),
        Index("idx_tenant_review_status", "tenant_id", "review_status"),
        {"extend_existing": True},
    )
