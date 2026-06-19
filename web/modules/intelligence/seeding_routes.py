"""seeding_routes.py — API for AI-guided backfill seeding (Court/Hearing 6/10).

  POST /api/v1/seeding/matter/{matter_id}/collect    — run the 3 witnesses -> ledger
  GET  /api/v1/seeding/matter/{matter_id}/proposals  — cluster -> confirmable set
  POST /api/v1/seeding/matter/{matter_id}/narrate    — LLM narrates + flags weak
  POST /api/v1/seeding/matter/{matter_id}/revise     — {note} cheap re-narrate
  POST /api/v1/seeding/matter/{matter_id}/ratify     — {decisions} commit + lock
  GET  /api/v1/seeding/matter/{matter_id}/session    — current live session
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant, _serialize
from modules.intelligence import seeding_session as ss

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/seeding", tags=["seeding"])


def _uid(user):
    if user is None:
        return None
    return user.get("id") if isinstance(user, dict) else getattr(user, "id", None)


class NoteBody(BaseModel):
    note: str


class RatifyBody(BaseModel):
    decisions: dict


@router.post("/matter/{matter_id}/collect")
async def collect(request: Request, matter_id: str, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(await ss.collect(tid, matter_id)))
    except Exception as e:
        logger.exception("seed collect failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/matter/{matter_id}/proposals")
async def proposals(request: Request, matter_id: str, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(await ss.build_proposals(tid, matter_id)))
    except Exception as e:
        logger.exception("seed proposals failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/matter/{matter_id}/narrate")
async def narrate(request: Request, matter_id: str, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(await ss.narrate(tid, matter_id)))
    except Exception as e:
        logger.exception("seed narrate failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/matter/{matter_id}/revise")
async def revise(request: Request, matter_id: str, body: NoteBody,
                 user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(await ss.revise(tid, matter_id, body.note)))
    except Exception as e:
        logger.exception("seed revise failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/matter/{matter_id}/ratify")
async def ratify(request: Request, matter_id: str, body: RatifyBody,
                 user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(
            await ss.ratify(tid, matter_id, body.decisions, user_id=_uid(user))))
    except Exception as e:
        logger.exception("seed ratify failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/matter/{matter_id}/session")
async def session(request: Request, matter_id: str, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(await ss.get_session(tid, matter_id)))
    except Exception as e:
        logger.exception("seed session failed")
        return JSONResponse({"error": str(e)}, 500)
