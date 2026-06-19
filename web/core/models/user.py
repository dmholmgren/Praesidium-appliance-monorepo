"""
User ORM model.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    Index,
    Numeric,
    String,
    Text,
)

from core.db.base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)  # FIRST non-PK column — always
    username = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False)
    full_name = Column(String(500), nullable=False)
    password_hash = Column(String(255))
    role = Column(
        Enum("super_admin", "admin", "attorney", "paralegal", "staff", "read_only",
             "partner", "client", "deal_room_guest", "co_counsel",
             name="user_role_enum"),
        nullable=False,
        default="staff",
    )
    bar_number = Column(String(100))
    jurisdiction = Column(String(100))
    default_hourly_rate = Column(Numeric(10, 2))
    is_active = Column(Boolean, nullable=False, default=True)
    is_timekeeper = Column(Boolean, nullable=False, default=False)
    auth_provider = Column(String(50), default="local")  # local, ldaps, azure_ad, google, okta, saml
    external_id = Column(String(255))  # AD objectGUID, Azure OID, Google sub
    last_login = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant", "tenant_id"),
        Index("idx_tenant_username", "tenant_id", "username", unique=True),
        Index("idx_tenant_email", "tenant_id", "email"),
    )
