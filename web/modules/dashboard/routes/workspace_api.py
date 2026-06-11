"""
modules/dashboard/routes/workspace_api.py — Meeting Workspace REST API

CRUD for workspaces, pages, documents, participants, snapshots, sessions.
Calendar→workspace auto-creation. Template provisioning.

Routes:
  GET    /api/v1/workspaces                      — list workspaces (filter by matter, project, status)
  POST   /api/v1/workspaces                      — create workspace (from template or ad hoc)
  GET    /api/v1/workspaces/{id}                 — workspace detail with pages + docs + participants
  PATCH  /api/v1/workspaces/{id}                 — update workspace (title, status, config)
  DELETE /api/v1/workspaces/{id}                 — archive workspace

  GET    /api/v1/workspaces/{id}/pages           — list pages
  POST   /api/v1/workspaces/{id}/pages           — add page
  PATCH  /api/v1/workspaces/{id}/pages/{pid}     — update page (tldraw state, notes, etc.)
  DELETE /api/v1/workspaces/{id}/pages/{pid}     — remove page
  POST   /api/v1/workspaces/{id}/pages/{pid}/snapshot — export page to PDF/PNG

  GET    /api/v1/workspaces/{id}/documents       — list workspace documents
  POST   /api/v1/workspaces/{id}/documents       — add document ref (drag from DMS)
  DELETE /api/v1/workspaces/{id}/documents/{did}  — remove document ref

  GET    /api/v1/workspaces/{id}/participants    — list participants
  POST   /api/v1/workspaces/{id}/participants    — add participant

  POST   /api/v1/workspaces/{id}/session         — create/start meeting session
  PATCH  /api/v1/workspaces/{id}/session/{sid}   — update session (status, recording)

  GET    /api/v1/workspace-templates             — list available templates

Patent Pending — Series 1/2/3/4 — D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.workspaces")

router = APIRouter(prefix="/api/v1", tags=["workspaces"])


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _uid(request: Request) -> Optional[int]:
    user = getattr(request.state, "current_user", None)
    return user.id if user else None


# ── Pydantic models ──────────────────────────────────────────

class WorkspaceCreate(BaseModel):
    title: str
    matter_id: Optional[str] = None
    project_id: Optional[str] = None
    calendar_event_id: Optional[str] = None
    workspace_type: str = "meeting"
    template_slug: Optional[str] = None
    scheduled_at: Optional[str] = None
    description: Optional[str] = None
    config: Optional[dict] = None


class WorkspaceUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    description: Optional[str] = None
    config: Optional[dict] = None


class PageCreate(BaseModel):
    page_type: str = "whiteboard"
    title: str = "Untitled"
    sort_order: Optional[int] = None


class PageUpdate(BaseModel):
    title: Optional[str] = None
    tldraw_state: Optional[dict] = None
    content_md: Optional[str] = None
    content_json: Optional[dict] = None
    sort_order: Optional[int] = None
    is_locked: Optional[bool] = None


class DocumentAdd(BaseModel):
    document_id: Optional[str] = None
    filename: str
    storage_path: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    role: str = "reference"
    notes: Optional[str] = None


class ParticipantAdd(BaseModel):
    user_id: Optional[int] = None
    contact_id: Optional[str] = None
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: str = "participant"
    connection_type: Optional[str] = None


class SessionCreate(BaseModel):
    media_backend: str = "praesidium_native"
    external_meeting_id: Optional[str] = None
    external_passcode: Optional[str] = None
    config: Optional[dict] = None


class SessionUpdate(BaseModel):
    status: Optional[str] = None
    recording_status: Optional[str] = None
    recording_path: Optional[str] = None
    transcript_path: Optional[str] = None


# ══════════════════════════════════════════════════════════════
# WORKSPACE CRUD
# ══════════════════════════════════════════════════════════════

@router.get("/workspaces")
async def list_workspaces(
    request: Request,
    matter_id: Optional[str] = None,
    project_id: Optional[str] = None,
    status: str = "active",
    limit: int = 50,
    offset: int = 0,
):
    tid = _tid(request)
    filters = ["TRIM(w.tenant_id) = :tid"]
    params = {"tid": tid, "lim": limit, "off": offset}

    if matter_id:
        filters.append("w.matter_id = CAST(:mid AS uuid)")
        params["mid"] = matter_id
    if project_id:
        filters.append("w.project_id = CAST(:pid AS uuid)")
        params["pid"] = project_id
    if status:
        filters.append("w.status = :status")
        params["status"] = status

    where = " AND ".join(filters)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text(f"""
            SELECT w.id, w.title, w.workspace_type, w.status,
                   w.matter_id, w.project_id, w.calendar_event_id,
                   w.scheduled_at, w.started_at, w.ended_at,
                   w.created_at, w.updated_at,
                   m.matter_name, m.matter_number,
                   (SELECT COUNT(*) FROM workspace_pages wp WHERE wp.workspace_id = w.id) as page_count,
                   (SELECT COUNT(*) FROM workspace_documents wd WHERE wd.workspace_id = w.id) as doc_count,
                   (SELECT COUNT(*) FROM workspace_participants wpa WHERE wpa.workspace_id = w.id) as participant_count
            FROM meeting_workspaces w
            LEFT JOIN matters m ON w.matter_id = m.id
            WHERE {where}
            ORDER BY COALESCE(w.scheduled_at, w.created_at) DESC
            LIMIT :lim OFFSET :off
        """), params)).fetchall()

    return [dict(r._mapping) for r in rows]


@router.post("/workspaces")
async def create_workspace(request: Request, body: WorkspaceCreate):
    tid = _tid(request)
    uid = _uid(request)
    ws_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    async with AsyncSessionLocal() as db:
        # Create the workspace
        await db.execute(sa_text("""
            INSERT INTO meeting_workspaces
                (id, tenant_id, matter_id, project_id, calendar_event_id,
                 title, description, workspace_type, scheduled_at, config, created_by, created_at, updated_at)
            VALUES
                (CAST(:id AS uuid), :tid,
                 CAST(NULLIF(:mid,'') AS uuid), CAST(NULLIF(:pid,'') AS uuid), CAST(NULLIF(:ceid,'') AS uuid),
                 :title, :desc, :wtype, CAST(NULLIF(:sched,'') AS timestamptz),
                 CAST(:config AS jsonb), :uid, :now, :now)
        """), {
            "id": ws_id, "tid": tid,
            "mid": body.matter_id or "", "pid": body.project_id or "",
            "ceid": body.calendar_event_id or "",
            "title": body.title, "desc": body.description or "",
            "wtype": body.workspace_type,
            "sched": body.scheduled_at or "",
            "config": json.dumps(body.config or {}),
            "uid": uid, "now": now,
        })

        # Load template if specified, else use defaults
        default_pages = ["whiteboard", "notes", "agenda"]
        if body.template_slug:
            tmpl = (await db.execute(sa_text("""
                SELECT default_pages, widget_layout FROM workspace_templates
                WHERE template_slug = :slug AND (tenant_id IS NULL OR TRIM(tenant_id) = :tid)
                ORDER BY tenant_id DESC NULLS LAST LIMIT 1
            """), {"slug": body.template_slug, "tid": tid})).fetchone()
            if tmpl:
                default_pages = tmpl.default_pages if isinstance(tmpl.default_pages, list) else json.loads(tmpl.default_pages)

        # Create default pages
        page_type_map = {
            "whiteboard": "whiteboard", "notes": "notes", "agenda": "agenda",
            "exhibit_notes": "notes", "checklist": "checklist",
            "argument_outline": "notes", "case_theory": "whiteboard",
            "timeline": "whiteboard", "damages": "whiteboard",
            "witnesses": "notes", "closing_checklist": "checklist",
            "open_items": "checklist", "action_items": "action_items",
        }
        for i, page_key in enumerate(default_pages):
            page_id = str(uuid.uuid4())
            ptype = page_type_map.get(page_key, "notes")
            ptitle = page_key.replace("_", " ").title()
            await db.execute(sa_text("""
                INSERT INTO workspace_pages
                    (id, tenant_id, workspace_id, page_type, title, sort_order, created_by, created_at, updated_at)
                VALUES
                    (CAST(:pid AS uuid), :tid, CAST(:wsid AS uuid), :ptype, :title, :sort, :uid, :now, :now)
            """), {
                "pid": page_id, "tid": tid, "wsid": ws_id,
                "ptype": ptype, "title": ptitle, "sort": i,
                "uid": uid, "now": now,
            })

        await db.commit()

    return JSONResponse({"id": ws_id, "status": "created", "pages": len(default_pages)}, status_code=201)


@router.get("/workspaces/{workspace_id}")
async def get_workspace(request: Request, workspace_id: str):
    tid = _tid(request)

    async with AsyncSessionLocal() as db:
        ws = (await db.execute(sa_text("""
            SELECT w.*, m.matter_name, m.matter_number, p.title as project_title
            FROM meeting_workspaces w
            LEFT JOIN matters m ON w.matter_id = m.id
            LEFT JOIN projects p ON w.project_id = p.id
            WHERE w.id = CAST(:wid AS uuid) AND TRIM(w.tenant_id) = :tid
        """), {"wid": workspace_id, "tid": tid})).fetchone()

        if not ws:
            raise HTTPException(404, "Workspace not found")

        pages = (await db.execute(sa_text("""
            SELECT id, page_type, title, sort_order, is_locked, created_at, updated_at,
                   CASE WHEN tldraw_state IS NOT NULL THEN true ELSE false END as has_tldraw,
                   CASE WHEN content_md IS NOT NULL AND content_md != '' THEN true ELSE false END as has_content
            FROM workspace_pages
            WHERE workspace_id = CAST(:wid AS uuid) AND TRIM(tenant_id) = :tid
            ORDER BY sort_order
        """), {"wid": workspace_id, "tid": tid})).fetchall()

        docs = (await db.execute(sa_text("""
            SELECT wd.*, d.title as dms_title
            FROM workspace_documents wd
            LEFT JOIN documents d ON wd.document_id = d.id
            WHERE wd.workspace_id = CAST(:wid AS uuid) AND TRIM(wd.tenant_id) = :tid
            ORDER BY wd.sort_order
        """), {"wid": workspace_id, "tid": tid})).fetchall()

        participants = (await db.execute(sa_text("""
            SELECT * FROM workspace_participants
            WHERE workspace_id = CAST(:wid AS uuid) AND TRIM(tenant_id) = :tid
            ORDER BY joined_at
        """), {"wid": workspace_id, "tid": tid})).fetchall()

        sessions = (await db.execute(sa_text("""
            SELECT * FROM meeting_sessions
            WHERE workspace_id = CAST(:wid AS uuid) AND TRIM(tenant_id) = :tid
            ORDER BY created_at DESC LIMIT 5
        """), {"wid": workspace_id, "tid": tid})).fetchall()

    return {
        "workspace": dict(ws._mapping),
        "pages": [dict(r._mapping) for r in pages],
        "documents": [dict(r._mapping) for r in docs],
        "participants": [dict(r._mapping) for r in participants],
        "sessions": [dict(r._mapping) for r in sessions],
    }


@router.patch("/workspaces/{workspace_id}")
async def update_workspace(request: Request, workspace_id: str, body: WorkspaceUpdate):
    tid = _tid(request)
    sets = ["updated_at = now()"]
    params = {"wid": workspace_id, "tid": tid}

    if body.title is not None:
        sets.append("title = :title")
        params["title"] = body.title
    if body.status is not None:
        sets.append("status = :status")
        params["status"] = body.status
    if body.description is not None:
        sets.append("description = :desc")
        params["desc"] = body.description
    if body.config is not None:
        sets.append("config = CAST(:config AS jsonb)")
        params["config"] = json.dumps(body.config)

    async with AsyncSessionLocal() as db:
        await db.execute(sa_text(f"""
            UPDATE meeting_workspaces SET {', '.join(sets)}
            WHERE id = CAST(:wid AS uuid) AND TRIM(tenant_id) = :tid
        """), params)
        await db.commit()

    return {"status": "updated"}


@router.delete("/workspaces/{workspace_id}")
async def archive_workspace(request: Request, workspace_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            UPDATE meeting_workspaces SET status = 'archived', updated_at = now()
            WHERE id = CAST(:wid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"wid": workspace_id, "tid": tid})
        await db.commit()
    return {"status": "archived"}


# ══════════════════════════════════════════════════════════════
# PAGES
# ══════════════════════════════════════════════════════════════

@router.get("/workspaces/{workspace_id}/pages")
async def list_pages(request: Request, workspace_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text("""
            SELECT * FROM workspace_pages
            WHERE workspace_id = CAST(:wid AS uuid) AND TRIM(tenant_id) = :tid
            ORDER BY sort_order
        """), {"wid": workspace_id, "tid": tid})).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/workspaces/{workspace_id}/pages")
async def create_page(request: Request, workspace_id: str, body: PageCreate):
    tid = _tid(request)
    uid = _uid(request)
    page_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as db:
        # Auto sort_order if not specified
        sort = body.sort_order
        if sort is None:
            r = (await db.execute(sa_text("""
                SELECT COALESCE(MAX(sort_order), -1) + 1 as next_sort
                FROM workspace_pages
                WHERE workspace_id = CAST(:wid AS uuid) AND TRIM(tenant_id) = :tid
            """), {"wid": workspace_id, "tid": tid})).fetchone()
            sort = r.next_sort if r else 0

        await db.execute(sa_text("""
            INSERT INTO workspace_pages (id, tenant_id, workspace_id, page_type, title, sort_order, created_by, created_at, updated_at)
            VALUES (CAST(:pid AS uuid), :tid, CAST(:wid AS uuid), :ptype, :title, :sort, :uid, now(), now())
        """), {"pid": page_id, "tid": tid, "wid": workspace_id, "ptype": body.page_type, "title": body.title, "sort": sort, "uid": uid})
        await db.commit()

    return JSONResponse({"id": page_id, "page_type": body.page_type, "title": body.title}, status_code=201)


@router.get("/workspaces/{workspace_id}/pages/{page_id}")
async def get_page(request: Request, workspace_id: str, page_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        row = (await db.execute(sa_text("""
            SELECT * FROM workspace_pages
            WHERE id = CAST(:pid AS uuid) AND workspace_id = CAST(:wid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"pid": page_id, "wid": workspace_id, "tid": tid})).fetchone()
    if not row:
        raise HTTPException(404, "Page not found")
    return dict(row._mapping)


@router.patch("/workspaces/{workspace_id}/pages/{page_id}")
async def update_page(request: Request, workspace_id: str, page_id: str, body: PageUpdate):
    tid = _tid(request)
    sets = ["updated_at = now()"]
    params = {"pid": page_id, "wid": workspace_id, "tid": tid}

    if body.title is not None:
        sets.append("title = :title")
        params["title"] = body.title
    if body.tldraw_state is not None:
        sets.append("tldraw_state = CAST(:tldraw AS jsonb)")
        params["tldraw"] = json.dumps(body.tldraw_state)
    if body.content_md is not None:
        sets.append("content_md = :cmd")
        params["cmd"] = body.content_md
    if body.content_json is not None:
        sets.append("content_json = CAST(:cjson AS jsonb)")
        params["cjson"] = json.dumps(body.content_json)
    if body.sort_order is not None:
        sets.append("sort_order = :sort")
        params["sort"] = body.sort_order
    if body.is_locked is not None:
        sets.append("is_locked = :locked")
        params["locked"] = body.is_locked
        if body.is_locked:
            sets.append("locked_by = :uid")
            sets.append("locked_at = now()")
            params["uid"] = _uid(request)

    async with AsyncSessionLocal() as db:
        await db.execute(sa_text(f"""
            UPDATE workspace_pages SET {', '.join(sets)}
            WHERE id = CAST(:pid AS uuid) AND workspace_id = CAST(:wid AS uuid) AND TRIM(tenant_id) = :tid
        """), params)
        await db.commit()

    return {"status": "updated"}


@router.delete("/workspaces/{workspace_id}/pages/{page_id}")
async def delete_page(request: Request, workspace_id: str, page_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            DELETE FROM workspace_pages
            WHERE id = CAST(:pid AS uuid) AND workspace_id = CAST(:wid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"pid": page_id, "wid": workspace_id, "tid": tid})
        await db.commit()
    return {"status": "deleted"}


# ══════════════════════════════════════════════════════════════
# DOCUMENTS
# ══════════════════════════════════════════════════════════════

@router.get("/workspaces/{workspace_id}/documents")
async def list_workspace_docs(request: Request, workspace_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text("""
            SELECT wd.*, d.title as dms_title
            FROM workspace_documents wd
            LEFT JOIN documents d ON wd.document_id = d.id
            WHERE wd.workspace_id = CAST(:wid AS uuid) AND TRIM(wd.tenant_id) = :tid
            ORDER BY wd.sort_order
        """), {"wid": workspace_id, "tid": tid})).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/workspaces/{workspace_id}/documents")
async def add_workspace_doc(request: Request, workspace_id: str, body: DocumentAdd):
    tid = _tid(request)
    uid = _uid(request)
    doc_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            INSERT INTO workspace_documents
                (id, tenant_id, workspace_id, document_id, storage_path, filename, mime_type, file_size, role, notes, added_by, created_at)
            VALUES
                (CAST(:did AS uuid), :tid, CAST(:wid AS uuid),
                 CAST(NULLIF(:docid,'') AS uuid), :spath, :fname, :mime, :fsize, :role, :notes, :uid, now())
        """), {
            "did": doc_id, "tid": tid, "wid": workspace_id,
            "docid": body.document_id or "", "spath": body.storage_path,
            "fname": body.filename, "mime": body.mime_type, "fsize": body.file_size,
            "role": body.role, "notes": body.notes, "uid": uid,
        })
        await db.commit()

    return JSONResponse({"id": doc_id}, status_code=201)


@router.delete("/workspaces/{workspace_id}/documents/{doc_ref_id}")
async def remove_workspace_doc(request: Request, workspace_id: str, doc_ref_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            DELETE FROM workspace_documents
            WHERE id = CAST(:did AS uuid) AND workspace_id = CAST(:wid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"did": doc_ref_id, "wid": workspace_id, "tid": tid})
        await db.commit()
    return {"status": "removed"}


# ══════════════════════════════════════════════════════════════
# PARTICIPANTS
# ══════════════════════════════════════════════════════════════

@router.get("/workspaces/{workspace_id}/participants")
async def list_participants(request: Request, workspace_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text("""
            SELECT * FROM workspace_participants
            WHERE workspace_id = CAST(:wid AS uuid) AND TRIM(tenant_id) = :tid
            ORDER BY joined_at NULLS LAST
        """), {"wid": workspace_id, "tid": tid})).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/workspaces/{workspace_id}/participants")
async def add_participant(request: Request, workspace_id: str, body: ParticipantAdd):
    tid = _tid(request)
    pid = str(uuid.uuid4())

    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            INSERT INTO workspace_participants
                (id, tenant_id, workspace_id, user_id, contact_id, display_name, email, role, connection_type, joined_at, created_at)
            VALUES
                (CAST(:pid AS uuid), :tid, CAST(:wid AS uuid),
                 :uid, CAST(NULLIF(:cid,'') AS uuid), :dname, :email, :role, :ctype, now(), now())
        """), {
            "pid": pid, "tid": tid, "wid": workspace_id,
            "uid": body.user_id, "cid": body.contact_id or "",
            "dname": body.display_name, "email": body.email,
            "role": body.role, "ctype": body.connection_type,
        })
        await db.commit()

    return JSONResponse({"id": pid}, status_code=201)


# ══════════════════════════════════════════════════════════════
# MEETING SESSIONS
# ══════════════════════════════════════════════════════════════

@router.post("/workspaces/{workspace_id}/session")
async def create_session(request: Request, workspace_id: str, body: SessionCreate):
    tid = _tid(request)
    sid = str(uuid.uuid4())
    room_name = f"ws-{workspace_id[:8]}-{sid[:8]}" if body.media_backend == "praesidium_native" else None

    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            INSERT INTO meeting_sessions
                (id, tenant_id, workspace_id, media_backend, livekit_room_name,
                 external_meeting_id, external_passcode, config, status, created_at, updated_at)
            VALUES
                (CAST(:sid AS uuid), :tid, CAST(:wid AS uuid), :backend, :room,
                 :extid, :extpass, CAST(:config AS jsonb), 'created', now(), now())
        """), {
            "sid": sid, "tid": tid, "wid": workspace_id,
            "backend": body.media_backend, "room": room_name,
            "extid": body.external_meeting_id, "extpass": body.external_passcode,
            "config": json.dumps(body.config or {}),
        })
        await db.commit()

    return JSONResponse({"id": sid, "livekit_room_name": room_name, "media_backend": body.media_backend}, status_code=201)


@router.patch("/workspaces/{workspace_id}/session/{session_id}")
async def update_session(request: Request, workspace_id: str, session_id: str, body: SessionUpdate):
    tid = _tid(request)
    sets = ["updated_at = now()"]
    params = {"sid": session_id, "wid": workspace_id, "tid": tid}

    if body.status is not None:
        sets.append("status = :status")
        params["status"] = body.status
        if body.status == "active":
            sets.append("started_at = COALESCE(started_at, now())")
        elif body.status == "ended":
            sets.append("ended_at = now()")
    if body.recording_status is not None:
        sets.append("recording_status = :rstatus")
        params["rstatus"] = body.recording_status
    if body.recording_path is not None:
        sets.append("recording_path = :rpath")
        params["rpath"] = body.recording_path
    if body.transcript_path is not None:
        sets.append("transcript_path = :tpath")
        params["tpath"] = body.transcript_path

    async with AsyncSessionLocal() as db:
        await db.execute(sa_text(f"""
            UPDATE meeting_sessions SET {', '.join(sets)}
            WHERE id = CAST(:sid AS uuid) AND workspace_id = CAST(:wid AS uuid) AND TRIM(tenant_id) = :tid
        """), params)
        await db.commit()

    return {"status": "updated"}


# ══════════════════════════════════════════════════════════════
# TEMPLATES
# ══════════════════════════════════════════════════════════════

@router.get("/workspace-templates")
async def list_templates(request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text("""
            SELECT id, template_slug, template_name, description, workspace_type,
                   widget_layout, default_pages, is_platform_standard
            FROM workspace_templates
            WHERE tenant_id IS NULL OR TRIM(tenant_id) = :tid
            ORDER BY is_platform_standard DESC, template_name
        """), {"tid": tid})).fetchall()
    return [dict(r._mapping) for r in rows]
