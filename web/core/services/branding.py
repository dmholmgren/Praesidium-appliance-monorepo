"""
BrandingService — concrete implementation (NOT abstract).

Mandatory Rule 10: ALL branding references go through BrandingService.
Never hardcode "Praesidium", "hjmmlegal.com", or any firm-specific value.

Loaded once per request via TenantResolver.
Cached in Redis with 5-minute TTL per tenant.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional, Tuple

from core.models.tenant import TenantBranding


REDIS_URL = os.environ.get("REDIS_URL", "")
BRANDING_CACHE_TTL = int(os.environ.get("BRANDING_CACHE_TTL", "300"))  # 5 minutes


@dataclass
class BrandingConfig:
    """Immutable branding configuration for a single tenant."""
    tenant_id: str
    platform_name: str = "Praesidium"
    platform_short_name: str = "Praesidium"
    tagline: str = ""
    suppress_attribution: bool = False
    base_domain: str = "praesidium.legal"
    use_praesidium_subdomain: bool = False
    logo_url: str = ""
    logo_dark_url: str = ""
    favicon_url: str = ""
    css_vars: dict = field(default_factory=lambda: {
        "primary": "#0D1F3C",
        "secondary": "#1A3A5C",
        "accent": "#D4A843",
        "bg": "#FFFFFF",
        "text": "#1A1A1A",
        "heading_font": "Inter, sans-serif",
        "body_font": "Inter, sans-serif",
    })
    email_from_name: str = "Praesidium"
    email_from_address: str = ""
    email_footer_text: str = ""
    email_logo_url: str = ""
    pwa_name: str = "Praesidium"
    pwa_short_name: str = "Praesidium"
    pwa_theme_color: str = "#0D1F3C"
    pwa_icon_url: str = ""
    doc_footer_text: str = ""
    doc_logo_url: str = ""
    addin_display_name: str = "Praesidium"
    addin_description: str = ""

    def to_dict(self) -> dict:
        """Serialize for Redis cache."""
        return {
            "tenant_id": self.tenant_id,
            "platform_name": self.platform_name,
            "platform_short_name": self.platform_short_name,
            "tagline": self.tagline,
            "suppress_attribution": self.suppress_attribution,
            "base_domain": self.base_domain,
            "use_praesidium_subdomain": self.use_praesidium_subdomain,
            "logo_url": self.logo_url,
            "logo_dark_url": self.logo_dark_url,
            "favicon_url": self.favicon_url,
            "css_vars": self.css_vars,
            "email_from_name": self.email_from_name,
            "email_from_address": self.email_from_address,
            "email_footer_text": self.email_footer_text,
            "email_logo_url": self.email_logo_url,
            "pwa_name": self.pwa_name,
            "pwa_short_name": self.pwa_short_name,
            "pwa_theme_color": self.pwa_theme_color,
            "pwa_icon_url": self.pwa_icon_url,
            "doc_footer_text": self.doc_footer_text,
            "doc_logo_url": self.doc_logo_url,
            "addin_display_name": self.addin_display_name,
            "addin_description": self.addin_description,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BrandingConfig":
        """Deserialize from Redis cache."""
        return cls(**data)

    @classmethod
    def from_db_row(cls, row: TenantBranding) -> "BrandingConfig":
        """Build from a TenantBranding ORM instance."""
        return cls(
            tenant_id=row.tenant_id,
            platform_name=row.platform_name or "Praesidium",
            platform_short_name=row.platform_short_name or "Praesidium",
            tagline=row.tagline or "",
            suppress_attribution=row.suppress_attribution or False,
            base_domain=row.base_domain or "praesidium.legal",
            use_praesidium_subdomain=row.use_praesidium_subdomain or False,
            logo_url=row.logo_url or "",
            logo_dark_url=row.logo_dark_url or "",
            favicon_url=row.favicon_url or "",
            css_vars=row.css_vars if isinstance(row.css_vars, dict) else {},
            email_from_name=row.email_from_name or "Praesidium",
            email_from_address=row.email_from_address or "",
            email_footer_text=row.email_footer_text or "",
            email_logo_url=row.email_logo_url or "",
            pwa_name=row.pwa_name or "Praesidium",
            pwa_short_name=row.pwa_short_name or "Praesidium",
            pwa_theme_color=row.pwa_theme_color or "#0D1F3C",
            pwa_icon_url=row.pwa_icon_url or "",
            doc_footer_text=row.doc_footer_text or "",
            doc_logo_url=row.doc_logo_url or "",
            addin_display_name=row.addin_display_name or "Praesidium",
            addin_description=row.addin_description or "",
        )


_redis_client = None


def _get_redis():
    """Lazy-load Redis client."""
    global _redis_client
    if _redis_client is None:
        url = REDIS_URL
        if url:
            import redis
            _redis_client = redis.Redis.from_url(url, decode_responses=True)
        else:
            _redis_client = None
    return _redis_client


class BrandingService:
    """
    Concrete branding service — NOT abstract.

    Loaded once per request via TenantResolver.
    Cached in Redis with 5-minute TTL per tenant.
    Never falls back to hardcoded values without an explicit DEFAULT in the schema.
    """

    @staticmethod
    def _cache_key(tenant_id: str) -> str:
        return f"branding:{tenant_id}"

    @classmethod
    async def load(cls, tenant_id: str, db_session) -> BrandingConfig:
        """
        Load branding config for a tenant.

        1. Check Redis cache
        2. If miss, query tenant_branding table
        3. Cache result in Redis with TTL
        4. Return BrandingConfig
        """
        # Try cache first
        redis_client = _get_redis()
        cache_key = cls._cache_key(tenant_id)

        if redis_client:
            try:
                cached = redis_client.get(cache_key)
                if cached:
                    return BrandingConfig.from_dict(json.loads(cached))
            except Exception:
                pass  # Redis down — fall through to DB

        # Query database
        from sqlalchemy import select
        stmt = select(TenantBranding).where(TenantBranding.tenant_id == tenant_id)
        result = await db_session.execute(stmt)
        row = result.scalar_one_or_none()

        if row:
            config = BrandingConfig.from_db_row(row)
        else:
            # No branding row — return defaults with this tenant_id
            config = BrandingConfig(tenant_id=tenant_id)

        # Cache in Redis
        if redis_client:
            try:
                redis_client.setex(
                    cache_key,
                    BRANDING_CACHE_TTL,
                    json.dumps(config.to_dict()),
                )
            except Exception:
                pass  # Redis down — not fatal

        return config

    @classmethod
    def invalidate(cls, tenant_id: str) -> None:
        """Invalidate cached branding for a tenant (call after updates)."""
        redis_client = _get_redis()
        if redis_client:
            try:
                redis_client.delete(cls._cache_key(tenant_id))
            except Exception:
                pass

    # Convenience methods matching the interface spec from Master Guide Section 3.2

    @staticmethod
    def get_platform_name(config: BrandingConfig) -> str:
        return config.platform_name

    @staticmethod
    def get_base_domain(config: BrandingConfig) -> str:
        return config.base_domain

    @staticmethod
    def get_subdomain_url(config: BrandingConfig, app: str) -> str:
        """Returns https://{app}.{base_domain}"""
        return f"https://{app}.{config.base_domain}"

    @staticmethod
    def get_css_vars(config: BrandingConfig) -> dict:
        return config.css_vars

    @staticmethod
    def get_logo_url(config: BrandingConfig, dark: bool = False) -> str:
        return config.logo_dark_url if dark else config.logo_url

    @staticmethod
    def get_email_from(config: BrandingConfig) -> Tuple[str, str]:
        return (config.email_from_name, config.email_from_address)

    @staticmethod
    def get_pwa_manifest(config: BrandingConfig) -> dict:
        return {
            "name": config.pwa_name,
            "short_name": config.pwa_short_name,
            "theme_color": config.pwa_theme_color,
            "icon": config.pwa_icon_url,
        }

    @staticmethod
    def suppress_attribution(config: BrandingConfig) -> bool:
        return config.suppress_attribution

    @staticmethod
    async def health_check() -> dict:
        """Check Redis connectivity for branding cache."""
        redis_client = _get_redis()
        if redis_client:
            try:
                redis_client.ping()
                return {"status": "healthy", "cache": "redis", "ttl": BRANDING_CACHE_TTL}
            except Exception as e:
                return {"status": "degraded", "cache": "unavailable", "error": str(e)}
        return {"status": "healthy", "cache": "none", "note": "No Redis configured — no caching"}
