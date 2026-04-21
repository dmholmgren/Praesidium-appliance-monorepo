from datetime import datetime
from sqlalchemy import Column, BigInteger, String, Text, DateTime, Enum, Boolean, Integer
from core.db.base import Base

class QBOSyncLog(Base):
    __tablename__ = "qbo_sync_log"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    object_type = Column(String(50), nullable=False)
    object_id = Column(BigInteger, nullable=False, index=True)
    direction = Column(Enum("push","pull", name="sync_direction_enum"), nullable=False)
    qbo_id = Column(String(100), nullable=True)
    status = Column(Enum("pending","success","error","retrying", name="sync_status_enum"), nullable=False, default="pending")
    request_payload = Column(Text, nullable=True)
    response_payload = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

class QBOMapping(Base):
    __tablename__ = "qbo_mappings"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    mapping_type = Column(String(50), nullable=False)
    platform_code = Column(String(100), nullable=False)
    platform_label = Column(String(255), nullable=True)
    qbo_id = Column(String(100), nullable=False)
    qbo_name = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
