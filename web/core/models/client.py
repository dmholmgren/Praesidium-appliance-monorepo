"""
Client ORM model.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    Index,
    String,
    Text,
)

from core.db.base import Base


class Client(Base):
    __tablename__ = "clients"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    client_number = Column(String(50), nullable=False)
    client_name = Column(String(500), nullable=False)
    client_type = Column(
        Enum("individual", "corporation", "llc", "partnership", "government",
             "nonprofit", "trust", "estate", "other", name="client_type_enum"),
        default="individual",
    )
    primary_contact = Column(String(500))
    email = Column(String(255))
    phone = Column(String(50))
    address1 = Column(String(255))
    address2 = Column(String(255))
    city = Column(String(100))
    state = Column(String(50))
    zip_code = Column(String(20))
    country = Column(String(100), default="US")
    tax_id = Column(String(50))
    notes = Column(Text)
    is_active = Column(Boolean, nullable=False, default=True)
    legacy_id = Column(String(100))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant", "tenant_id"),
        Index("idx_tenant_number", "tenant_id", "client_number", unique=True),
        Index("idx_tenant_name", "tenant_id", "client_name"),
    )
