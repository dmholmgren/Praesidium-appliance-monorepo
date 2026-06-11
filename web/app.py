"""
Praesidium Platform — Main FastAPI Application.

All branding via BrandingService. Zero hardcoded names, colors, or URLs.
Test bed: HJMM on 10.10.60.x — config only, not code.
"""

import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

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

# CORS for Stream Deck Property Inspector (runs in embedded Chromium)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
from modules.dms.services.dms_home_api import router as dms_home_api_router
from modules.dms.services.widget_api import router as widget_api_router
from modules.dms.services.matter_workspace_api import router as matter_ws_api_router
from modules.dms.services.dms_upload_route import router as dms_upload_router
from modules.dms.services.dms_upload_route import router as dms_upload_router
from modules.ediscovery.routes.ediscovery_home_api import router as edisco_home_api_router
from modules.ediscovery.routes.review_api import router as review_api_router
from modules.ediscovery.routes.search_api import router as search_api_router
from modules.billing import register_billing_module
from modules.reconciliation import register_reconciliation_module
register_dms_routes(app)

app.include_router(dms_home_api_router)
app.include_router(widget_api_router)
app.include_router(matter_ws_api_router)
from modules.dms.services.onlyoffice_route import router as oo_router
app.include_router(oo_router)
app.include_router(dms_upload_router)
app.include_router(dms_upload_router)

# DMS Client API
from modules.dms.services.dms_client_api import router as dms_client_api_router
app.include_router(dms_client_api_router)

# DMS Search API
from modules.dms.services.dms_search_api import router as dms_search_api_router
app.include_router(dms_search_api_router)

# DMS Annotation API
from modules.dms.services.dms_annotation_api import router as dms_ann_router
app.include_router(dms_ann_router)
app.include_router(edisco_home_api_router)
app.include_router(review_api_router)
app.include_router(search_api_router)
from modules.ediscovery.routes.annotation_api import router as annotation_api_router
app.include_router(annotation_api_router)
from modules.ediscovery.routes.production_api import router as production_api_router
app.include_router(production_api_router)
from modules.ediscovery.routes.share_public import router as share_public_router
app.include_router(share_public_router)
register_billing_module(app)
register_reconciliation_module(app)

from modules.dashboard.routes.dashboard import router as dashboard_router
from modules.dashboard.routes.pi_dashboard_api import router as pi_api_router
app.include_router(dashboard_router)
app.include_router(pi_api_router)

# Contacts Dashboard API — contacts list, CRUD, proposals, stats
from modules.dashboard.routes.contacts_dashboard_api import router as contacts_dash_api_router
app.include_router(contacts_dash_api_router)

# Contacts Seeder API — global batch extraction across all data sources
from modules.dashboard.routes.contacts_seeder_api import router as contacts_seeder_api_router
app.include_router(contacts_seeder_api_router)

# Matter dashboard API — matters home, search, per-matter dashboard
# === Witness Workspace API (must be before matter_dashboard_api) ===
# === end Witness Workspace API ===
# === Witness Workspace API (must be before matter_dashboard_api) ===
from modules.dashboard.routes.witness_workspace_api import router as witness_workspace_api_router
app.include_router(witness_workspace_api_router)
# === end Witness Workspace API ===
from modules.dashboard.routes.matter_dashboard_api import router as matter_dash_api_router
app.include_router(matter_dash_api_router)

# Matter People API — people, witnesses, contact seeding
from modules.dashboard.routes.matter_people_api import router as matter_people_api_router
app.include_router(matter_people_api_router)
from modules.dashboard.routes.practice_areas_api import router as practice_areas_api_router
app.include_router(practice_areas_api_router)
# Deal Room API — key documents, deal points, subject
from modules.dashboard.routes.deal_room_api import router as deal_room_api_router
app.include_router(deal_room_api_router)

# Matter detail page — widget grid surface replacing dashboard_panel_registry model.
# Registered BEFORE the old matter routes in dashboard_router so FastAPI matches first.
from modules.dashboard.routes.matter_detail import router as matter_detail_router
app.include_router(matter_detail_router)

# Matters Home + Matter Dashboard — React page shells
from modules.dashboard.routes.matters_pages import router as matters_pages_router
app.include_router(matters_pages_router)

from modules.ediscovery.routes import router as ediscovery_router
app.include_router(ediscovery_router)

from modules.ediscovery.collection_api import router as collection_router
app.include_router(collection_router)

from modules.ediscovery.production_import import router as production_router
app.include_router(production_router)

from modules.ediscovery.intelligence_layer import router as intelligence_router
app.include_router(intelligence_router)

# === WIAM Adversarial Gap Analysis API (S2-002) ===
from modules.ediscovery.routes.wiam_api import router as wiam_api_router
app.include_router(wiam_api_router)
# === end WIAM API ===

# Note: issue_map and drift_detection routes live in intelligence_layer.py
# issue_map.py and drift_detection.py provide exported trigger hooks only —
# their routers are NOT registered here to avoid duplicate route conflicts.

from modules.ediscovery.tag_intelligence import router as tag_intelligence_router
app.include_router(tag_intelligence_router)

from modules.ediscovery.routes.chat import router as ediscovery_chat_router
app.include_router(ediscovery_chat_router)

from modules.ediscovery.routes.collection_status import router as collection_status_router
app.include_router(collection_status_router)

from modules.ediscovery.routes.partials import router as ediscovery_partials_router
app.include_router(ediscovery_partials_router)

from modules.ediscovery.routes.upload import router as ediscovery_upload_router
app.include_router(ediscovery_upload_router)

from modules.admin.platform import router as platform_router
app.include_router(platform_router)

from modules.admin.review_api_json import router as review_json_router
app.include_router(review_json_router)

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

# === Contact workstream Phase C routers ===
# Review queues registered FIRST so /dedup-review and /proposals-review
# match before the contacts router's /{contact_id} catchall.
from modules.admin.contact_review_api import router as contact_review_router
app.include_router(contact_review_router)

from modules.admin.contacts_api import router as contacts_router
app.include_router(contacts_router)
# === end Contact workstream Phase C routers ===

from modules.admin.crawl_api import router as crawl_router
app.include_router(crawl_router)

from modules.admin.seeding_api import router as seeding_router
app.include_router(seeding_router)

from modules.admin.timesheet_api import router as timesheet_router
app.include_router(timesheet_router)

from modules.admin.provisioning_wizard import router as provision_router
app.include_router(provision_router)

from modules.tenant_admin.tenant_admin import router as tenant_admin_router
from modules.tenant_admin.tenant_admin_api import router as tenant_admin_api_router
from modules.tenant_admin.email_routes import router as email_routes_router
from modules.tenant_admin.file_import_api import router as file_import_api_router
from modules.tenant_admin.onboarding_api import router as onboarding_api_router
app.include_router(tenant_admin_router)
app.include_router(tenant_admin_api_router)
app.include_router(email_routes_router)
app.include_router(file_import_api_router)
app.include_router(onboarding_api_router)

from modules.tenant_admin.onboarding_metadata_api import router as metadata_recon_router

from modules.tenant_admin.permissions_api import router as permissions_api_router
app.include_router(permissions_api_router)
app.include_router(metadata_recon_router)

from modules.tenant_admin.email_sync_api import router as email_sync_api_router
app.include_router(email_sync_api_router)

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

from modules.drafting.drafting_api import router as drafting_api_router
app.include_router(drafting_api_router)

from modules.intelligence import ai_usage_router
# === Matter Intelligence Extraction Engine ===
from modules.intelligence.matter_extract import router as matter_extract_router
from modules.intelligence.primitives_api import router as primitives_router
app.include_router(matter_extract_router)
app.include_router(primitives_router)
# === end Matter Intelligence ===

app.include_router(ai_usage_router)

# === Universal Document Viewer API ===
from modules.dms.services.document_viewer_api import router as document_viewer_router

from modules.dms.services.adobe_convert_api import router as adobe_convert_router
app.include_router(document_viewer_router)
# === end Universal Document Viewer ===

# === Project Workspace — Document Assembly (registered BEFORE generic projects_router) ===
from modules.dashboard.routes.project_documents_api import router as project_docs_router, exhibit_router as project_exhibit_router
from modules.dashboard.routes.project_workspace_route import router as project_workspace_router
app.include_router(project_workspace_router)
app.include_router(project_docs_router)
app.include_router(project_exhibit_router)
# === end Project Workspace ===

from modules.dashboard.routes.projects_router import router as projects_router
from modules.dashboard.routes.ai_chat_route import router as ai_chat_router
from modules.dashboard.routes.task_project_api import router as task_project_router
from modules.widgets.widget_routes import router as widget_router
app.include_router(projects_router)
app.include_router(ai_chat_router)

# === Fill Me In ===
from modules.dashboard.routes.fill_me_in_api import router as fill_me_in_api_router
from modules.dashboard.routes.fill_me_in_pages import router as fill_me_in_pages_router
app.include_router(fill_me_in_api_router)
app.include_router(fill_me_in_pages_router)
# === end Fill Me In ===
app.include_router(task_project_router)

app.include_router(widget_router)

from modules.widgets.layout_loader import router as layout_router
app.include_router(layout_router)

# === M-DESK C1 — Desktop client backend ===
from modules.desktop.auth_router import router as desktop_auth_router
from modules.desktop.checkout_router import router as desktop_checkout_router
from modules.desktop.manifest_router import router as desktop_manifest_router
from modules.desktop.compare_router import router as desktop_compare_router
app.include_router(desktop_auth_router)
app.include_router(desktop_checkout_router)
app.include_router(desktop_manifest_router)
app.include_router(desktop_compare_router)
# === end M-DESK C1 ===

# === M-DESK C2 — Desktop client browse endpoints ===
from modules.desktop.browse_router import router as desktop_browse_router
app.include_router(desktop_browse_router)
# === end M-DESK C2 ===

# === M-DESK C3 — Search + AI Drafting + Matter Selector ===
from modules.desktop.desktop_c3_router import router as desktop_c3_router
app.include_router(desktop_c3_router)
# === end M-DESK C3 ===

# === Communications Center ===
from modules.dashboard.services.comms_api import router as comms_router
app.include_router(comms_router)

# === Presentation WebSocket Relay (Conference Space) ===
from core.services.presentation_ws import router as presentation_ws_router

# === Zoom Meeting SDK API (Conference Space) ===
from core.services.zoom_sdk_api import router as zoom_sdk_router
app.include_router(zoom_sdk_router)
# === end Zoom SDK ===

# === Zoom OAuth + OBF Token Service ===
from core.services.zoom_oauth_service import router as zoom_oauth_router
app.include_router(zoom_oauth_router)
# === end Zoom OAuth ===
app.include_router(presentation_ws_router)
# === end Presentation WebSocket ===

# === end Communications Center ===

# === Calendar API ===
from modules.dashboard.services.calendar_api import router as calendar_api_router
app.include_router(calendar_api_router)
# === end Calendar API ===



# === M-DESK C5 — Upload, Projects & Platform Checkout ===
from modules.desktop.desktop_c5_router import router as desktop_c5_router
app.include_router(desktop_c5_router)
app.include_router(adobe_convert_router)
# === end M-DESK C5 ===

# === M-MOBILE C1 — Mobile PWA ===
from modules.mobile.mobile_api import router as mobile_router, pwa_router
app.include_router(mobile_router)
app.include_router(pwa_router)
# === end M-MOBILE C1 ===






# === Communications Center page route ===
@app.get("/communications/")
@app.get("/communications")
async def communications_page(request: Request):
    """Communications Center — full page."""
    from core.services.nav_context import get_nav_context
    brand = getattr(request.state, "branding", None)
    if not brand:
        brand = BrandingConfig(tenant_id=getattr(request.state, "tenant_id", None) or "unknown")
    user = getattr(request.state, "current_user", None)
    nav_ctx = await get_nav_context(request)
    tmpl = Jinja2Templates(directory=["core/templates", "modules/dashboard/templates"])
    return tmpl.TemplateResponse("dashboard/comms_center_react.html", {
        "request": request, "brand": brand, "current_user": user,
        "page": "communications", **nav_ctx,
    })

# ─── Static files

# === Calendar page route ===
@app.get("/calendar")
@app.get("/calendar/")
async def calendar_page(request: Request):
    """Calendar — full page with month/week views."""
    from core.services.nav_context import get_nav_context
    brand = getattr(request.state, "branding", None)
    if not brand:
        brand = BrandingConfig(tenant_id=getattr(request.state, "tenant_id", None) or "unknown")
    user = getattr(request.state, "current_user", None)
    nav_ctx = await get_nav_context(request)
    tmpl = Jinja2Templates(directory=["core/templates"])
    return tmpl.TemplateResponse("calendar_react.html", {
        "request": request, "brand": brand, "current_user": user,
        "page": "calendar", **nav_ctx,
    })
# === end Calendar page ===

# === Contacts Search ===
from modules.dashboard.routes.contacts_search_api import router as contacts_search_router
app.include_router(contacts_search_router)
# === end Contacts Search ===
# === User Settings ===
from modules.dashboard.routes.user_settings_api import router as user_settings_api_router
from modules.dashboard.routes.user_settings_pages import router as user_settings_pages_router
app.include_router(user_settings_api_router)
app.include_router(user_settings_pages_router)
# === end User Settings ===

# === Meeting Workspaces ===
from modules.dashboard.routes.workspace_api import router as workspace_api_router
from modules.dashboard.routes.meeting_workspace_route import router as meeting_workspace_router
app.include_router(workspace_api_router)
app.include_router(meeting_workspace_router)
# === end Meeting Workspaces ===

# === Witness Workspace ===
from modules.dashboard.routes.witness_workspace_route import router as witness_workspace_router
app.include_router(witness_workspace_router)
# === end Witness Workspace ===

# === Widget Layout API ===
from modules.dashboard.routes.widget_layout_api import router as widget_layout_api_router
from modules.dashboard.routes.research_capture_api import router as research_capture_router
app.include_router(widget_layout_api_router)
app.include_router(research_capture_router)

# === Stream Deck Integration ===
from modules.dashboard.routes.streamdeck_api import router as streamdeck_api_router
app.include_router(streamdeck_api_router)
# === end Stream Deck ===
# === Keymap API (hotkeys) ===
from modules.dashboard.routes.keymap_api import router as keymap_api_router
app.include_router(keymap_api_router)
# === end Keymap API ===
# === end Widget Layout API ===

# === Conference Space redirect ===
@app.get("/conference-space")
@app.get("/conference-space/")
async def conference_space_redirect():
    return HTMLResponse(status_code=302, headers={"Location": "/communications/?tab=conference"})
# === end Conference Space redirect ===


# === Witness Workspace API — direct app route ===
@app.get("/api/v1/matter/{matter_id}/witnesses/{contact_id}")
async def witness_workspace_api_direct(request: Request, matter_id: str, contact_id: str):
    from modules.dashboard.routes.witness_workspace_api import get_witness_workspace
    return await get_witness_workspace(request, matter_id, contact_id)
# === end Witness Workspace API direct ===

app.mount("/static", StaticFiles(directory="/app/static"), name="static")

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

# === Client Portal Admin ===
from modules.billing.api.client_portal_page import router as client_portal_page_router
app.include_router(client_portal_page_router)
# === end Client Portal Admin ===

# === Client Portal API ===
from modules.billing.api.client_portal_api import router as client_portal_api_router
app.include_router(client_portal_api_router)
# === end Client Portal API ===

# === Context Menu Registry ===
from modules.dashboard.routes.context_menu_api import router as context_menu_api_router
app.include_router(context_menu_api_router)
# === end Context Menu Registry ===

# === Property Intelligence ===
from modules.property.router import router as property_router
from modules.property.page_route import router as property_page_router
app.include_router(property_router)
app.include_router(property_page_router)
from modules.property.extract_api import router as property_extract_router
app.include_router(property_extract_router)
# === end Property Intelligence ===
# === Nav Section Tabs Registry ===
from modules.dashboard.routes.nav_tabs_api import router as nav_tabs_api_router
app.include_router(nav_tabs_api_router)
# === end Nav Section Tabs Registry ===
