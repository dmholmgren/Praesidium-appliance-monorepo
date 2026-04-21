from datetime import datetime
from sqlalchemy import Column, BigInteger, String, Text, DateTime, Boolean, Integer
from core.db.base import Base

class ReportDefinition(Base):
    __tablename__ = "report_definitions"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), nullable=False, index=True)
    description = Column(Text, nullable=True)
    category = Column(String(100), nullable=False)
    sql_template = Column(Text, nullable=False)
    default_filters = Column(Text, nullable=True)
    column_definitions = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    is_system = Column(Boolean, nullable=False, default=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
