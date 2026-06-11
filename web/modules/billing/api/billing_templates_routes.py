"""Route handler for /billing/settings and /billing/templates (redirect)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from core.services.nav_context import get_nav_context
from modules.billing.brand_helper import get_brand
from modules.dashboard.services.auth_helper import get_current_user

router = APIRouter(tags=["billing-settings-views"])


@router.get("/billing/settings", response_class=HTMLResponse)
async def billing_settings_page(request: Request, user=Depends(get_current_user)):
    nav = await get_nav_context(request)
    from modules.billing.api.views import templates
    return templates.TemplateResponse(
        request,
        "billing/billing_settings_react.html",
        {
            "user": user,
            "current_user": user,
            "brand": get_brand(request),
            "page": "billing",
            **nav,
        },
    )


@router.get("/billing/templates")
async def billing_templates_redirect(request: Request):
    """Redirect old URL to new settings page."""
    return RedirectResponse("/billing/settings?tab=templates", status_code=302)
