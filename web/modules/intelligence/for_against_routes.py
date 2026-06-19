"""for_against_routes.py -- JSON API for the reusable "Evidence For & Against" widget.

Thin async wrapper over modules.intelligence.for_against (the engine). Every heavy
call (LLM query generation + hybrid search) runs in a threadpool so the event loop
never blocks. Tenant comes from request.state (mirrors the other intelligence routes).

Entry points exposed:
  GET  /matters/{matter_id}/spines                 -- spine instances for a matter
  POST /matters/{matter_id}/build                  -- SEED: build a spine (+ optional run)
  GET  /spines/{spine_id}                           -- the two-pane matrix (groups->for/against)
  POST /spines/{spine_id}/run                       -- batch run the whole spine
  POST /spines/{spine_id}/items/{item_id}/run       -- lazy per-proposition run (on click)
  POST /analyze                                     -- right-click "AI Analyze" on selected text
  POST /similar                                     -- right-click "Find Similar" on selected text
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from typing import Optional

from modules.intelligence import for_against as fa

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/foragainst", tags=["evidence-for-against"])


def _tenant(r: Request) -> str:
    return (getattr(r.state, "tenant_id", "") or "").strip()


async def _appellate_case_for(tenant: str, matter_id: str):
    """Resolve the matter's appellate_case_id (record corpus needs it)."""
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text as sa_text
    async with AsyncSessionLocal() as s:
        r = await s.execute(sa_text(
            "SELECT id::text FROM appellate_cases WHERE matter_id=CAST(:m AS uuid) "
            "AND TRIM(tenant_id)=TRIM(:t) ORDER BY created_at LIMIT 1"),
            {"m": matter_id, "t": tenant})
        row = r.fetchone()
        return row[0] if row else None


# --------------------------------------------------------------------------- #
#  read                                                                         #
# --------------------------------------------------------------------------- #

@router.get("/matters/{matter_id}/spines")
async def matter_spines(request: Request, matter_id: str):
    out = await run_in_threadpool(fa.list_spines, _tenant(request), matter_id)
    return JSONResponse(out)


@router.get("/spines/{spine_id}")
async def spine_matrix(request: Request, spine_id: str):
    out = await run_in_threadpool(fa.read_spine, _tenant(request), spine_id)
    if out.get("error"):
        return JSONResponse(out, status_code=404)
    return JSONResponse(out)


# --------------------------------------------------------------------------- #
#  build (seed) + run                                                           #
# --------------------------------------------------------------------------- #

class BuildBody(BaseModel):
    spine_kind: str                       # pleading_coa | ffcl
    corpus: str                           # record | ediscovery
    doc_path: Optional[str] = None        # pleading or FF/CL file (litigation modes)
    appellate_case_id: Optional[str] = None
    run: bool = False                     # also run the frontier pass after building
    rebuild: bool = False                 # re-detect claims/elements (pleading_coa)
    tier: str = "cascade"


@router.post("/matters/{matter_id}/build")
async def build_spine(request: Request, matter_id: str, body: BuildBody):
    tenant = _tenant(request)
    appeal = body.appellate_case_id
    if body.corpus == "record" and not appeal:
        appeal = await _appellate_case_for(tenant, matter_id)

    if body.spine_kind == "pleading_coa":
        out = await run_in_threadpool(
            fa.build_pleading_coa_spine, tenant, matter_id, body.corpus,
            {"doc_path": body.doc_path} if body.doc_path else None, appeal, body.rebuild)
    elif body.spine_kind == "ffcl":
        if not body.doc_path:
            return JSONResponse({"error": "ffcl build requires doc_path"}, status_code=400)
        out = await run_in_threadpool(
            fa.build_ffcl_spine, tenant, matter_id, body.corpus, body.doc_path, appeal)
    else:
        return JSONResponse({"error": "unknown spine_kind"}, status_code=400)

    if out.get("error"):
        return JSONResponse(out, status_code=400)
    if body.run and out.get("spine_id"):
        run = await run_in_threadpool(fa.run_for_against, tenant, out["spine_id"], fa.SIDE_K, 0,
                                      body.tier)
        out["run"] = run
    return JSONResponse(out)


class RunBody(BaseModel):
    tier: str = "cascade"
    limit_items: int = 0


@router.post("/spines/{spine_id}/run")
async def run_spine(request: Request, spine_id: str, body: RunBody):
    out = await run_in_threadpool(fa.run_for_against, _tenant(request), spine_id, fa.SIDE_K,
                                  body.limit_items, body.tier)
    return JSONResponse(out)


@router.post("/spines/{spine_id}/items/{item_id}/run")
async def run_item(request: Request, spine_id: str, item_id: str, body: RunBody):
    out = await run_in_threadpool(fa.run_one_item, _tenant(request), item_id, body.tier)
    return JSONResponse(out)


# --------------------------------------------------------------------------- #
#  right-click modals on selected text                                          #
# --------------------------------------------------------------------------- #

class SelectionBody(BaseModel):
    matter_id: str
    corpus: str                            # record | ediscovery
    text: str
    appellate_case_id: Optional[str] = None
    tier: str = "frontier"                 # viewer Regular(sonnet) vs Frontier(opus)
    k: int = 8


@router.get("/locator")
async def evidence_locator(request: Request, kind: str, ref_id: str):
    """Pinpoint target for a piece of evidence (analytics report click)."""
    out = await run_in_threadpool(fa.evidence_locator, _tenant(request), kind, ref_id)
    return JSONResponse(out)


@router.post("/analyze")
async def analyze_selection(request: Request, body: SelectionBody):
    tenant = _tenant(request)
    appeal = body.appellate_case_id
    if body.corpus == "record" and not appeal:
        appeal = await _appellate_case_for(tenant, body.matter_id)
    if not (body.text or "").strip():
        return JSONResponse({"error": "empty selection"}, status_code=400)
    out = await run_in_threadpool(fa.analyze_text, tenant, body.matter_id, body.corpus,
                                  body.text, body.k, appeal, body.tier)
    return JSONResponse(out)


@router.post("/similar")
async def similar_selection(request: Request, body: SelectionBody):
    tenant = _tenant(request)
    appeal = body.appellate_case_id
    if body.corpus == "record" and not appeal:
        appeal = await _appellate_case_for(tenant, body.matter_id)
    if not (body.text or "").strip():
        return JSONResponse({"error": "empty selection"}, status_code=400)
    out = await run_in_threadpool(fa.find_similar, tenant, body.matter_id, body.corpus,
                                  body.text, max(body.k, 12), appeal)
    return JSONResponse(out)
