"""calendar_lifecycle_routes.py — API for the calendar event lifecycle.

  GET  /api/v1/calendar-lifecycle/reasons                     — guided remove reasons
  POST /api/v1/calendar-lifecycle/match                       — match-on-add prompt
  POST /api/v1/calendar-lifecycle/task                        — "just create a task"
  POST /api/v1/calendar-lifecycle/event/{id}/remove          — soft (default) / hard
  POST /api/v1/calendar-lifecycle/event/{id}/reinstate
  POST /api/v1/calendar-lifecycle/event/{id}/reschedule
  POST /api/v1/calendar-lifecycle/event/{id}/documents       — link a document
  POST /api/v1/calendar-lifecycle/event/{id}/documents/waive — will_add_later | none
  POST /api/v1/calendar-lifecycle/event/{id}/task            — spawn backing task
  GET  /api/v1/calendar-lifecycle/event/{id}/workspace       — dispatch to workspace
  GET  /api/v1/calendar-lifecycle/event/{id}/changes         — change log
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant, _serialize
from modules.intelligence import calendar_lifecycle as cl

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/calendar-lifecycle", tags=["calendar-lifecycle"])


def _uid(user):
    if user is None:
        return None
    return user.get("id") if isinstance(user, dict) else getattr(user, "id", None)


class RemoveBody(BaseModel):
    reason_code: str
    reason_note: Optional[str] = None
    hard: bool = False


class RescheduleBody(BaseModel):
    new_start_at: str
    new_end_at: Optional[str] = None
    reason_code: str = "reset"
    reason_note: Optional[str] = None


class MatchBody(BaseModel):
    subject: str
    start_at: Optional[str] = None
    matter_id: Optional[str] = None


class DocBody(BaseModel):
    document_id: str
    role: Optional[str] = None


class WaiveBody(BaseModel):
    mode: str  # will_add_later | none


class TaskBody(BaseModel):
    title: Optional[str] = None
    task_type: Optional[str] = None
    due_date: Optional[str] = None
    matter_id: Optional[str] = None


def _ok(coro):
    return coro


@router.get("/reasons")
async def reasons(request: Request, user=Depends(get_current_user)):
    return JSONResponse(_serialize(await cl.list_reasons(_tenant(request))))


@router.post("/match")
async def match(request: Request, body: MatchBody, user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await cl.find_matches(
            _tenant(request), body.subject, body.start_at, body.matter_id)))
    except Exception as e:
        logger.exception("match failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/task")
async def task_only(request: Request, body: TaskBody, user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await cl.create_task_only(
            _tenant(request), title=body.title or "Task",
            task_type=body.task_type or "general", due_date=body.due_date,
            matter_id=body.matter_id, actor=_uid(user))))
    except Exception as e:
        logger.exception("task_only failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/event/{event_id}/remove")
async def remove(request: Request, event_id: str, body: RemoveBody,
                 user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await cl.remove_event(
            _tenant(request), event_id, body.reason_code, body.reason_note,
            hard=body.hard, actor=_uid(user))))
    except Exception as e:
        logger.exception("remove failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/event/{event_id}/reinstate")
async def reinstate(request: Request, event_id: str, user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await cl.reinstate_event(
            _tenant(request), event_id, actor=_uid(user))))
    except Exception as e:
        logger.exception("reinstate failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/event/{event_id}/reschedule")
async def reschedule(request: Request, event_id: str, body: RescheduleBody,
                     user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await cl.reschedule_event(
            _tenant(request), event_id, body.new_start_at, body.new_end_at,
            body.reason_code, body.reason_note, actor=_uid(user))))
    except Exception as e:
        logger.exception("reschedule failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/event/{event_id}/documents")
async def link_doc(request: Request, event_id: str, body: DocBody,
                   user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await cl.link_document(
            _tenant(request), event_id, body.document_id, body.role,
            actor=_uid(user))))
    except Exception as e:
        logger.exception("link_doc failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/event/{event_id}/documents/waive")
async def waive(request: Request, event_id: str, body: WaiveBody,
                user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await cl.waive_documents(
            _tenant(request), event_id, body.mode, actor=_uid(user))))
    except Exception as e:
        logger.exception("waive failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/event/{event_id}/task")
async def spawn_task(request: Request, event_id: str, body: TaskBody,
                     user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await cl.spawn_task(
            _tenant(request), event_id, title=body.title,
            task_type=body.task_type, due_date=body.due_date, actor=_uid(user))))
    except Exception as e:
        logger.exception("spawn_task failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/event/{event_id}/workspace")
async def workspace(request: Request, event_id: str, user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await cl.resolve_workspace(
            _tenant(request), event_id)))
    except Exception as e:
        logger.exception("workspace failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/event/{event_id}/changes")
async def changes(request: Request, event_id: str, user=Depends(get_current_user)):
    try:
        return JSONResponse(_serialize(await cl.change_log(_tenant(request), event_id)))
    except Exception as e:
        logger.exception("changes failed")
        return JSONResponse({"error": str(e)}, 500)
