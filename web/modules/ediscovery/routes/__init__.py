"""eDiscovery route registration."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from modules.ediscovery.routes.collections import router as collections_router
from modules.ediscovery.routes.review import router as review_router

router = APIRouter()


@router.get("/ediscovery", response_class=HTMLResponse)
async def ediscovery_home(request: Request):
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates"])
    brand = getattr(request.state, "branding", None)
    user = getattr(request.state, "current_user", None)
    user_role = getattr(user, "role", "attorney") or "attorney"
    is_admin = user_role in ("admin", "super_admin")
    return templates.TemplateResponse(request, "ediscovery/ediscovery_home.html", {
        "brand": brand, "user": user, "is_admin": is_admin, "page": "ediscovery",
    })


# Sub-routers
router.include_router(collections_router)
router.include_router(review_router)


@router.get("/ediscovery/overview", response_class=HTMLResponse)
async def ediscovery_overview(request: Request):
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates"])
    brand = getattr(request.state, "branding", None)
    return templates.TemplateResponse(request, "ediscovery/ediscovery_overview.html", {"brand": brand})


@router.get("/ediscovery/search", response_class=HTMLResponse)
async def ediscovery_search(
    request: Request,
    matter_id: str = "",
    collection_id: str = "",
    q: str = "",
    doc_type: str = "",
    review_status: str = "",
    custodian: str = "",
    date_from: str = "",
    date_to: str = "",
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
        "per_page":       per_page,
        "total_pages":    total_pages,
        "facets":         facets,
        "results":        results,
    })
