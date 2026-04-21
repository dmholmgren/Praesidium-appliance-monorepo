"""
AuditLog ORM model.

Every write operation to the database is logged here.
Immutable — no UPDATE or DELETE privilege for the application user.
Append-only with INSERT privilege only.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    String,
    Text,
    JSON,
)

from core.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    user_id = Column(BigInteger)
    action = Column(String(50), nullable=False)  # create, update, delete
    table_name = Column(String(100), nullable=False)
    record_id = Column(String(100), nullable=False)
    old_values = Column(JSON)
    new_values = Column(JSON)
    ip_address = Column(String(45))
    user_agent = Column(String(500))
    request_id = Column(String(36))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant", "tenant_id"),
        Index("idx_tenant_table", "tenant_id", "table_name"),
        Index("idx_tenant_record", "tenant_id", "table_name", "record_id"),
        Index("idx_tenant_user", "tenant_id", "user_id"),
        Index("idx_created", "created_at"),
    )
