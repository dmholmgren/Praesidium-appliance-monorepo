"""
Matter and MatterTimekeeper ORM models.
"""

from datetime import datetime, date

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)

from core.db.base import Base


class Matter(Base):
    __tablename__ = "matters"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    client_id = Column(BigInteger, ForeignKey("clients.id"), nullable=False)
    matter_number = Column(String(50), nullable=False)
    matter_name = Column(String(500), nullable=False)
    matter_type = Column(
        Enum("litigation", "transactional", "regulatory", "advisory",
             "bankruptcy", "immigration", "family", "criminal",
             "estate_planning", "real_estate", "ip", "employment",
             "general", name="matter_type_enum"),
        nullable=False,
        default="general",
    )
    status = Column(
        Enum("active", "closed", "inactive", name="matter_status_enum"),
        default="active",
    )
    responsible_attorney_id = Column(BigInteger, ForeignKey("users.id"))
    originating_attorney_id = Column(BigInteger, ForeignKey("users.id"))
    billing_type = Column(
        Enum("hourly", "flat_fee", "contingency", "retainer", name="billing_type_enum"),
        nullable=False,
    )
    hourly_rate = Column(Numeric(10, 2))
    flat_fee_amount = Column(Numeric(10, 2))
    contingency_pct = Column(Numeric(5, 2))
    court = Column(String(255))
    cause_number = Column(String(100))
    jurisdiction = Column(String(100))
    judge = Column(String(255))
    sol_date = Column(Date)
    open_date = Column(Date, nullable=False)
    close_date = Column(Date)
    folder_path = Column(String(1000))
    legacy_id = Column(String(100))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant", "tenant_id"),
        Index("idx_tenant_client", "tenant_id", "client_id"),
        Index("idx_tenant_status", "tenant_id", "status"),
        Index("idx_tenant_number", "tenant_id", "matter_number", unique=True),
    )


class MatterTimekeeper(Base):
    __tablename__ = "matter_timekeepers"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    matter_id = Column(BigInteger, ForeignKey("matters.id"), nullable=False)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    role = Column(String(50))  # lead, supporting, paralegal
    rate_override = Column(Numeric(10, 2))  # NULL = use user default rate
    assigned_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant_matter", "tenant_id", "matter_id"),
        Index("idx_tenant_user", "tenant_id", "user_id"),
    )
