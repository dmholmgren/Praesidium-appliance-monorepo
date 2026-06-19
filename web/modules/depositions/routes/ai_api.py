"""
modules/depositions/routes/ai_api.py
Deposition AI layer (scope §9), headline feature first: element-driven clip
suggestion. Take a pleaded claim's elements (coa_elements.element_name -- the
Layer-2 exemplars) -> embed -> vector + retrieve over transcript_qa_embeddings ->
rank Q&A units bearing on each element -> return page:line-ready suggested clips.

Same retrieval engine as pleading-informed evidence suggestion, pointed at
testimony. Gated (§9): runs on an explicit action over a bounded set (one matter
/ one transcript / one cause), never a bulk pass. Embedding the elements is the
only model touch and it is cheap + local (praesidium-embed). Suggestions only --
the designation a user creates from a suggestion stays deterministic (U5).
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant, _serialize

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/depositions", tags=["depositions-ai-api"])


class SuggestBody(BaseModel):
    matter_id: str
    transcript_id: Optional[str] = None    # scope to one transcript (else whole matter)
    cause_id: Optional[str] = None         # focus one pleaded claim (else all)
    per_element: int = 3                   # top-K Q&A units per element
    min_sim: float = 0.25                  # drop weak matches


def _embed_many_sync(texts):
    from modules.depositions.jobs.embed_qa import _embed, EMBED_URL_DEFAULT, _vec_literal
    vecs, _m, _r = _embed(EMBED_URL_DEFAULT, texts)
    return [_vec_literal(v) for v in vecs]


@router.post("/suggest-clips")
async def suggest_clips(request: Request, body: SuggestBody,
                        user=Depends(get_current_user)):
    """Element-driven clip suggestion. Returns suggested page:line spans per
    pleaded element, ranked by similarity over the Q&A index."""
    tid = _tenant(request)
    per = max(1, min(body.per_element, 10))
    try:
        async with AsyncSessionLocal() as session:
            # 1. load the matter's pleaded elements (optionally one cause)
            where = ["TRIM(co.tenant_id) = TRIM(:tid)", "co.matter_id = CAST(:mid AS uuid)"]
            params = {"tid": tid, "mid": body.matter_id}
            if body.cause_id:
                where.append("co.id = CAST(:cid AS uuid)")
                params["cid"] = body.cause_id
            r = await session.execute(sa_text(
                "SELECT co.id::text AS cause_id, co.title, co.count_number, "
                "       e.id::text AS element_id, e.element_name "
                "FROM causes_of_action co "
                "JOIN coa_elements e ON e.cause_of_action_id = co.id "
                "WHERE " + " AND ".join(where) +
                "  AND e.element_name IS NOT NULL "
                "ORDER BY co.count_number NULLS LAST, e.created_at"), params)
            elements = [dict(x) for x in r.mappings().fetchall()]
            if not elements:
                return JSONResponse({"causes": [], "note": "no pleaded elements for this matter"})

            # 2. embed every element statement in one batched call
            texts = [e["element_name"] for e in elements]
            vecs = await asyncio.get_event_loop().run_in_executor(None, _embed_many_sync, texts)

            # 3. per element: kNN over the Q&A index (best chunk per unit), scoped
            scope = ["TRIM(emb.tenant_id) = TRIM(:tid)"]
            sparams = {"tid": tid}
            if body.transcript_id:
                scope.append("emb.transcript_id = CAST(:trid AS uuid)")
                sparams["trid"] = body.transcript_id
            else:
                scope.append("t.matter_id = CAST(:mid AS uuid)")
                sparams["mid"] = body.matter_id
            knn_sql = (
                "WITH ranked AS ("
                "  SELECT emb.qa_unit_id AS qa_id, "
                "         min(emb.embedding <=> CAST(:qv AS vector)) AS dist "
                "  FROM transcript_qa_embeddings emb "
                "  JOIN deposition_transcripts t ON t.id = emb.transcript_id "
                "  WHERE " + " AND ".join(scope) +
                "  GROUP BY emb.qa_unit_id) "
                "SELECT q.id::text, q.transcript_id::text, q.q_start_page, q.q_start_line, "
                "       q.a_end_page, q.a_end_line, q.question_text, q.answer_text, "
                "       q.is_colloquy, t.deponent, (1 - r.dist) AS sim "
                "FROM ranked r "
                "JOIN transcript_qa_units q ON q.id = r.qa_id "
                "JOIN deposition_transcripts t ON t.id = q.transcript_id "
                "WHERE (1 - r.dist) >= :minsim "
                "ORDER BY r.dist LIMIT :per")

            by_cause = {}
            for el, qv in zip(elements, vecs):
                qp = dict(sparams); qp.update({"qv": qv, "per": per, "minsim": body.min_sim})
                rr = await session.execute(sa_text(knn_sql), qp)
                hits = [dict(x) for x in rr.mappings().fetchall()]
                c = by_cause.setdefault(el["cause_id"], {
                    "cause_id": el["cause_id"], "title": el["title"],
                    "count_number": el["count_number"], "elements": []})
                c["elements"].append({
                    "element_id": el["element_id"],
                    "element_name": el["element_name"],
                    "suggestions": hits})
        return JSONResponse(_serialize({"causes": list(by_cause.values())}))
    except Exception as e:
        logger.exception("suggest_clips failed")
        return JSONResponse({"error": str(e)}, 500)
