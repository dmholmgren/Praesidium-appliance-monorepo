"""
Tenant and TenantBranding ORM models.

tenant_branding has NO tenant_id column — it IS the tenant identity table.
Joined to tenants via tenants.id = tenant_branding.tenant_id (FK).
This is Schema Rule 3 from the Master Implementation Guide.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    String,
    Text,
    JSON,
    Index,
)
from sqlalchemy.orm import relationship

from core.db.base import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(String(36), primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), nullable=False, unique=True)
    domain = Column(String(255), nullable=False, unique=True)
    tier = Column(String(1), nullable=False, default="C")  # A=shared, B=isolated, C=dedicated
    is_active = Column(Boolean, nullable=False, default=True)

    # Adapter configuration — read by Chat 9 adapter factory
    storage_adapter = Column(String(50), nullable=False, default="cifs")
    auth_adapter = Column(String(50), nullable=False, default="ldaps")
    email_adapter = Column(String(50), nullable=False, default="exchange_ews")
    calendar_adapter = Column(String(50), nullable=False, default="exchange_ews")
    communication_adapter = Column(String(50), nullable=False, default="freepbx")
    ai_provider = Column(String(50), nullable=False, default="anthropic")
    research_adapter = Column(String(50), nullable=False, default="none")
    accounting_adapter = Column(String(50), nullable=False, default="none")

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    branding = relationship("TenantBranding", back_populates="tenant", uselist=False)

    __table_args__ = (
        Index("idx_tenant_domain", "domain"),
        Index("idx_tenant_slug", "slug"),
    )


class TenantBranding(Base):
    """
    White-label branding for a tenant.
    NO tenant_id column — this table IS the branding identity.
    Joined via tenant_id FK to tenants.id.
    """
    __tablename__ = "tenant_branding"

    id = Column(String(36), primary_key=True)
    tenant_id = Column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # Platform identity
    platform_name = Column(String(255), nullable=False, default="Praesidium")
    platform_short_name = Column(String(50), nullable=False, default="Praesidium")
    tagline = Column(String(500))
    suppress_attribution = Column(Boolean, nullable=False, default=False)

    # Domain
    base_domain = Column(String(255), nullable=False)
    use_praesidium_subdomain = Column(Boolean, default=False)

    # Visual identity
    logo_url = Column(String(1000))
    logo_dark_url = Column(String(1000))
    favicon_url = Column(String(1000))
    css_vars = Column(JSON, nullable=False, default=dict)

    # Email
    email_from_name = Column(String(255), nullable=False, default="Praesidium")
    email_from_address = Column(String(255))
    email_footer_text = Column(Text)
    email_logo_url = Column(String(1000))

    # Mobile PWA
    pwa_name = Column(String(100), nullable=False, default="Praesidium")
    pwa_short_name = Column(String(30), nullable=False, default="Praesidium")
    pwa_theme_color = Column(String(7), default="#0D1F3C")
    pwa_icon_url = Column(String(1000))

    # Document footers
    doc_footer_text = Column(String(500))
    doc_logo_url = Column(String(1000))

    # Office Add-In identity
    addin_display_name = Column(String(100), nullable=False, default="Praesidium")
    addin_description = Column(String(500))

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    tenant = relationship("Tenant", back_populates="branding")

    __table_args__ = (
        Index("idx_branding_tenant", "tenant_id"),
    )
