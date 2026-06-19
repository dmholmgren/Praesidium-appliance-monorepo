"""routing_routes.py — API for forward-loop routing + the alert inbox.

  POST /api/v1/routing/triage                          — auto-route or raise alerts
  GET  /api/v1/routing/alerts                           — the inbox (snoozed return)
  POST /api/v1/routing/alert/{id}/snooze   {days}       — silence; reappears after
  POST /api/v1/routing/alert/{id}/dismiss  {reason}     — affirmative no-action (stays gone)
  POST /api/v1/routing/alert/{id}/route    {matter_id,target}
  GET  /api/v1/routing/matter/{matter_id}/motions       — motion workspaces
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant, _serialize
from modules.intelligence import routing as rt

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/routing", tags=["routing"])


def _uid(user):
    if user is None:
        return None
    return user.get("id") if isinstance(user, dict) else getattr(user, "id", None)


class TriageBody(BaseModel):
    matter_id: Optional[str] = None


class SnoozeBody(BaseModel):
    days: int = 1


class DismissBody(BaseModel):
    reason: Optional[str] = None


class RouteBody(BaseModel):
    matter_id: Optional[str] = None
    target: Optional[str] = None


@router.post("/triage")
async def triage(request: Request, body: TriageBody, user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await rt.triage(_tenant(request), matter_id=body.matter_id)))
    except Exception as e:
        logger.exception("triage failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/alerts")
async def alerts(request: Request, matter_id: Optional[str] = None,
                 include_resolved: bool = False, user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await rt.surface_alerts(
            _tenant(request), matter_id=matter_id, include_resolved=include_resolved)))
    except Exception as e:
        logger.exception("alerts failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/alert/{alert_id}/snooze")
async def snooze(request: Request, alert_id: str, body: SnoozeBody,
                 user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await rt.snooze_alert(
            _tenant(request), alert_id, body.days, actor=_uid(user))))
    except Exception as e:
        logger.exception("snooze failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/alert/{alert_id}/dismiss")
async def dismiss(request: Request, alert_id: str, body: DismissBody,
                  user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await rt.dismiss_alert(
            _tenant(request), alert_id, body.reason, actor=_uid(user))))
    except Exception as e:
        logger.exception("dismiss failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/alert/{alert_id}/route")
async def route(request: Request, alert_id: str, body: RouteBody,
                user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await rt.route_alert(
            _tenant(request), alert_id, matter_id=body.matter_id,
            target=body.target, actor=_uid(user))))
    except Exception as e:
        logger.exception("route failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/matter/{matter_id}/motions")
async def motions(request: Request, matter_id: str, user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await rt.list_motion_workspaces(
            _tenant(request), matter_id)))
    except Exception as e:
        logger.exception("motions failed")
        return JSONResponse({"error": str(e)}, 500)
