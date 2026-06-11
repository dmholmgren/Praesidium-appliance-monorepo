"""EdiscoveryCollection model with source tracking for chain of custody."""

import enum
from datetime import datetime
from sqlalchemy import (
    Column, BigInteger, String, Text, Enum, Date, DateTime,
    JSON, ForeignKey, Index, func,
)
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import relationship
from core.db.base import Base


class CollectionStatus(str, enum.Enum):
    collecting = "collecting"
    processing = "processing"
    review_ready = "review_ready"
    review_complete = "review_complete"
    produced = "produced"


class SourceType(str, enum.Enum):
    client_collection_dms = "client_collection_dms"
    client_collection_dedicated = "client_collection_dedicated"
    opposing_production = "opposing_production"
    internal_collection = "internal_collection"
    third_party_subpoena = "third_party_subpoena"
    client_documents = "client_documents"


class EdiscoveryCollection(Base):
    """
    Tracks an eDiscovery document collection for a matter.

    Storage layout per collection:
        /{ediscovery_storage}/{tenant_id}/{id}/
            originals/
                as_received/    <- exact archive as delivered, hashed
                unpacked/       <- individual files extracted, read-only
            working/            <- OCR text, embeddings, extracted content
            productions/        <- outbound production sets

    All source types get the originals/ preservation treatment.
    For client_collection_dms, files are copied from DMS to originals/
    before ingestion begins.
    """
    __tablename__ = "ediscovery_collections"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id = Column(String(36), nullable=False)
    matter_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("matters.id"),
        nullable=False,
    )
    collection_name = Column(String(255), nullable=False)
    storage_path = Column(String(2000))  # dedicated eDiscovery storage path

    # --- Source tracking (chain of custody) ---
    source_party = Column(String(500))
    source_type = Column(
        String(100),
        nullable=False,
        default="client_collection_dms",
    )
    received_date = Column(Date)
    received_method = Column(String(255))  # "USB drive", "Relativity transfer", etc.
    received_by = Column(BIGINT(unsigned=True), ForeignKey("users.id"))
    original_hash = Column(String(64))  # SHA-256 of original archive
    original_file_name = Column(String(500))  # "Smith_Jones_Production_001.zip"
    stated_bates_range = Column(String(255))  # "SJ000001 - SJ045832"
    dms_source_path = Column(String(2000))  # where on DMS file share these came from

    # --- Collection stats ---
    total_docs = Column(BIGINT(unsigned=True), default=0)
    processed_docs = Column(BIGINT(unsigned=True), default=0)
    reviewed_docs = Column(BIGINT(unsigned=True), default=0)
    status = Column(
        String(50),
        nullable=False,
        default="collecting",
    )

    # --- Issue map (TAR seed from pleading analysis) ---
    issue_map = Column(JSON)

    created_at = Column(DateTime(), nullable=False, default=func.now())
    updated_at = Column(DateTime(), default=func.now(), onupdate=func.now())

    # Relationships
    documents = relationship("EdiscoveryDocument", back_populates="collection")
    received_by_user = relationship("User", foreign_keys=[received_by])

    __table_args__ = (
        Index("idx_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_tenant_status", "tenant_id", "status"),
        {"extend_existing": True},
    )

    def originals_as_received_path(self) -> str:
        """Path to the exact archive/container as delivered."""
        return f"{self.storage_path}/originals/as_received"

    def originals_unpacked_path(self) -> str:
        """Path to individual files extracted from the original, read-only."""
        return f"{self.storage_path}/originals/unpacked"

    def working_path(self) -> str:
        """Path to OCR text, embeddings, and extracted content."""
        return f"{self.storage_path}/working"

    def productions_path(self) -> str:
        """Path to outbound production sets."""
        return f"{self.storage_path}/productions"
