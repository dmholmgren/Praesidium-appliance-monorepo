"""
modules/drafting/research_router.py

Tenant-level research router. ONE indirection so research queries go where the
tenant configures (research_routes table) instead of being hard-wired in each
caller. Add/scale providers here; switch a tenant by changing a DB row, not code.

Providers:
  - courtlistener_corpus : semantic search over the local reference_opinions
                           ModernBERT-768 corpus (reference_opinion_embeddings).
                           Guarded: needs a pgvector ANN index (ivfflat/hnsw) to
                           be safe at 72M rows — returns a 'not yet indexed' note
                           until the corpus goes live.
  - courtlistener_live   : live CourtListener API via modules.drafting.research_service.
  - (extend: matter_dms, lexis, westlaw, …)

Result rows are normalized to the record-search shape:
  {id, source:'research', kind:'RESEARCH', label, locus, snippet, sim, view_url}
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "courtlistener_corpus"


async def resolve_route(tenant_id: str, route_key: str = "default") -> tuple:
    """Resolve (provider, params) for a tenant. A tenant-specific row wins over
    the platform-default (tenant_id IS NULL) row. Falls back to the built-in
    default if the table/row is missing."""
    tid = (tenant_id or "").strip()
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT provider, params FROM research_routes "
                "WHERE route_key = :rk AND is_active "
                "  AND (TRIM(tenant_id) = TRIM(:tid) OR tenant_id IS NULL) "
                "ORDER BY (tenant_id IS NOT NULL) DESC LIMIT 1"),
                {"rk": route_key, "tid": tid})
            row = r.mappings().fetchone()
            if row:
                return row["provider"], (row["params"] or {})
    except Exception:
        logger.exception("resolve_route failed; using built-in default")
    return DEFAULT_PROVIDER, {}


async def _corpus_indexed(session) -> bool:
    """True iff reference_opinion_embeddings has a pgvector ANN index — the
    readiness gate that flips on when the CourtListener corpus goes live."""
    r = await session.execute(sa_text(
        "SELECT 1 FROM pg_indexes WHERE tablename = 'reference_opinion_embeddings' "
        "  AND (indexdef ILIKE '%ivfflat%' OR indexdef ILIKE '%hnsw%') LIMIT 1"))
    return r.fetchone() is not None


async def _search_courtlistener_corpus(tenant_id: str, qv, k: int, minsim: float) -> dict:
    """ANN search over the local reference_opinions corpus. qv is a pgvector
    literal (ModernBERT-768, same space as the record-search query)."""
    async with AsyncSessionLocal() as session:
        if not await _corpus_indexed(session):
            return {"results": [],
                    "note": "CourtListener corpus not yet indexed — research goes live after tonight"}
        # statement_timeout backstop in case the planner ignores the ANN index.
        await session.execute(sa_text("SET LOCAL statement_timeout = '8000'"))
        r = await session.execute(sa_text(
            "SELECT o.id::text AS opinion_id, o.case_name, o.reporter, o.date_filed, "
            "       e.chunk_text, (1 - (e.embedding <=> CAST(:qv AS vector))) AS sim "
            "FROM reference_opinion_embeddings e "
            "JOIN reference_opinions o ON o.id = e.opinion_id "
            "ORDER BY e.embedding <=> CAST(:qv AS vector) LIMIT :k"),
            {"qv": qv, "k": k})
        out = []
        for x in r.mappings().all():
            sim = float(x["sim"])
            if sim < minsim:
                continue
            cite = " · ".join([s for s in (x["reporter"],
                              (str(x["date_filed"])[:4] if x["date_filed"] else None)) if s])
            out.append({
                "id": "res:" + x["opinion_id"],
                "source": "research", "kind": "RESEARCH",
                "opinion_id": x["opinion_id"],
                "page": None, "line": None,
                "locus": cite or "authority",
                "label": (x["case_name"] or "Authority") + ((" — " + cite) if cite else ""),
                "snippet": (x["chunk_text"] or "").strip()[:320],
                "sim": sim,
                "view_url": None})
    return {"results": out, "note": None}


async def _search_courtlistener_live(tenant_id: str, query: str, k: int) -> dict:
    """Live CourtListener API via the existing research connector."""
    try:
        from modules.drafting.research_service import research_query
        res = await research_query(query=query, connector_type="courtlistener",
                                   tenant_id=tenant_id, max_results=k)
    except Exception as e:
        logger.exception("courtlistener_live research failed")
        return {"results": [], "note": "live research unavailable: %s" % e}
    out = []
    for i, h in enumerate(getattr(res, "results", []) or []):
        # ResearchHit shape varies by connector — pull common fields defensively.
        g = (lambda *names: next((getattr(h, n) for n in names
                                  if getattr(h, n, None)), None))
        label = g("case_name", "title", "name") or "Authority"
        cite = g("citation", "reporter")
        out.append({
            "id": "res:live:%d" % i,
            "source": "research", "kind": "RESEARCH",
            "page": None, "line": None,
            "locus": cite or "authority",
            "label": label + ((" — " + cite) if cite else ""),
            "snippet": (g("snippet", "summary", "excerpt") or "")[:320],
            "sim": float(getattr(h, "score", 0.0) or 0.0),
            "view_url": g("url", "absolute_url", "download_url")})
    return {"results": out, "note": None}


async def search_research(tenant_id: str, query: str, qv, *, limit: int = 15,
                          minsim: float = 0.18, route_key: str = "default") -> dict:
    """Route a research query to the tenant's configured provider and return
    normalized record-search rows + provider/note metadata."""
    provider, _params = await resolve_route(tenant_id, route_key)
    k = max(1, min(int(limit), 50))
    try:
        if provider == "courtlistener_corpus":
            r = await _search_courtlistener_corpus(tenant_id, qv, k, minsim)
        elif provider == "courtlistener_live":
            r = await _search_courtlistener_live(tenant_id, query, k)
        else:
            r = {"results": [], "note": "research provider %r not implemented" % provider}
    except Exception as e:
        logger.exception("search_research failed (provider=%s)", provider)
        r = {"results": [], "note": "research error: %s" % e}
    r["provider"] = provider
    return r
