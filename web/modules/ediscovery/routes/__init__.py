"""eDiscovery route registration."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from core.services.nav_context import get_nav_context
from modules.ediscovery.routes.collections import router as collections_router
from modules.ediscovery.routes.review import router as review_router
from modules.ediscovery.routes.ediscovery_matter_home_api import router as matter_home_api_router
from modules.ediscovery.routes.ediscovery_matter_home_api import router as matter_home_api_router

router = APIRouter()


@router.get("/ediscovery", response_class=HTMLResponse)
async def ediscovery_home(request: Request):
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates"])
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(request, "ediscovery/ediscovery_home_react.html", {
        "brand": brand, "page": "ediscovery",
        "current_user": getattr(request.state, "current_user", None),
        **nav_ctx,
    })


# Sub-routers
router.include_router(collections_router)
router.include_router(review_router)
router.include_router(matter_home_api_router)

@router.get("/ediscovery/matter/{matter_id}", response_class=HTMLResponse)
async def ediscovery_matter_home(request: Request, matter_id: str):
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates"])
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    matter_name = "Matter"
    try:
        from core.db.base import AsyncSessionLocal
        from sqlalchemy import text as sa_text
        tid = (getattr(request.state, "tenant_id", "") or "").strip()
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT matter_name FROM matters WHERE id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)"
            ), {"mid": matter_id, "tid": tid})
            row = r.mappings().fetchone()
            if row:
                matter_name = row["matter_name"]
    except Exception:
        pass
    return templates.TemplateResponse(request, "ediscovery/ediscovery_matter_home_react.html", {
        "brand": brand, "page": "ediscovery",
        "current_user": getattr(request.state, "current_user", None),
        "matter_name": matter_name,
        **nav_ctx,
    })

@router.get("/ediscovery/productions", response_class=HTMLResponse)
async def ediscovery_productions_react(request: Request):
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates"])
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(request, "ediscovery/ediscovery_productions_react.html", {
        "brand": brand, "page": "ediscovery",
        "current_user": getattr(request.state, "current_user", None),
        **nav_ctx,
    })



@router.get("/ediscovery/review", response_class=HTMLResponse)
async def ediscovery_review_collections_react(request: Request):
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates"])
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(request, "ediscovery/ediscovery_review_collections_react.html", {
        "brand": brand, "page": "ediscovery",
        "current_user": getattr(request.state, "current_user", None),
        **nav_ctx,
    })



@router.get("/ediscovery/pii-review", response_class=HTMLResponse)
async def ediscovery_pii_review_react(request: Request):
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates"])
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(request, "ediscovery/ediscovery_pii_review_react.html", {
        "brand": brand, "page": "ediscovery",
        "current_user": getattr(request.state, "current_user", None),
        **nav_ctx,
    })


@router.get("/ediscovery/imports", response_class=HTMLResponse)
async def ediscovery_imports_react(request: Request):
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates"])
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(request, "ediscovery/ediscovery_imports_react.html", {
        "brand": brand, "page": "ediscovery",
        "current_user": getattr(request.state, "current_user", None),
        **nav_ctx,
    })



@router.get("/ediscovery/collections/{collection_id}/status", response_class=HTMLResponse)
async def ediscovery_collection_status_react(request: Request, collection_id: str):
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates"])
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(request, "ediscovery/ediscovery_collection_status_react.html", {
        "brand": brand, "page": "ediscovery",
        "current_user": getattr(request.state, "current_user", None),
        **nav_ctx,
    })

@router.get("/ediscovery/overview", response_class=HTMLResponse)
async def ediscovery_overview(request: Request):
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates"])
    brand = getattr(request.state, "branding", None)
    return templates.TemplateResponse(request, "ediscovery/ediscovery_overview.html", {"brand": brand})


@router.get("/ediscovery/search", response_class=HTMLResponse)
async def ediscovery_search_react(request: Request):
    from fastapi.templating import Jinja2Templates
    from core.services.nav_context import get_nav_context
    # Sticky default: a fresh navigation with no matter_id key in the URL is
    # redirected to the user's active matter (topbar pick) so search loads
    # scoped. The React bundle reads matter_id from the URL; an explicit
    # ?matter_id= (even empty) is left untouched — URL stays source of truth.
    if "matter_id" not in request.query_params:
        from core.services.active_matter import read_active_matter
        _uid = getattr(getattr(request.state, "current_user", None), "id", None)
        _tid = (getattr(request.state, "tenant_id", "") or "").strip()
        _am = await read_active_matter(_uid, _tid)
        if _am:
            from fastapi.responses import RedirectResponse
            from urllib.parse import urlencode
            _qs = dict(request.query_params)
            _qs["matter_id"] = _am["matter_id"]
            return RedirectResponse(url="/ediscovery/search?" + urlencode(_qs), status_code=303)
    templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates"])
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(request, "ediscovery/ediscovery_search_react.html", {
        "brand": brand, "page": "ediscovery",
        "current_user": getattr(request.state, "current_user", None),
        **nav_ctx,
    })


@router.get("/ediscovery/search-old", response_class=HTMLResponse)
async def ediscovery_search_old(
    request: Request,
    matter_id: str = "",
    collection_id: str = "",
    q: str = "",
    doc_type: str = "",
    review_status: str = "",
    custodian: str = "",
    date_from: str = "",
    date_to: str = "",
    tag_ids: str = "",
    production_ids: str = "",
    collection_ids: str = "",
    search_mode: str = "keyword",
    page: int = 1,
):
    """eDiscovery Search — Elasticsearch-backed, matter-scoped, with facet filters."""
    from fastapi.templating import Jinja2Templates
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text as sa_text
    from core.services.search_service import SearchService

    templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates"])
    brand = getattr(request.state, "branding", None)
    tenant_id = (getattr(request.state, "tenant_id", "") or "").strip()

    per_page = 25
    offset   = (page - 1) * per_page

    # ── Matter context ────────────────────────────────────────────────────────
    matter = None
    collections = []
    if matter_id:
        async with AsyncSessionLocal() as session:
            mr = await session.execute(sa_text("""
                SELECT m.id::text, m.matter_name, m.matter_number, c.client_name
                FROM matters m
                LEFT JOIN clients c ON m.client_id = c.id
                    AND trim(m.tenant_id) = trim(c.tenant_id)
                WHERE m.id = CAST(:mid AS uuid) AND trim(m.tenant_id) = :tid
            """), {"mid": matter_id, "tid": tenant_id})
            row = mr.mappings().fetchone()
            if row:
                matter = dict(row)

            cr = await session.execute(sa_text("""
                SELECT id::text, display_name
                FROM ediscovery_collections
                WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = :tid
                ORDER BY created_at DESC
            """), {"mid": matter_id, "tid": tenant_id})
            collections = [dict(r) for r in cr.mappings().fetchall()]

    # ── Facet aggregations (DB) ───────────────────────────────────────────────
    facets = {"doc_types": [], "review_statuses": [], "custodians": []}
    if matter_id or collection_id:
        async with AsyncSessionLocal() as session:
            where = "trim(tenant_id) = :tid"
            params: dict = {"tid": tenant_id}
            if matter_id:
                where += " AND matter_id IN (SELECT id FROM ediscovery_collections WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = :tid)"
                params["mid"] = matter_id
            if collection_id:
                where += " AND collection_id = CAST(:cid AS uuid)"
                params["cid"] = collection_id

            # doc_type facet
            dt = await session.execute(sa_text(f"""
                SELECT doc_type, COUNT(*) as cnt
                FROM ediscovery_documents
                WHERE {where} AND doc_type IS NOT NULL
                GROUP BY doc_type ORDER BY cnt DESC LIMIT 20
            """), params)
            facets["doc_types"] = [dict(r) for r in dt.mappings().fetchall()]

            # review_status facet
            rs = await session.execute(sa_text(f"""
                SELECT review_status, COUNT(*) as cnt
                FROM ediscovery_documents
                WHERE {where}
                GROUP BY review_status ORDER BY cnt DESC
            """), params)
            facets["review_statuses"] = [dict(r) for r in rs.mappings().fetchall()]

            # custodian facet
            cu = await session.execute(sa_text(f"""
                SELECT custodian, COUNT(*) as cnt
                FROM ediscovery_documents
                WHERE {where} AND custodian IS NOT NULL AND custodian != ''
                GROUP BY custodian ORDER BY cnt DESC LIMIT 20
            """), params)
            facets["custodians"] = [dict(r) for r in cu.mappings().fetchall()]

    # ── Filter lists (always loaded — not matter-scoped) ─────────────────────
    all_tags = []
    all_productions = []
    all_collections = []
    if tenant_id:
        async with AsyncSessionLocal() as session:
            # Tags
            tr = await session.execute(sa_text("""
                SELECT id::text, name, color, category
                FROM tags
                WHERE TRIM(tenant_id) = :tid
                ORDER BY category NULLS LAST, name
            """), {"tid": tenant_id})
            all_tags = [dict(r) for r in tr.mappings().fetchall()]

            # Productions
            pr = await session.execute(sa_text("""
                SELECT id::text, production_name, status, bates_prefix,
                       bates_start, bates_end, doc_count
                FROM ediscovery_productions
                WHERE TRIM(tenant_id) = :tid
                ORDER BY created_at DESC
            """), {"tid": tenant_id})
            all_productions = [dict(r) for r in pr.mappings().fetchall()]

            # All collections (firm-wide)
            acr = await session.execute(sa_text("""
                SELECT c.id::text, c.collection_name,
                       c.status, c.total_docs,
                       m.matter_name
                FROM ediscovery_collections c
                LEFT JOIN matters m ON m.id = c.matter_id
                    AND TRIM(m.tenant_id) = TRIM(c.tenant_id)
                WHERE TRIM(c.tenant_id) = :tid
                ORDER BY c.created_at DESC
            """), {"tid": tenant_id})
            all_collections = [dict(r) for r in acr.mappings().fetchall()]

    # ── AI-assisted search (Ask AI mode) ─────────────────────────────────────
    ai_result = None
    if search_mode == "ai" and q:
        try:
            from core.services.ai_search_service import ai_search
            ai_result = await ai_search(
                query=q,
                tenant_id=tenant_id,
                matter_id=matter_id or None,
                matter_context=matter if matter else None,
            )
        except Exception as exc:
            import logging
            logging.getLogger("praesidium").error("AI search failed: %s", exc)
            ai_result = None

    # ── Elasticsearch search ──────────────────────────────────────────────────
    results = {"total": 0, "results": []}
    if q or doc_type or review_status or custodian or date_from or date_to:
        try:
            results = await SearchService.search_documents(
                tenant_id=tenant_id,
                query=q,
                matter_id=matter_id or None,
                document_type=doc_type or None,
                limit=per_page,
                offset=offset,
            )
        except Exception as exc:
            import logging
            logging.getLogger("praesidium").error("eDiscovery search error: %s", exc)
            results = {"total": 0, "results": [], "error": str(exc)}

    total_pages = max(1, (results.get("total", 0) + per_page - 1) // per_page)

    return templates.TemplateResponse(request, "ediscovery/ediscovery_search.html", {
        "edisco_tab": "search",
        "brand":          brand,
        "matter":         matter,
        "matter_id":      matter_id,
        "collection_id":  collection_id,
        "collections":    collections,
        "q":              q,
        "doc_type":       doc_type,
        "review_status":  review_status,
        "custodian":      custodian,
        "date_from":      date_from,
        "date_to":        date_to,
        "page":           page,
        "tag_ids":        tag_ids,
        "production_ids": production_ids,
        "collection_ids": collection_ids,
        "per_page":       per_page,
        "total_pages":    total_pages,
        "facets":         facets,
        "results":        results,
        "ai_result":      ai_result,
        "search_mode":    search_mode,
        "all_tags":       all_tags,
        "all_productions": all_productions,
        "all_collections": all_collections,
    })


@router.get("/ediscovery/productions/{production_id}/status", response_class=HTMLResponse)
async def ediscovery_production_status_react(request: Request, production_id: str):
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates"])
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(request, "ediscovery/ediscovery_production_status_react.html", {
        "brand": brand, "page": "ediscovery",
        "current_user": getattr(request.state, "current_user", None),
        **nav_ctx,
    })


@router.get("/ediscovery/collections/project", response_class=HTMLResponse)
@router.get("/ediscovery/collections/project/{matter_id}", response_class=HTMLResponse)
async def ediscovery_collection_project_react(request: Request, matter_id: str = ""):
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates"])
    brand = getattr(request.state, "branding", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(request, "ediscovery/ediscovery_collection_project_react.html", {
        "brand": brand, "page": "ediscovery",
        "current_user": getattr(request.state, "current_user", None),
        **nav_ctx,
    })
