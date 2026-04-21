"""
TenantResolver — resolves incoming request hostname to a tenant.

Supports three domain patterns:
1. Custom domain: acmefirm.com → tenant with domain='acmefirm.com'
2. Platform subdomain: acme.praesidium.legal → tenant with slug='acme'
3. Subdomain of custom domain: docs.acmefirm.com → tenant with domain='acmefirm.com'

Never hardcodes any domain — reads from tenants table and PLATFORM_DOMAIN env var.
"""

import os
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession

from core.models.tenant import Tenant


PLATFORM_DOMAIN = os.environ.get("PLATFORM_DOMAIN", "praesidium.legal")


def resolve_tenant(hostname: str, db_session: Session) -> Optional[Tuple[str, str]]:
    """
    Resolve a hostname to (tenant_id, subdomain).

    Returns None if no matching tenant found.

    Examples:
        docs.acmefirm.com     → (tenant_id_for_acme, 'docs')
        billing.acmefirm.com  → (tenant_id_for_acme, 'billing')
        acmefirm.com          → (tenant_id_for_acme, 'portal')
        acme.praesidium.legal  → (tenant_id_for_acme, 'portal')
        docs.acme.praesidium.legal → (tenant_id_for_acme, 'docs')
    """
    hostname = hostname.lower().strip()
    # Remove port if present
    if ":" in hostname:
        hostname = hostname.split(":")[0]

    platform_domain = PLATFORM_DOMAIN.lower()

    # Case 1: Platform subdomain (e.g., acme.praesidium.legal)
    if hostname.endswith(f".{platform_domain}"):
        prefix = hostname[: -(len(platform_domain) + 1)]
        parts = prefix.split(".")
        if len(parts) == 1:
            # acme.praesidium.legal → slug=acme, subdomain=portal
            slug = parts[0]
            subdomain = "portal"
        elif len(parts) == 2:
            # docs.acme.praesidium.legal → slug=acme, subdomain=docs
            subdomain = parts[0]
            slug = parts[1]
        else:
            return None

        stmt = select(Tenant).where(
            Tenant.slug == slug,
            Tenant.is_active == True,
        )
        tenant = db_session.execute(stmt).scalar_one_or_none()
        if tenant:
            return (tenant.id, subdomain)
        return None

    # Case 2: Exact domain match (e.g., acmefirm.com)
    if "." in hostname:
        parts = hostname.split(".")
        # Try progressively longer domain suffixes
        # e.g., for docs.acmefirm.com try acmefirm.com first
        for i in range(len(parts)):
            candidate_domain = ".".join(parts[i:])
            stmt = select(Tenant).where(
                Tenant.domain == candidate_domain,
                Tenant.is_active == True,
            )
            tenant = db_session.execute(stmt).scalar_one_or_none()
            if tenant:
                subdomain = ".".join(parts[:i]) if i > 0 else "portal"
                return (tenant.id, subdomain or "portal")

    return None




async def resolve_tenant_async(hostname: str, db_session: AsyncSession):
    """Async version of resolve_tenant for use in async middleware."""
    hostname = hostname.lower().strip()
    if ":" in hostname:
        hostname = hostname.split(":")[0]
    platform_domain = PLATFORM_DOMAIN.lower()
    if hostname.endswith(f".{platform_domain}"):
        prefix = hostname[: -(len(platform_domain) + 1)]
        parts = prefix.split(".")
        if len(parts) == 1:
            slug, subdomain = parts[0], "portal"
        elif len(parts) == 2:
            subdomain, slug = parts[0], parts[1]
        else:
            return None
        from core.models.tenant import Tenant
        stmt = select(Tenant).where(Tenant.slug == slug)
        result = await db_session.execute(stmt)
        tenant = result.scalar_one_or_none()
        if tenant:
            return (tenant.id, subdomain)
        return None
    if "." in hostname:
        parts = hostname.split(".")
        for i in range(len(parts)):
            candidate_domain = ".".join(parts[i:])
            from core.models.tenant import Tenant
            stmt = select(Tenant).where(Tenant.domain == candidate_domain)
            result = await db_session.execute(stmt)
            tenant = result.scalar_one_or_none()
            if tenant:
                subdomain = ".".join(parts[:i]) if i > 0 else "portal"
                return (tenant.id, subdomain or "portal")
    return None
class TenantResolverMiddleware:
    """
    Starlette middleware that resolves tenant from request hostname
    and attaches tenant_id + subdomain to request.state.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope.setdefault("state", {})
            scope["state"]["tenant_id"] = None
            scope["state"]["subdomain"] = None

            headers = dict(scope.get("headers", []))
            host = headers.get(b"host", b"").decode("utf-8", errors="ignore")

            try:
                from core.db.base import AsyncSessionLocal
                async with AsyncSessionLocal() as session:
                    result = await resolve_tenant_async(host, session)
                    if result:
                        scope["state"]["tenant_id"] = result[0]
                        scope["state"]["subdomain"] = result[1]
            except Exception:
                pass

        await self.app(scope, receive, send)
