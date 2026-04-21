"""
Praesidium Platform — Main FastAPI Application.

All branding via BrandingService. Zero hardcoded names, colors, or URLs.
Test bed: HJMM on 10.10.60.x — config only, not code.
"""

import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from core.db.base import init_db, get_session_factory, get_engine, AsyncSessionLocal
from core.db.tenant import TenantResolverMiddleware
from core.services.branding import BrandingService, BrandingConfig
from core.auth.middleware import AuthMiddleware


# ─── Lifespan ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize services on startup."""
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url:
        try:
            pass  # Schema managed by Alembic migrations
        except Exception as e:
            import logging
            logging.getLogger("praesidium").warning(f"DB init failed (will retry on first request): {e}")
    yield


# ─── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Praesidium Platform",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

# ─── Middleware (order matters — outermost first) ────────────────────────────

SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.add_middleware(AuthMiddleware)
app.add_middleware(TenantResolverMiddleware)
app.add_middleware(__import__("modules.dashboard.services.activity_middleware", fromlist=["ActivityMiddleware"]).ActivityMiddleware)

# ─── Templates ───────────────────────────────────────────────────────────────

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "core", "templates")
)


async def get_brand_context(request: Request) -> dict:
    """
    Build template context with brand.* variables.
    Mandatory Rule 10: All templates use {{ brand.* }} — never hardcoded.
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    brand = getattr(request.state, "branding", None)

    if not brand and tenant_id:
        async with AsyncSessionLocal() as session:
            brand = await BrandingService.load(tenant_id, session)
        request.state.branding = brand

    if not brand:
        brand = BrandingConfig(tenant_id=tenant_id or "unknown")

    css_vars = brand.css_vars or {}
    return {
        "brand": {
            "platform_name":        brand.platform_name,
            "platform_short_name":  brand.platform_short_name,
            "tagline":              brand.tagline,
            "base_domain":          brand.base_domain,
            "logo_url":             brand.logo_url,
            "logo_dark_url":        brand.logo_dark_url,
            "favicon_url":          brand.favicon_url,
            "primary_color":        css_vars.get("primary", "#0D1F3C"),
            "secondary_color":      css_vars.get("secondary", "#1A3A5C"),
            "accent_color":         css_vars.get("accent", "#D4A843"),
            "bg_color":             css_vars.get("bg", "#FFFFFF"),
            "text_color":           css_vars.get("text", "#1A1A1A"),
            "heading_font":         css_vars.get("heading_font", "Inter, sans-serif"),
            "body_font":            css_vars.get("body_font", "Inter, sans-serif"),
            "email_from_name":      brand.email_from_name,
            "pwa_name":             brand.pwa_name,
            "pwa_theme_color":      brand.pwa_theme_color,
            "suppress_attribution": brand.suppress_attribution,
        },
        "current_user": getattr(request.state, "current_user", None),
        "subdomain":    getattr(request.state, "subdomain", "portal"),
    }


# ─── Branding middleware ──────────────────────────────────────────────────────

@app.middleware("http")
async def branding_middleware(request: Request, call_next):
    """Load branding config for every request — cached in Redis."""
    if not hasattr(request.state, "tenant_id"):
        request.state.tenant_id = None
    if not hasattr(request.state, "branding"):
        request.state.branding = None

    tenant_id = request.state.tenant_id
    if tenant_id:
        try:
            async with AsyncSessionLocal() as session:
                request.state.branding = await BrandingService.load(tenant_id, session)
        except Exception:
            request.state.branding = BrandingConfig(tenant_id=tenant_id)

    response = await call_next(request)
    return response


# ─── Routes ──────────────────────────────────────────────────────────────────

from modules.dms.router import register_dms_routes
from modules.billing import register_billing_module
register_dms_routes(app)
register_billing_module(app)

from modules.dashboard.routes.dashboard import router as dashboard_router
app.include_router(dashboard_router)

from modules.ediscovery.routes import router as ediscovery_router
app.include_router(ediscovery_router)

from modules.ediscovery.collection_api import router as collection_router
app.include_router(collection_router)

from modules.ediscovery.production_import import router as production_router
app.include_router(production_router)

from modules.ediscovery.intelligence_layer import router as intelligence_router
app.include_router(intelligence_router)

# Note: issue_map and drift_detection routes live in intelligence_layer.py
# issue_map.py and drift_detection.py provide exported trigger hooks only —
# their routers are NOT registered here to avoid duplicate route conflicts.

from modules.ediscovery.tag_intelligence import router as tag_intelligence_router
app.include_router(tag_intelligence_router)

from modules.ediscovery.routes.chat import router as ediscovery_chat_router
app.include_router(ediscovery_chat_router)

from modules.admin.platform import router as platform_router
app.include_router(platform_router)

from modules.admin.config_generator import router as config_generator_router
app.include_router(config_generator_router)

from modules.admin.jobs_api import router as jobs_api_router
app.include_router(jobs_api_router)

from modules.admin.admin_panel import router as admin_panel_router
app.include_router(admin_panel_router)

from modules.admin.licensing_api import router as licensing_api_router
app.include_router(licensing_api_router)

from modules.admin.user_mgmt_api import router as user_mgmt_router
app.include_router(user_mgmt_router)

from modules.admin.crawl_api import router as crawl_router
app.include_router(crawl_router)

from modules.admin.seeding_api import router as seeding_router
app.include_router(seeding_router)

from modules.admin.timesheet_api import router as timesheet_router
app.include_router(timesheet_router)

from modules.admin.provisioning_wizard import router as provision_router
app.include_router(provision_router)

from modules.tenant_admin.tenant_admin import router as tenant_admin_router
app.include_router(tenant_admin_router)

from modules.connectors.router import router as connector_router
app.include_router(connector_router)

from modules.connectors.entities_router import router as entities_router
app.include_router(entities_router)

from modules.connectors.registry_router import router as registry_router
app.include_router(registry_router)

from modules.connectors.client_matter_cleanup_router import router as cleanup_router
app.include_router(cleanup_router)

from modules.drafting.drafting_router import router as drafting_router
app.include_router(drafting_router)

from modules.widgets.widget_routes import router as widget_router
app.include_router(widget_router)

from modules.widgets.layout_loader import router as layout_router
app.include_router(layout_router)


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health", response_class=JSONResponse)
async def health_check(request: Request):
    statuses = {}

    # Database
    try:
        engine = get_engine()
        if engine:
            from sqlalchemy import text as sa_text
            with engine.connect() as conn:
                conn.execute(sa_text("SELECT 1"))
            statuses["database"] = {"status": "healthy"}
        else:
            statuses["database"] = {"status": "not_configured"}
    except Exception as e:
        statuses["database"] = {"status": "unhealthy", "error": str(e)}

    # Redis
    try:
        import redis as redis_lib
        redis_url = os.environ.get("REDIS_URL", "")
        if redis_url:
            r = redis_lib.Redis.from_url(redis_url, decode_responses=True)
            r.ping()
            statuses["redis"] = {"status": "healthy"}
        else:
            statuses["redis"] = {"status": "not_configured"}
    except Exception as e:
        statuses["redis"] = {"status": "unhealthy", "error": str(e)}

    # Meilisearch
    try:
        import httpx
        meili_url = os.environ.get("MEILISEARCH_URL", "")
        if meili_url:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{meili_url}/health", timeout=5)
                if resp.status_code == 200:
                    statuses["meilisearch"] = {"status": "healthy"}
                else:
                    statuses["meilisearch"] = {"status": "unhealthy", "code": resp.status_code}
        else:
            statuses["meilisearch"] = {"status": "not_configured"}
    except Exception as e:
        statuses["meilisearch"] = {"status": "unhealthy", "error": str(e)}

    # Whisper
    try:
        import httpx
        whisper_url = os.environ.get("WHISPER_SERVICE_URL", "")
        if whisper_url:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{whisper_url}/health", timeout=5)
                statuses["whisper"] = {"status": "healthy" if resp.status_code == 200 else "unhealthy"}
        else:
            statuses["whisper"] = {"status": "not_configured"}
    except Exception as e:
        statuses["whisper"] = {"status": "unhealthy", "error": str(e)}

    # BrandingService
    branding_status = await BrandingService.health_check()
    statuses["branding"] = branding_status

    tenant_id = getattr(request.state, "tenant_id", None)
    branding_config = getattr(request.state, "branding", None)
    if branding_config:
        statuses["tenant"] = {
            "tenant_id":     tenant_id,
            "platform_name": branding_config.platform_name,
            "base_domain":   branding_config.base_domain,
        }

    all_healthy = all(
        s.get("status") in ("healthy", "not_configured", "stub", "degraded")
        for s in statuses.values()
        if isinstance(s, dict) and "status" in s
    )

    return JSONResponse(
        status_code=200 if all_healthy else 503,
        content={
            "status":    "healthy" if all_healthy else "degraded",
            "timestamp": datetime.utcnow().isoformat(),
            "services":  statuses,
        },
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    context = {"request": request}
    context.update(await get_brand_context(request))
    return templates.TemplateResponse("login.html", context)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = getattr(request.state, "current_user", None)
    if user:
        return HTMLResponse(status_code=302, headers={"Location": "/dashboard"})

    subdomain = getattr(request.state, "subdomain", None)
    if subdomain == "portal":
        brand = getattr(request.state, "branding", None)
        if brand and brand.base_domain:
            return HTMLResponse(
                status_code=302,
                headers={"Location": f"https://login.{brand.base_domain}/login"},
            )
    return HTMLResponse(status_code=302, headers={"Location": "/login"})
