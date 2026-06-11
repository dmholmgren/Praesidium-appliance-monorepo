"""
User Settings page route.

Serves GET /user-settings → user_settings_react.html
Mount this router in core/routes.py or wherever top-level page routes live.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from core.services.nav_context import get_nav_context

router = APIRouter(tags=["user-settings-pages"])

# Adjust path if templates live elsewhere
templates = Jinja2Templates(directory="core/templates")


@router.get("/user-settings", response_class=HTMLResponse)
async def user_settings_page(request: Request):
    nav = await get_nav_context(request)
    return templates.TemplateResponse(
        request,
        "user_settings_react.html",
        {**nav},
    )
