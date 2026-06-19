"""reconciliation_routes.py — API for the reconciliation resolver (§4.3).

  POST /api/v1/reconcile/run                              — reconstruct hearings
  GET  /api/v1/reconcile/matter/{matter_id}/hearings      — hearings for a matter
  GET  /api/v1/reconcile/hearing/{hearing_id}             — hearing + reschedules + signals
  POST /api/v1/reconcile/hearing/{hearing_id}/confirm     — lock (attorney-confirmed)
  POST /api/v1/reconcile/hearing/{hearing_id}/correct     — edit + lock (sticky)
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant, _serialize
from modules.intelligence import reconciliation_resolver as rr

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/reconcile", tags=["reconcile"])


def _uid(user):
    if user is None:
        return None
    return user.get("id") if isinstance(user, dict) else getattr(user, "id", None)


class RunBody(BaseModel):
    matter_id: Optional[str] = None


class CorrectBody(BaseModel):
    hearing_type: Optional[str] = None
    judge: Optional[str] = None
    courtroom: Optional[str] = None
    outcome: Optional[str] = None
    status: Optional[str] = None
    notice_status: Optional[str] = None
    current_start_at: Optional[str] = None
    original_start_at: Optional[str] = None


@router.post("/run")
async def run(request: Request, body: RunBody, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(await rr.reconcile(tid, matter_id=body.matter_id)))
    except Exception as e:
        logger.exception("reconcile run failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/matter/{matter_id}/hearings")
async def matter_hearings(request: Request, matter_id: str,
                          user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(await rr.list_hearings(tid, matter_id)))
    except Exception as e:
        logger.exception("list_hearings failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/hearing/{hearing_id}")
async def hearing(request: Request, hearing_id: str, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(await rr.get_hearing(tid, hearing_id)))
    except Exception as e:
        logger.exception("get_hearing failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/hearing/{hearing_id}/confirm")
async def confirm(request: Request, hearing_id: str, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(
            await rr.confirm_hearing(tid, hearing_id, _uid(user))))
    except Exception as e:
        logger.exception("confirm_hearing failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/hearing/{hearing_id}/correct")
async def correct(request: Request, hearing_id: str, body: CorrectBody,
                  user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(
            await rr.correct_hearing(tid, hearing_id, body.dict(), _uid(user))))
    except Exception as e:
        logger.exception("correct_hearing failed")
        return JSONResponse({"error": str(e)}, 500)
