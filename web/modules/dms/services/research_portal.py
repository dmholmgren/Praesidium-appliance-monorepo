from sqlalchemy import text as sa_text
from modules.dms.brand_helper import get_brand
"""
COMP 10 — Research Portal UI
HTMX UI at /dms/research/{matter_id}/
- Search interface with jurisdiction filter, date range
- Results with Shepards/KeyCite signals
- Save to matter, cross-matter search
"""

import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dms/research", tags=["dms-research"])
from jinja2 import Environment, FileSystemLoader
_loader = FileSystemLoader(["/app/modules/dms/templates", "/app/core/templates"])
_env = Environment(loader=_loader, autoescape=True)

class _Templates:
    """Wrapper to make Jinja2 env work like Starlette Jinja2Templates."""
    def __init__(self, env):
        self.env = env
    def TemplateResponse(self, name, context, status_code=200):
        from starlette.responses import HTMLResponse
        template = self.env.get_template(name)
        content = template.render(context)
        return HTMLResponse(content=content, status_code=status_code)

templates = _Templates(_env)


class SaveResearchRequest(BaseModel):
    result_citation: str
    result_title: str
    provider_doc_id: str
    provider: str


@router.get("/{matter_id}", response_class=HTMLResponse)
async def research_portal(
    request: Request,
    matter_id: str,
    q: str = "",
    jurisdiction: str = "",
    date_from: str = "",
    date_to: str = "",
    provider: str = "lexis",
):
    """Research portal for a specific matter."""
    from core.db.base import TenantSession, get_session_factory

    tenant_id = request.state.tenant_id
    brand = get_brand(request)
    session = TenantSession(get_session_factory()(), tenant_id)

    matter = session.execute(
        sa_text("""SELECT m.*, c.client_name as client_name
           FROM matters m
           LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
           WHERE m.id = :mid AND m.tenant_id = :tid"""),
        {"mid": matter_id, "tid": tenant_id},
    ).fetchone()

    if not matter:
        raise HTTPException(status_code=404, detail="Matter not found")

    results = []
    if q:
        # Check cache first
        from modules.dms.adapters.legal_research import (
            get_cached_results, cache_research_results,
        )
        cached = get_cached_results(tenant_id, q, provider, matter_id)
        if cached:
            results = cached
        else:
            # Call research provider
            adapter = _get_adapter(tenant_id, provider)
            if adapter:
                raw_results = await adapter.search(
                    query=q,
                    jurisdiction=jurisdiction,
                    date_from=date_from,
                    date_to=date_to,
                )
                results = [
                    {
                        "citation": r.citation, "title": r.title,
                        "court": r.court, "entry_date": r.date,
                        "relevance_score": r.relevance_score,
                        "snippet": r.snippet,
                        "cite_signal": r.cite_signal.value,
                        "provider": r.provider,
                        "provider_doc_id": r.provider_doc_id,
                    }
                    for r in raw_results
                ]
                # Cache results
                cache_research_results(
                    tenant_id, request.state.current_user,
                    matter_id, q, provider, raw_results,
                )
                await adapter.close()

    # Get prior research sessions for this matter
    prior_sessions = session.execute(
        sa_text("""SELECT id, query, provider, created_at
           FROM research_sessions
           WHERE tenant_id = :tid AND matter_id = :mid
           ORDER BY created_at DESC LIMIT 20"""),
        {"tid": tenant_id, "mid": matter_id},
    ).fetchall()

    return templates.TemplateResponse("research_portal.html", {
        "request": request,
        "brand": brand,
        "matter": matter,
        "query": q,
        "jurisdiction": jurisdiction,
        "date_from": date_from,
        "date_to": date_to,
        "provider": provider,
        "results": results,
        "prior_sessions": prior_sessions,
    })


@router.post("/{matter_id}/save")
async def save_research_to_matter(
    request: Request, matter_id: str, body: SaveResearchRequest,
):
    """Save a research result to the matter's research folder."""
    from core.audit import write_audit

    tenant_id = request.state.tenant_id
    user_id = getattr(request.state.current_user, "id", request.state.current_user) if request.state.current_user else "anonymous"
    session = TenantSession(get_session_factory()(), tenant_id)

    doc_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Get full document text from provider
    adapter = _get_adapter(tenant_id, body.provider)
    full_text = ""
    if adapter:
        try:
            result = await adapter.get_document(body.provider_doc_id)
            full_text = result.full_text or ""
        except Exception as e:
            logger.error(f"Failed to fetch full document: {e}")
        await adapter.close()

    # Save as a research document in DMS
    session.execute(
        sa_text("""INSERT INTO documents
        (id, tenant_id, matter_id, name, storage_path, file_type,
         file_size, ocr_text, created_at, modified_at, is_deleted,
         uploaded_by, doc_category)
        VALUES (:id, :tid, :mid, :name, :path, 'research',
                :size, :text, :now, :now, 0, :by, 'research')"""),
        {
            "id": doc_id, "tid": tenant_id, "mid": matter_id,
            "name": f"{body.result_citation} - {body.result_title[:80]}",
            "path": f"research/{body.provider}/{body.provider_doc_id}",
            "size": len(full_text),
            "text": full_text,
            "now": now, "by": user_id,
        },
    )

    write_audit(
        tenant_id=tenant_id, user_id=user_id,
        action="save_research", module="dms", table_name="documents",
        record_id=doc_id,
        new_value={"citation": body.result_citation, "provider": body.provider},
        source="research_portal",
    )
    session.commit()

    return HTMLResponse(
        content='<span class="text-green-600 text-sm">Saved to matter</span>',
    )


@router.get("/{matter_id}/cite-check", response_class=HTMLResponse)
async def cite_check(
    request: Request, matter_id: str,
    citation: str = "", provider: str = "lexis",
):
    """Run Shepards/KeyCite check on a citation."""
    tenant_id = request.state.tenant_id
    brand = get_brand(request)

    result = None
    if citation:
        adapter = _get_adapter(tenant_id, provider)
        if adapter:
            result = await adapter.cite_check(citation)
            await adapter.close()

    return templates.TemplateResponse("cite_check_result.html", {
        "request": request,
        "brand": brand,
        "citation": citation,
        "result": result,
    })


@router.get("/cross-matter-search", response_class=HTMLResponse)
async def cross_matter_search(request: Request, q: str = ""):
    """Search across all matters for prior research on similar issues."""

    tenant_id = request.state.tenant_id
    brand = get_brand(request)
    session = TenantSession(get_session_factory()(), tenant_id)

    results = []
    if q:
        results = session.execute(
            sa_text("""SELECT rs.query, rs.provider, rs.created_at,
                      m.matter_name as matter_name, m.matter_number as matter_number,
                      rs.matter_id
               FROM research_sessions rs
               JOIN matters m ON rs.matter_id = m.id AND rs.tenant_id = m.tenant_id
               WHERE rs.tenant_id = :tid
               AND (rs.query LIKE :q OR rs.results_json LIKE :q)
               ORDER BY rs.created_at DESC LIMIT 50"""),
            {"tid": tenant_id, "q": f"%{q}%"},
        ).fetchall()

    return templates.TemplateResponse("cross_matter_search.html", {
        "request": request,
        "brand": brand,
        "query": q,
        "results": results,
    })


def _get_adapter(tenant_id: str, provider: str):
    """Get the appropriate research adapter based on provider name."""
    from modules.dms.adapters.legal_research import LexisAdapter, WestlawAdapter

    if provider == "westlaw":
        return WestlawAdapter()
    return LexisAdapter()  # Default to Lexis
