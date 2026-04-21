"""
Contact and MatterContact ORM models.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
)

from core.db.base import Base


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    full_name = Column(String(500), nullable=False)
    company = Column(String(500))
    contact_type = Column(
        Enum("client", "opposing_counsel", "judge", "expert", "vendor",
             "court_clerk", "witness", "other", name="contact_type_enum"),
    )
    email = Column(String(255))
    phone = Column(String(50))
    address1 = Column(String(255))
    city = Column(String(100))
    state = Column(String(50))
    bar_number = Column(String(100))
    firm_name = Column(String(500))
    notes = Column(Text)
    external_id = Column(String(255))  # Exchange contact ID or Google contact ID
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant", "tenant_id"),
        Index("idx_tenant_type", "tenant_id", "contact_type"),
        Index("idx_tenant_email", "tenant_id", "email"),
    )


class MatterContact(Base):
    __tablename__ = "matter_contacts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    matter_id = Column(BigInteger, ForeignKey("matters.id"), nullable=False)
    contact_id = Column(BigInteger, ForeignKey("contacts.id"), nullable=False)
    role = Column(String(100))  # plaintiff, defendant, expert witness, etc.
    is_primary = Column(String(1), default="N")
    notes = Column(Text)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_tenant_contact", "tenant_id", "contact_id"),
    )
