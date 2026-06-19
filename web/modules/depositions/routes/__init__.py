"""Deposition module route registration — viewer page + JSON APIs."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from modules.depositions.routes.depo_api import router as depo_api_router
from modules.depositions.routes.exhibits_api import router as exhibits_api_router
from modules.depositions.routes.designations_api import router as designations_api_router
from modules.depositions.routes.alerts_api import router as alerts_api_router
from modules.depositions.routes.ai_api import router as ai_api_router
from modules.depositions.routes.report_api import router as report_api_router
from modules.depositions.routes.clips_api import router as clips_api_router
from modules.depositions.routes.sync_api import router as sync_api_router
from modules.depositions.routes.brief_workspace_api import router as brief_workspace_api_router
from modules.depositions.routes.schedule_api import router as schedule_api_router
from modules.depositions.routes.trial_center_api import router as trial_center_api_router
from modules.depositions.routes.trial_intake_api import router as trial_intake_api_router
from modules.depositions.routes.bundle_api import router as bundle_api_router

router = APIRouter()
router.include_router(depo_api_router)
router.include_router(exhibits_api_router)
router.include_router(designations_api_router)
router.include_router(alerts_api_router)
router.include_router(ai_api_router)
router.include_router(report_api_router)
router.include_router(clips_api_router)
router.include_router(sync_api_router)
router.include_router(brief_workspace_api_router)
router.include_router(schedule_api_router)
router.include_router(trial_center_api_router)
router.include_router(trial_intake_api_router)
router.include_router(bundle_api_router)


def _templates():
    from fastapi.templating import Jinja2Templates
    return Jinja2Templates(directory=["core/templates", "modules/depositions/templates"])


@router.get("/depositions", response_class=HTMLResponse)
async def depositions_index(request: Request):
    """Top-level Depositions module landing — lists matters with deposition activity;
    each drills into the matter-scoped homepage at /depositions/home/{matter_id}."""
    from core.services.nav_context import get_nav_context
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    return _templates().TemplateResponse(request, "depositions/transcripts_home_react.html", {
        "brand": brand, "page": "depositions",
        "current_user": getattr(request.state, "current_user", None),
        **nav_ctx,
    })


@router.get("/depositions/home/{matter_id}", response_class=HTMLResponse)
async def deposition_home(request: Request, matter_id: str):
    """Depositions homepage — the always-present landing for the Depositions tab.
    The bundle reads the matter id from the URL."""
    from core.services.nav_context import get_nav_context
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    matter_name = "Depositions"
    try:
        from core.db.base import AsyncSessionLocal
        from sqlalchemy import text as sa_text
        tid = (getattr(request.state, "tenant_id", "") or "").strip()
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT matter_name FROM matters "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"id": matter_id, "tid": tid})
            row = r.mappings().fetchone()
            if row and row["matter_name"]:
                matter_name = row["matter_name"]
    except Exception:
        pass
    return _templates().TemplateResponse(request, "depositions/deposition_home_react.html", {
        "brand": brand, "page": "depositions",
        "current_user": getattr(request.state, "current_user", None),
        "matter_id": matter_id, "matter_name": matter_name,
        **nav_ctx,
    })


@router.get("/depositions/transcript/{transcript_id}", response_class=HTMLResponse)
async def deposition_viewer(request: Request, transcript_id: str):
    """Transcript viewer page. transcript_id is read from the URL by the bundle."""
    from core.services.nav_context import get_nav_context
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    deponent = "Transcript"
    try:
        from core.db.base import AsyncSessionLocal
        from sqlalchemy import text as sa_text
        tid = (getattr(request.state, "tenant_id", "") or "").strip()
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT deponent FROM deposition_transcripts "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"id": transcript_id, "tid": tid})
            row = r.mappings().fetchone()
            if row and row["deponent"]:
                deponent = row["deponent"]
    except Exception:
        pass
    # record a per-user view for the "My Recent Transcripts" landing panel (best effort)
    try:
        uid = getattr(getattr(request.state, "current_user", None), "id", None)
        if uid is not None:
            from core.db.base import AsyncSessionLocal
            from sqlalchemy import text as sa_text
            ten = (getattr(request.state, "tenant_id", "") or "").strip()
            async with AsyncSessionLocal() as session:
                await session.execute(sa_text(
                    "INSERT INTO transcript_views (tenant_id, user_id, transcript_id, view_count, viewed_at) "
                    "VALUES (:ten, :uid, CAST(:trid AS uuid), 1, now()) "
                    "ON CONFLICT (tenant_id, user_id, transcript_id) "
                    "DO UPDATE SET view_count = transcript_views.view_count + 1, viewed_at = now()"),
                    {"ten": ten, "uid": uid, "trid": transcript_id})
                await session.commit()
    except Exception:
        pass
    return _templates().TemplateResponse(request, "depositions/deposition_viewer_react.html", {
        "brand": brand, "page": "depositions",
        "current_user": getattr(request.state, "current_user", None),
        "transcript_id": transcript_id, "deponent": deponent,
        **nav_ctx,
    })


@router.get("/appellate/brief/{appellate_case_id}", response_class=HTMLResponse)
async def brief_workspace_page(request: Request, appellate_case_id: str):
    """Appellate brief workspace page; the bundle reads the case id from the URL."""
    from core.services.nav_context import get_nav_context
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    style = "Appellate Brief"
    try:
        from core.db.base import AsyncSessionLocal
        from sqlalchemy import text as sa_text
        tid = (getattr(request.state, "tenant_id", "") or "").strip()
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT style, appellate_cause_number FROM appellate_cases "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"id": appellate_case_id, "tid": tid})
            row = r.mappings().fetchone()
            if row and row["style"]:
                style = row["style"]
    except Exception:
        pass
    return _templates().TemplateResponse(request, "depositions/brief_workspace_react.html", {
        "brand": brand, "page": "appellate",
        "current_user": getattr(request.state, "current_user", None),
        "appellate_case_id": appellate_case_id, "style": style,
        **nav_ctx,
    })


# --------------------------------------------------------------------------- #
#  Trial Center (Scope v2 §4) — top-level module                              #
# --------------------------------------------------------------------------- #
@router.get("/trial", response_class=HTMLResponse)
async def trial_index(request: Request):
    """Trial Center landing — React page listing matters with trial activity
    (exhibit register or trial/hearing transcripts) with a matter picker and a
    drop zone. Matters/search come from /api/v1/trial/matters[/search]; each card
    drills into /trial/home/{matter_id}. Mirrors the Depositions module landing."""
    from core.services.nav_context import get_nav_context
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    # Sticky default: jump to the active matter's trial workspace unless ?pick
    # is set (escape hatch to reach the landing/chooser).
    if "pick" not in request.query_params:
        from core.services.active_matter import read_active_matter
        _uid = getattr(getattr(request.state, "current_user", None), "id", None)
        _tid = (getattr(request.state, "tenant_id", "") or "").strip()
        _am = await read_active_matter(_uid, _tid)
        if _am:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/trial/home/" + _am["matter_id"], status_code=303)
    return _templates().TemplateResponse(request, "depositions/trial_index_react.html", {
        "brand": brand, "page": "trial",
        "current_user": getattr(request.state, "current_user", None),
        **nav_ctx,
    })


@router.get("/trial/home/{matter_id}", response_class=HTMLResponse)
async def trial_home(request: Request, matter_id: str):
    """Trial Center workspace for a matter. The bundle reads the matter id from the URL."""
    from core.services.nav_context import get_nav_context
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    matter_name = "Trial Center"
    try:
        from core.db.base import AsyncSessionLocal
        from sqlalchemy import text as sa_text
        tid = (getattr(request.state, "tenant_id", "") or "").strip()
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT matter_name FROM matters "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"id": matter_id, "tid": tid})
            row = r.mappings().fetchone()
            if row and row["matter_name"]:
                matter_name = row["matter_name"]
    except Exception:
        pass
    return _templates().TemplateResponse(request, "depositions/trial_center_react.html", {
        "brand": brand, "page": "trial",
        "current_user": getattr(request.state, "current_user", None),
        "matter_id": matter_id, "matter_name": matter_name,
        **nav_ctx,
    })
