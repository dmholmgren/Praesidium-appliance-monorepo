"""
Brand context helper for eDiscovery templates.

Lesson learned from DMS Chat 1: Chat 0's branding_middleware populates
request.state.branding with a BrandingConfig object. Module templates
must use get_brand(request) to convert it to a template-friendly dict.

DO NOT create custom Jinja2 template wrappers that bypass the
branding middleware — that causes templates to render with Praesidium
defaults instead of tenant-specific branding (HJMM colors, logos, etc.).

Usage in routes:
    from modules.ediscovery.utils.brand_helper import get_brand
    brand = get_brand(request)
    return templates.TemplateResponse("page.html", {"brand": brand, ...})

Usage in templates:
    {{ brand.platform_name }}
    {{ brand.primary_color }}
    {{ brand.ediscovery_url }}
"""

from fastapi import Request


_DEFAULTS = {
    "platform_name": "Praesidium",
    "platform_short_name": "Praesidium",
    "tagline": "",
    "base_domain": "",
    "docs_url": "",
    "ediscovery_url": "",
    "scan_url": "",
    "support_url": "",
    "logo_url": "",
    "logo_dark_url": "",
    "favicon_url": "",
    "primary_color": "#0D1F3C",
    "secondary_color": "#1A3A5C",
    "accent_color": "#D4A843",
    "bg_color": "#FFFFFF",
    "text_color": "#1A1A1A",
    "heading_font": "Inter, sans-serif",
    "body_font": "Inter, sans-serif",
    "pwa_theme_color": "#0D1F3C",
    "email_from_name": "Praesidium",
    "company_name": "Praesidium",
    "suppress_attribution": False,
}


def get_brand(request: Request) -> dict:
    branding = getattr(request.state, "branding", None)
    if branding is None:
        return dict(_DEFAULTS)

    base_domain = getattr(branding, "base_domain", "") or ""
    css_vars = getattr(branding, "css_vars", None) or {}

    return {
        "platform_name": getattr(branding, "platform_name", None) or "Praesidium",
        "platform_short_name": getattr(branding, "platform_short_name", None) or "Praesidium",
        "tagline": getattr(branding, "tagline", "") or "",
        "base_domain": base_domain,
        "docs_url": f"https://docs.{base_domain}" if base_domain else "",
        "ediscovery_url": f"https://ediscovery.{base_domain}" if base_domain else "",
        "scan_url": f"https://scan.{base_domain}" if base_domain else "",
        "support_url": f"https://support.{base_domain}" if base_domain else "",
        "logo_url": getattr(branding, "logo_url", "") or "",
        "logo_dark_url": getattr(branding, "logo_dark_url", "") or "",
        "favicon_url": getattr(branding, "favicon_url", "") or "",
        "primary_color": css_vars.get("primary", "#0D1F3C"),
        "secondary_color": css_vars.get("secondary", "#1A3A5C"),
        "accent_color": css_vars.get("accent", "#D4A843"),
        "bg_color": css_vars.get("bg", "#FFFFFF"),
        "text_color": css_vars.get("text", "#1A1A1A"),
        "heading_font": css_vars.get("heading_font", "Inter, sans-serif"),
        "body_font": css_vars.get("body_font", "Inter, sans-serif"),
        "pwa_theme_color": getattr(branding, "pwa_theme_color", "") or "#0D1F3C",
        "email_from_name": getattr(branding, "email_from_name", "") or "Praesidium",
        "company_name": getattr(branding, "email_from_name", "") or "Praesidium",
        "suppress_attribution": getattr(branding, "suppress_attribution", False),
    }
