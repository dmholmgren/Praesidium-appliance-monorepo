"""calendar_classification_routes.py — API for the calendar event typing +
matter-link resolver (Court/Hearing build, Step 2, Gap #3).

  POST /api/v1/calendar-classify/run                         — batch
  GET  /api/v1/calendar-classify/event/{event_id}            — typing + link + history
  POST /api/v1/calendar-classify/event/{event_id}/correct-type   {event_type}
  POST /api/v1/calendar-classify/event/{event_id}/correct-matter {matter_id}

Corrections are sticky (manual typing / assigned_by='manual') and protected from
automated re-runs, same invariant as the document classifier.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant, _serialize
from modules.intelligence import calendar_classifier as cc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/calendar-classify", tags=["calendar-classify"])


def _uid(user):
    if user is None:
        return None
    return user.get("id") if isinstance(user, dict) else getattr(user, "id", None)


class RunBody(BaseModel):
    limit: int = 1000
    use_ai: bool = False
    all: bool = False
    types_only: bool = False
    link_only: bool = False


class TypeBody(BaseModel):
    event_type: str


class MatterBody(BaseModel):
    matter_id: str


@router.post("/run")
async def run(request: Request, body: RunBody, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        out = await cc.classify_calendar(
            tid, do_types=not body.link_only, do_link=not body.types_only,
            only_new=not body.all, limit=body.limit, use_ai=body.use_ai,
            user_id=_uid(user))
        return JSONResponse(_serialize(out))
    except Exception as e:
        logger.exception("calendar classify run failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/event/{event_id}")
async def event(request: Request, event_id: str, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(await cc.get_event(tid, event_id)))
    except Exception as e:
        logger.exception("get_event failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/event/{event_id}/correct-type")
async def correct_type(request: Request, event_id: str, body: TypeBody,
                       user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(
            await cc.correct_event_type(tid, event_id, body.event_type, _uid(user))))
    except Exception as e:
        logger.exception("correct_event_type failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/event/{event_id}/correct-matter")
async def correct_matter(request: Request, event_id: str, body: MatterBody,
                         user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        return JSONResponse(_serialize(
            await cc.correct_matter(tid, event_id, body.matter_id, _uid(user))))
    except Exception as e:
        logger.exception("correct_matter failed")
        return JSONResponse({"error": str(e)}, 500)
