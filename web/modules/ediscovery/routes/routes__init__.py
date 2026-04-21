"""eDiscovery route registration."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from modules.ediscovery.routes.collections import router as collections_router

router = APIRouter()

templates = Jinja2Templates(
    directory=["core/templates", "modules/ediscovery/templates"]
)


def _get_brand(request: Request):
    return getattr(request.state, "branding", None)


@router.get("/ediscovery", response_class=HTMLResponse)
async def ediscovery_home(request: Request):
    """
    eDiscovery landing page.
    Loads the ediscovery_firm layout via HTMX — widget composition,
    not a hardcoded handler. Collection list loads as a widget below.
    """
    brand = _get_brand(request)
    user = getattr(request.state, "current_user", None)
    user_role = getattr(user, "role", "attorney") or "attorney"
    is_admin = user_role in ("admin", "super_admin")

    return templates.TemplateResponse(
        request,
        "ediscovery/ediscovery_home.html",
        {
            "brand": brand,
            "user": user,
            "is_admin": is_admin,
            "page": "ediscovery",
        },
    )


router.include_router(collections_router)
