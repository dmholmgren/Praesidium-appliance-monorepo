"""
Page-Raster Service — HTTP surface (Spec v1.0, Slice PR-1).

PR-1 ships the authenticated lazy-fetch endpoint only. Token-scoped trial-display
fetch (PR-3) and prewarm/RQ (PR-2) are separate slices.

Authz contract mirrors /api/review/source-pdf: authenticated session + tenant
scope (tenant enforcement happens inside the resolver via TRIM(tenant_id)=tid).
The route is unmapped in the external-role module guard, so it is an internal
attorney/staff tool — not reachable by client / co_counsel projection roles.
"""

import asyncio
import logging

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from modules.render.page_raster import (
    render_page, RasterResult, SUPPORTED_CORPORA, DEFAULT_WIDTH, clamp_width, gc_sweep,
)

log = logging.getLogger("praesidium.render.page_raster")

router = APIRouter(prefix="/api/v1/render", tags=["page-raster"])


def _require_tenant(request: Request) -> str:
    if not getattr(request.state, "current_user", None):
        raise HTTPException(401, "Not authenticated")
    tid = (getattr(request.state, "tenant_id", "") or "").strip()
    if not tid:
        raise HTTPException(401, "Tenant not resolved")
    return tid


@router.get("/page/{corpus}/{doc_id}/{page}")
async def render_page_endpoint(request: Request, corpus: str, doc_id: str,
                               page: int, rendition: str = "native_pdf",
                               w: int = DEFAULT_WIDTH):
    """Lazy page raster, tenant-scoped. Cache hit -> stream WebP; miss -> render,
    cache, stream. X-Page-Dims lets the client scale percent/0-1 overlays."""
    tid = _require_tenant(request)
    if corpus not in SUPPORTED_CORPORA:
        raise HTTPException(400, "Unsupported corpus '%s'" % corpus)
    if page < 1:
        raise HTTPException(400, "page must be >= 1")
    try:
        res: RasterResult = await run_in_threadpool(
            render_page, tid, corpus, doc_id, page, rendition, w)
    except ValueError:
        raise HTTPException(404, "Page out of range")
    except Exception:
        log.exception("page raster failed: %s/%s p%s", corpus, doc_id, page)
        raise HTTPException(500, "Render failed")
    if res is None:
        raise HTTPException(404, "Document not found or not renderable")
    if not res.cached:
        _maybe_gc()
    return Response(
        content=res.image_bytes,
        media_type="image/webp",
        headers={
            "Cache-Control": "private, max-age=86400, immutable",
            "X-Page-Dims": "%dx%d" % (res.width, res.height),
            "X-Page-Count": str(res.page_count or ""),
            "X-Cache": "HIT" if res.cached else "MISS",
        },
    )


# ── Prewarm / read-ahead ─────────────────────────────────────────────────────
# Bounded in-process background warm: render the requested (doc, page) cells into
# the shared cache so the next page/doc a reviewer opens is an instant HIT. Best-
# effort and fire-and-forget — the request returns immediately. UPGRADE PATH: swap
# the in-process warmer for Redis/RQ jobs (same request contract) once the worker
# fleet mounts /datapool/cache/page_raster; until then this avoids a fleet recreate.
_PREWARM_CONCURRENCY = 3
_PREWARM_MAX_CELLS = 80
_PREWARM_SEM = asyncio.Semaphore(_PREWARM_CONCURRENCY)


@router.post("/prewarm")
async def prewarm(request: Request):
    """Schedule background page renders. Body: {corpus, doc_ids:[...] | items:[{doc_id,pages}],
    pages?:[1], rendition?, width?}. Returns {scheduled} (cells queued, after cap)."""
    tid = _require_tenant(request)
    body = await request.json()
    corpus = body.get("corpus")
    if corpus not in SUPPORTED_CORPORA:
        raise HTTPException(400, "Unsupported corpus '%s'" % corpus)
    width = clamp_width(body.get("width") or DEFAULT_WIDTH)
    rendition = body.get("rendition") or "native_pdf"
    default_pages = body.get("pages") or [1]

    cells = []
    if body.get("items"):
        for it in body["items"]:
            d = it.get("doc_id")
            for pg in (it.get("pages") or default_pages):
                cells.append((d, pg))
    else:
        for d in (body.get("doc_ids") or []):
            for pg in default_pages:
                cells.append((d, pg))
    seen, jobs = set(), []
    for d, pg in cells:
        try:
            pg = int(pg)
        except (TypeError, ValueError):
            continue
        if not d or pg < 1 or (d, pg) in seen:
            continue
        seen.add((d, pg))
        jobs.append((d, pg))
        if len(jobs) >= _PREWARM_MAX_CELLS:
            break

    async def _one(d, pg):
        async with _PREWARM_SEM:
            try:
                await run_in_threadpool(render_page, tid, corpus, d, pg, rendition, width)
            except Exception:
                pass  # best-effort; a cold page just renders on first real view

    async def _warm():
        try:
            await asyncio.gather(*[_one(d, pg) for d, pg in jobs])
        except Exception:
            log.exception("prewarm batch failed")

    if jobs:
        asyncio.create_task(_warm())
    return {"scheduled": len(jobs)}


# ── Cache GC ──────────────────────────────────────────────────────────────────
# Self-maintaining: every _GC_EVERY fresh renders, kick a single background LRU
# sweep (single-flight). Plus a manual ops endpoint. Per-doc stale-version pruning
# happens inline in render_page() on each re-render.
_GC_EVERY = 400
_render_since_gc = 0
_gc_running = False


def _maybe_gc():
    global _render_since_gc, _gc_running
    _render_since_gc += 1
    if _gc_running or _render_since_gc < _GC_EVERY:
        return
    _render_since_gc = 0
    _gc_running = True

    async def _run():
        global _gc_running
        try:
            stats = await run_in_threadpool(gc_sweep)
            if stats.get("deleted_files"):
                log.info("page-raster GC: freed %d files / %d bytes (total was %d)",
                         stats["deleted_files"], stats["freed_bytes"], stats["total_bytes"])
        except Exception:
            log.exception("background GC failed")
        finally:
            _gc_running = False

    asyncio.create_task(_run())


@router.post("/gc")
async def gc(request: Request):
    """Run an LRU cache sweep now. Body: {max_gb?}. Authenticated ops endpoint;
    the cache is regenerable so eviction is safe. Returns sweep stats."""
    _require_tenant(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    max_gb = body.get("max_gb")
    max_bytes = int(float(max_gb) * (1024 ** 3)) if max_gb else None
    stats = await run_in_threadpool(gc_sweep, max_bytes)
    return stats
