"""Brand context helper — converts BrandingConfig to template-friendly dict."""
from fastapi import Request

_DEFAULTS = {
    "platform_name": "Praesidium", "platform_short_name": "Praesidium",
    "base_domain": "", "billing_url": "", "firm_name": "Praesidium",
    "firm_address": "", "firm_phone": "", "firm_email": "",
    "primary_color": "#0D1F3C", "logo_url": "", "favicon_url": "",
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
        "base_domain": base_domain,
        "billing_url": f"https://billing.{base_domain}" if base_domain else "",
        "firm_name": getattr(branding, "email_from_name", None) or getattr(branding, "platform_name", "Praesidium"),
        "firm_address": "", "firm_phone": "", "firm_email": "",
        "primary_color": css_vars.get("primary", "#0D1F3C"),
        "logo_url": getattr(branding, "logo_url", "") or "",
        "favicon_url": getattr(branding, "favicon_url", "") or "",
    }
