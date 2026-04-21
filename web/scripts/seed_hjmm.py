"""
Seed script for HJMM test bed.

Creates the HJMM tenant and branding configuration.
This is CONFIG, not code — HJMM-specific values live here only.

Run: python3 scripts/seed_hjmm.py
"""

import os
import sys
import uuid
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.base import Base
from core.models.tenant import Tenant, TenantBranding
from core.models.user import User


def seed():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable required")
        sys.exit(1)

    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Check if HJMM tenant already exists
        existing = session.query(Tenant).filter(Tenant.slug == "hjmm").first()
        if existing:
            print(f"HJMM tenant already exists: {existing.id}")
            return

        # Create HJMM tenant
        tenant_id = str(uuid.uuid4())
        tenant = Tenant(
            id=tenant_id,
            name="Holmgren Johnson Mitchell Madden, LLP",
            slug="hjmm",
            domain="hjmmlegal.com",
            tier="C",  # Dedicated deployment
            is_active=True,
            storage_adapter="cifs",
            auth_adapter="ldaps",
            email_adapter="exchange_ews",
            calendar_adapter="exchange_ews",
            communication_adapter="freepbx",
            ai_provider="anthropic",
            research_adapter="none",
            accounting_adapter="none",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(tenant)

        # Create HJMM branding
        branding = TenantBranding(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            platform_name="HJMM Legal",
            platform_short_name="HJMM",
            tagline="Holmgren Johnson Mitchell Madden, LLP",
            suppress_attribution=True,  # HJMM does not show "Powered by Praesidium"
            base_domain="hjmmlegal.com",
            use_praesidium_subdomain=False,
            css_vars={
                "primary": "#1B365D",
                "secondary": "#2C5282",
                "accent": "#C5A55A",
                "bg": "#FFFFFF",
                "text": "#1A202C",
                "heading_font": "Georgia, serif",
                "body_font": "Inter, sans-serif",
            },
            email_from_name="HJMM Legal",
            email_from_address="noreply@hjmmlegal.com",
            email_footer_text="Holmgren Johnson Mitchell Madden, LLP | Dallas, Texas",
            pwa_name="HJMM Legal",
            pwa_short_name="HJMM",
            pwa_theme_color="#1B365D",
            doc_footer_text="HJMM Legal | Confidential",
            addin_display_name="HJMM Legal",
            addin_description="HJMM Legal Practice Management",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(branding)

        # Create initial admin user
        admin = User(
            tenant_id=tenant_id,
            username="admin",
            email="admin@hjmmlegal.com",
            full_name="System Administrator",
            password_hash="$2b$12$placeholder_hash_replace_on_first_login",
            role="super_admin",
            is_active=True,
            is_timekeeper=False,
            auth_provider="ldaps",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(admin)

        session.commit()
        print(f"HJMM tenant created: {tenant_id}")
        print(f"  Domain: hjmmlegal.com")
        print(f"  Branding: HJMM Legal")
        print(f"  Admin user: admin@hjmmlegal.com")
        print(f"  Tier: C (Dedicated)")

    except Exception as e:
        session.rollback()
        print(f"ERROR: {e}")
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    seed()
