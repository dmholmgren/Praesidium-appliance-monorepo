"""Production tracking — outbound document productions."""

import enum
from sqlalchemy import (
    Column, String, Text, DateTime, Enum, ForeignKey, Index, JSON, func,
)
from sqlalchemy.dialects.mysql import BIGINT
from core.db.base import Base


class ProductionStatus(str, enum.Enum):
    preparing = "preparing"
    qc_review = "qc_review"
    produced = "produced"


class ProductionFormat(str, enum.Enum):
    relativity_dat = "relativity_dat"
    native = "native"
    tiff = "tiff"
    pdf = "pdf"


class Production(Base):
    """Tracks an outbound production — bates range, format, recipient."""
    __tablename__ = "productions"

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    collection_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("ediscovery_collections.id"),
        nullable=False,
    )
    production_name = Column(String(255), nullable=False)
    produced_to = Column(String(500), nullable=False)  # recipient party
    production_date = Column(DateTime())
    format = Column(Enum(ProductionFormat), nullable=False, default=ProductionFormat.relativity_dat)
    bates_prefix = Column(String(20), nullable=False)
    bates_start = Column(BIGINT(unsigned=True), nullable=False)
    bates_end = Column(BIGINT(unsigned=True))
    total_documents = Column(BIGINT(unsigned=True), default=0)
    total_pages = Column(BIGINT(unsigned=True), default=0)
    output_path = Column(String(2000))  # path to production output
    output_hash = Column(String(64))  # SHA-256 of production archive
    status = Column(Enum(ProductionStatus), nullable=False, default=ProductionStatus.preparing)
    dat_file_path = Column(String(2000))
    opt_file_path = Column(String(2000))
    notes = Column(Text)
    production_metadata = Column("metadata", JSON)  # field mapping, confidentiality designations, etc.
    created_at = Column(DateTime(), nullable=False, default=func.now())
    created_by = Column(BIGINT(unsigned=True), ForeignKey("users.id"))

    __table_args__ = (
        Index("idx_tenant_collection", "tenant_id", "collection_id"),
    )
