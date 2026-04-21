"""ESI protocol tracking."""

from sqlalchemy import (
    Column, String, Text, DateTime, ForeignKey, Index, JSON, func,
)
from sqlalchemy.dialects.mysql import BIGINT
from core.database import Base


class EsiProtocol(Base):
    """Generated ESI protocol for a matter — tracks versions and drafts."""
    __tablename__ = "esi_protocols"

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    matter_id = Column(BIGINT(unsigned=True), ForeignKey("matters.id"), nullable=False)
    collection_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("ediscovery_collections.id"),
    )
    version = Column(BIGINT(unsigned=True), nullable=False, default=1)
    version_label = Column(String(255))
    protocol_data = Column(JSON)  # structured protocol content
    document_path = Column(String(2000))  # path to generated .docx
    notes = Column(Text)
    created_at = Column(DateTime(fsp=3), nullable=False, default=func.now())
    created_by = Column(BIGINT(unsigned=True), ForeignKey("users.id"))

    __table_args__ = (
        Index("idx_tenant_matter", "tenant_id", "matter_id"),
    )
