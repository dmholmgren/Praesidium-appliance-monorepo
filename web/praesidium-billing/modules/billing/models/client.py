from datetime import datetime
from sqlalchemy import Column, BigInteger, String, Text, Boolean, DateTime, Enum
from sqlalchemy.orm import relationship
from core.db.base import Base

class Client(Base):
    __tablename__ = "clients"
    __table_args__ = {"extend_existing": True}
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    client_number = Column(String(50), nullable=False)
    client_name = Column(String(500), nullable=False)
    client_type = Column(Enum("individual","corporation","llc","partnership","government","nonprofit","trust","estate","other", name="client_type_enum"), nullable=True, default="individual")
    primary_contact = Column(String(500), nullable=True)
    email = Column(String(255), nullable=True)
    phone = Column(String(50), nullable=True)
    address1 = Column(String(255), nullable=True)
    address2 = Column(String(255), nullable=True)
    city = Column(String(100), nullable=True)
    state = Column(String(50), nullable=True)
    zip_code = Column(String(20), nullable=True)
    country = Column(String(100), nullable=True, default="US")
    tax_id = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    legacy_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    matters = relationship("Matter", back_populates="client", lazy="dynamic")
    trust_ledger = relationship("TrustLedger", back_populates="client", uselist=False)
