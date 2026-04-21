from datetime import datetime, date
from sqlalchemy import Column, BigInteger, String, Text, DateTime, Date, Enum, ForeignKey, Numeric, Boolean
from core.db.base import Base

class RateCard(Base):
    __tablename__ = "rate_cards"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    scope = Column(Enum("firm","timekeeper","client","matter", name="rate_scope_enum"), nullable=False)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    client_id = Column(BigInteger, ForeignKey("clients.id"), nullable=True)
    matter_id = Column(BigInteger, ForeignKey("matters.id"), nullable=True)
    hourly_rate = Column(Numeric(10,2), nullable=False)
    effective_date = Column(Date, nullable=False, default=date.today)
    end_date = Column(Date, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

class RateChangeLog(Base):
    __tablename__ = "rate_change_log"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    rate_card_id = Column(BigInteger, ForeignKey("rate_cards.id"), nullable=False)
    old_rate = Column(Numeric(10,2), nullable=False)
    new_rate = Column(Numeric(10,2), nullable=False)
    effective_date = Column(Date, nullable=False)
    scope = Column(String(50), nullable=False)
    changed_by_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    reason = Column(Text, nullable=True)
    apply_to_unbilled_wip = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
