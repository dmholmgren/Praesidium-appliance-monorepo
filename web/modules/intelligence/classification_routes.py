"""classification_routes.py — API for the persisted document classifier
(Court/Hearing build, Step 2 shared-core item #1).

Thin HTTP layer over modules.intelligence.document_classifier:
  POST /api/v1/classification/run                    — classify a batch
  GET  /api/v1/classification/document/{doc_id}      — live decision + history
  POST /api/v1/classification/document/{doc_id}/correct — human correction

Surfaces (Trial Center pleadings/motions, the hearing classifier, the matter
drill-down) read classification_results directly; this router is the write +
review path. Corrections are method='manual' and protected from re-runs.
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant, _serialize
from modules.intelligence import document_classifier as dc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/classification", tags=["classification"])


def _uid(user):
    if user is None:
        return None
    return user.get("id") if isinstance(user, dict) else getattr(user, "id", None)


class RunBody(BaseModel):
    matter_id: Optional[str] = None
    doc_ids: Optional[List[str]] = None
    limit: int = 200
    use_ai: bool = False
    all: bool = False          # reclassify already-classified docs too


class CorrectBody(BaseModel):
    code: str                  # document_type_taxonomy.code


@router.post("/run")
async def run(request: Request, body: RunBody, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        out = await dc.classify_documents(
            tid, matter_id=body.matter_id, doc_ids=body.doc_ids,
            only_unclassified=not body.all, limit=body.limit,
            use_ai=body.use_ai, user_id=_uid(user))
        return JSONResponse(_serialize(out))
    except Exception as e:
        logger.exception("classification run failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/document/{doc_id}")
async def document(request: Request, doc_id: str, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(await dc.get_classification(tid, doc_id)))
    except Exception as e:
        logger.exception("get_classification failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/document/{doc_id}/correct")
async def correct(request: Request, doc_id: str, body: CorrectBody,
                  user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        out = await dc.correct_classification(tid, doc_id, body.code, _uid(user))
        return JSONResponse(_serialize(out))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("correct_classification failed")
        return JSONResponse({"error": str(e)}, 500)
