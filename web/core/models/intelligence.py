"""
Intelligence / Learning Layer ORM models.

Tables: learning_signals, learned_preferences, ai_api_calls
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    JSON,
)

from core.db.base import Base


class LearningSignal(Base):
    __tablename__ = "learning_signals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    user_id = Column(BigInteger, nullable=False)
    signal_type = Column(String(100), nullable=False)
    # time_edit, doc_code_change, research_accept, research_reject,
    # billing_approve, billing_modify, deadline_override, etc.
    module = Column(String(50), nullable=False)  # billing, dms, court, etc.
    entity_type = Column(String(50))  # time_entry, document, deadline, etc.
    entity_id = Column(BigInteger)
    old_value = Column(JSON)
    new_value = Column(JSON)
    context = Column(JSON)  # matter_type, jurisdiction, judge, etc.
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant", "tenant_id"),
        Index("idx_tenant_type", "tenant_id", "signal_type"),
        Index("idx_tenant_user", "tenant_id", "user_id"),
        Index("idx_tenant_module", "tenant_id", "module"),
    )


class LearnedPreference(Base):
    __tablename__ = "learned_preferences"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    user_id = Column(BigInteger)  # NULL = firm-wide preference
    preference_type = Column(String(100), nullable=False)
    module = Column(String(50), nullable=False)
    rule = Column(JSON, nullable=False)  # structured rule definition
    confidence = Column(Numeric(3, 2), nullable=False)  # 0.00 to 1.00
    sample_count = Column(Integer, nullable=False, default=0)
    is_active = Column(String(1), nullable=False, default="Y")
    last_derived_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant", "tenant_id"),
        Index("idx_tenant_type", "tenant_id", "preference_type"),
        Index("idx_tenant_user", "tenant_id", "user_id"),
    )


class AIApiCall(Base):
    __tablename__ = "ai_api_calls"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    user_id = Column(BigInteger)
    provider = Column(String(50), nullable=False)  # anthropic, openai, etc.
    model = Column(String(100), nullable=False)
    module = Column(String(50), nullable=False)
    purpose = Column(String(255), nullable=False)  # time_description, doc_summary, etc.
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    total_tokens = Column(Integer)
    cost_usd = Column(Numeric(8, 6))
    latency_ms = Column(Integer)
    status = Column(String(20), nullable=False)  # success, error, timeout
    error_message = Column(Text)
    request_metadata = Column(JSON)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_tenant", "tenant_id"),
        Index("idx_tenant_module", "tenant_id", "module"),
        Index("idx_tenant_provider", "tenant_id", "provider"),
        Index("idx_created", "created_at"),
    )
