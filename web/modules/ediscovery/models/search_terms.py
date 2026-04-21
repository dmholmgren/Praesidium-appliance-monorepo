"""Search term generation and negotiation tracking."""

import enum
from sqlalchemy import (
    Column, String, Text, Integer, DateTime, Enum,
    ForeignKey, Index, JSON, func,
)
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.orm import relationship
from core.db.base import Base


class TermTier(str, enum.Enum):
    tier_1 = "tier_1"  # exact names, account numbers, project codes
    tier_2 = "tier_2"  # role descriptions, divisions, transaction types
    tier_3 = "tier_3"  # general legal concepts


class SearchTermSet(Base):
    """Versioned set of search terms — tracks negotiation history."""
    __tablename__ = "search_term_sets"

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    collection_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("ediscovery_collections.id"),
        nullable=False,
    )
    version = Column(Integer, nullable=False, default=1)
    version_label = Column(String(255))  # "Initial", "After meet and confer", etc.
    author = Column(String(255))  # "Firm" or "Opposing - Smith & Jones"
    notes = Column(Text)
    # Multi-platform formatted outputs
    format_o365_kql = Column(Text)
    format_gmail_vault = Column(Text)
    format_relativity = Column(Text)
    format_generic_boolean = Column(Text)
    hit_count_results = Column(JSON)  # per-term hit counts after running
    created_at = Column(DateTime(), nullable=False, default=func.now())
    created_by = Column(BIGINT(unsigned=True), ForeignKey("users.id"))

    terms = relationship("SearchTerm", back_populates="term_set")

    __table_args__ = (
        Index("idx_tenant_collection", "tenant_id", "collection_id"),
    )


class SearchTerm(Base):
    """Individual search term within a set."""
    __tablename__ = "search_terms"

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    term_set_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("search_term_sets.id"),
        nullable=False,
    )
    tier = Column(Enum(TermTier), nullable=False)
    issue_element = Column(String(500))  # which issue map element this targets
    term_text = Column(Text, nullable=False)  # the actual Boolean expression
    hit_count = Column(BIGINT(unsigned=True))
    notes = Column(Text)
    created_at = Column(DateTime(), nullable=False, default=func.now())

    term_set = relationship("SearchTermSet", back_populates="terms")

    __table_args__ = (
        Index("idx_tenant_termset", "tenant_id", "term_set_id"),
    )
