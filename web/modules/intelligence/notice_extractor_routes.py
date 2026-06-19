"""notice_extractor_routes.py — API for the notice/order extractor (§4.2).

  POST /api/v1/notice-extract/run                       — batch over classified court docs
  GET  /api/v1/notice-extract/document/{doc_id}         — live extraction + signals + history
  POST /api/v1/notice-extract/document/{doc_id}/correct — human correction (sticky)
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant, _serialize
from modules.intelligence import notice_extractor as nx

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/notice-extract", tags=["notice-extract"])


def _uid(user):
    if user is None:
        return None
    return user.get("id") if isinstance(user, dict) else getattr(user, "id", None)


class RunBody(BaseModel):
    matter_id: Optional[str] = None
    doc_ids: Optional[List[str]] = None
    limit: int = 200
    use_ai: bool = False
    all: bool = False


class CorrectBody(BaseModel):
    hearing_type: Optional[str] = None
    new_date: Optional[str] = None
    prior_date: Optional[str] = None
    is_reset: Optional[bool] = None
    moving_party: Optional[str] = None
    court: Optional[str] = None
    judge: Optional[str] = None
    cause_number: Optional[str] = None


@router.post("/run")
async def run(request: Request, body: RunBody, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        out = await nx.extract_notices(
            tid, matter_id=body.matter_id, doc_ids=body.doc_ids,
            only_new=not body.all, limit=body.limit, use_ai=body.use_ai,
            user_id=_uid(user))
        return JSONResponse(_serialize(out))
    except Exception as e:
        logger.exception("notice extract run failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/document/{doc_id}")
async def document(request: Request, doc_id: str, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(await nx.get_extraction(tid, doc_id)))
    except Exception as e:
        logger.exception("get_extraction failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/document/{doc_id}/correct")
async def correct(request: Request, doc_id: str, body: CorrectBody,
                  user=Depends(get_current_user)):
    tid = _tenant(request)
    patch = {k: v for k, v in body.dict().items() if v is not None}
    try:
        out = await nx.correct_extraction(tid, doc_id, patch, _uid(user))
        return JSONResponse(_serialize(out))
    except Exception as e:
        logger.exception("correct_extraction failed")
        return JSONResponse({"error": str(e)}, 500)
