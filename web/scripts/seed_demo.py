"""
Seed script for Praesidium Demo tenant.

Creates the demo tenant on praesidium-legal.com with Praesidium default branding
and synthetic public data. This tenant runs alongside HJMM in the same database,
isolated by tenant_id.

Run: docker compose exec web python3 scripts/seed_demo.py
"""

import os
import sys
import uuid
from datetime import datetime, date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.base import Base
from core.models.tenant import Tenant, TenantBranding
from core.models.user import User
from core.models.client import Client
from core.models.matter import Matter


def seed():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable required")
        sys.exit(1)

    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Check if demo tenant already exists
        existing = session.query(Tenant).filter(Tenant.slug == "demo").first()
        if existing:
            print(f"Demo tenant already exists: {existing.id}")
            return

        # ─── Tenant ──────────────────────────────────────────────────────
        tenant_id = str(uuid.uuid4())
        tenant = Tenant(
            id=tenant_id,
            name="Praesidium Demo",
            slug="demo",
            domain="praesidium-legal.com",
            tier="A",  # Shared — same DB as HJMM, isolated by tenant_id
            is_active=True,
            storage_adapter="none",       # Demo has no file share
            auth_adapter="native",        # Local username/password — no LDAPS
            email_adapter="none",         # No email integration for demo
            calendar_adapter="none",      # No calendar integration for demo
            communication_adapter="none", # No phone/SMS for demo
            ai_provider="anthropic",      # AI works for demo
            research_adapter="none",      # No legal research for demo
            accounting_adapter="none",    # No accounting for demo
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(tenant)

        # ─── Branding ────────────────────────────────────────────────────
        branding = TenantBranding(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            platform_name="Praesidium Legal",
            platform_short_name="Praesidium",
            tagline="Legal Practice Intelligence Platform",
            suppress_attribution=False,  # Show "Powered by Praesidium"
            base_domain="praesidium-legal.com",
            use_praesidium_subdomain=False,
            css_vars={
                "primary": "#0D1F3C",
                "secondary": "#1A3A5C",
                "accent": "#D4A843",
                "bg": "#FFFFFF",
                "text": "#1A1A1A",
                "heading_font": "Inter, sans-serif",
                "body_font": "Inter, sans-serif",
            },
            email_from_name="Praesidium Legal",
            email_from_address="demo@praesidium-legal.com",
            email_footer_text="Praesidium Legal | Demo Environment",
            pwa_name="Praesidium Legal",
            pwa_short_name="Praesidium",
            pwa_theme_color="#0D1F3C",
            doc_footer_text="Praesidium Legal | Demo",
            addin_display_name="Praesidium Legal",
            addin_description="Legal Practice Intelligence Platform — Demo",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(branding)

        # ─── Demo admin user ─────────────────────────────────────────────
        admin = User(
            tenant_id=tenant_id,
            username="demo",
            email="demo@praesidium-legal.com",
            full_name="Demo User",
            password_hash="$2b$12$placeholder_hash_replace_with_real",
            role="super_admin",
            is_active=True,
            is_timekeeper=True,
            auth_provider="native",  # Local auth — no LDAPS
            default_hourly_rate=350.00,
            bar_number="TX12345678",
            jurisdiction="Texas",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(admin)

        # ─── Demo attorney ───────────────────────────────────────────────
        attorney = User(
            tenant_id=tenant_id,
            username="jsmith",
            email="jsmith@praesidium-legal.com",
            full_name="Jane Smith",
            password_hash="$2b$12$placeholder_hash_replace_with_real",
            role="attorney",
            is_active=True,
            is_timekeeper=True,
            auth_provider="native",
            default_hourly_rate=275.00,
            bar_number="TX87654321",
            jurisdiction="Texas",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(attorney)
        session.flush()  # Get IDs

        # ─── Demo clients ────────────────────────────────────────────────
        clients_data = [
            ("C-001", "Acme Industries, LLC", "corporation"),
            ("C-002", "Summit Capital Partners", "corporation"),
            ("C-003", "Maria Rodriguez", "individual"),
            ("C-004", "Greenfield Properties, Inc.", "corporation"),
            ("C-005", "TechVenture Holdings", "llc"),
        ]
        client_ids = []
        for num, name, ctype in clients_data:
            c = Client(
                tenant_id=tenant_id,
                client_number=num,
                client_name=name,
                client_type=ctype,
                is_active=True,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            session.add(c)
            session.flush()
            client_ids.append(c.id)

        # ─── Demo matters ────────────────────────────────────────────────
        matters_data = [
            (client_ids[0], "M-2024-001", "Acme v. Consolidated Supply", "litigation", "hourly", "E.D. Tex.", "4:24-cv-00123"),
            (client_ids[0], "M-2024-002", "Acme — Series B Financing", "transactional", "flat_fee", None, None),
            (client_ids[1], "M-2024-003", "Summit — Fund III Formation", "transactional", "hourly", None, None),
            (client_ids[2], "M-2024-004", "Rodriguez v. ABC Corp", "litigation", "contingency", "Denton County", "24-12345-16"),
            (client_ids[3], "M-2024-005", "Greenfield — 1200 Main Acquisition", "real_estate", "flat_fee", None, None),
            (client_ids[4], "M-2025-001", "TechVenture — IP Portfolio Review", "ip", "hourly", None, None),
        ]
        for cid, mnum, mname, mtype, btype, court, cause in matters_data:
            m = Matter(
                tenant_id=tenant_id,
                client_id=cid,
                matter_number=mnum,
                matter_name=mname,
                matter_type=mtype,
                status="active",
                responsible_attorney_id=admin.id,
                originating_attorney_id=admin.id,
                billing_type=btype,
                hourly_rate=350.00 if btype == "hourly" else None,
                flat_fee_amount=25000.00 if btype == "flat_fee" else None,
                contingency_pct=33.33 if btype == "contingency" else None,
                court=court,
                cause_number=cause,
                jurisdiction="Texas",
                open_date=date(2024, 3, 15),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            session.add(m)

        session.commit()
        print(f"Demo tenant created: {tenant_id}")
        print(f"  Domain: praesidium-legal.com")
        print(f"  Branding: Praesidium Legal (default colors)")
        print(f"  Auth: native (local username/password)")
        print(f"  Users: demo@praesidium-legal.com (admin), jsmith@praesidium-legal.com (attorney)")
        print(f"  Clients: {len(clients_data)} demo clients")
        print(f"  Matters: {len(matters_data)} demo matters")
        print(f"  Tier: A (Shared — same database as HJMM)")

    except Exception as e:
        session.rollback()
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    seed()
