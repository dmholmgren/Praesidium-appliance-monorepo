"""
Stream Deck Context Broadcast API.

Endpoints for Stream Deck plugin communication:
- SSE event stream for real-time context push
- Context publishing from browser
- Template CRUD
- Device registration and config
- Review-and-advance composite endpoint
"""

import json
import hashlib
import secrets
import asyncio
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text

from core.db.base import AsyncSessionLocal


router = APIRouter(prefix="/api/v1/streamdeck", tags=["streamdeck"])


# ── Pydantic Models ──────────────────────────────────────────────────────────

class ContextUpdate(BaseModel):
    context_type: str  # ediscovery_review, billing, dms, global
    payload: dict = {}

class RegisterDevice(BaseModel):
    device_type: str = "mk2_15"
    device_serial: Optional[str] = None

class ConfigUpdate(BaseModel):
    context_assignments: Optional[dict] = None
    pinned_tags: Optional[dict] = None
    auto_advance: Optional[bool] = None

class TemplateCreate(BaseModel):
    template_slug: str
    display_name: str
    context_type: str
    device_type: str = "mk2_15"
    grid_rows: int = 3
    grid_cols: int = 5
    buttons: list = []
    clone_from: Optional[str] = None  # template_id to clone buttons from

class TemplateUpdate(BaseModel):
    display_name: Optional[str] = None
    buttons: Optional[list] = None
    is_default: Optional[bool] = None

class ReviewAdvance(BaseModel):
    doc_id: Optional[str] = None
    review_status: Optional[str] = None
    privilege_status: Optional[str] = None
    direction: str = "next"  # next, prev
    skip: bool = False


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _get_user_from_token(token_str: str):
    """Resolve user from Stream Deck API token (Bearer or query param)."""
    token_hash = _hash_token(token_str)
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text("""
            SELECT uc.user_id, uc.tenant_id
            FROM streamdeck_user_config uc
            WHERE uc.api_token_hash = :hash
            LIMIT 1
        """), {"hash": token_hash})).mappings().first()
        if not row:
            return None, None
        user_id = row["user_id"]
        tenant_id = (row["tenant_id"] or "").strip()
        # Load user record
        user_row = (await session.execute(text("""
            SELECT id, username, email, display_name, is_admin, tenant_id
            FROM users WHERE id = :uid AND is_active = true LIMIT 1
        """), {"uid": user_id})).mappings().first()
        if not user_row:
            return None, None
        # Update last_connected_at
        await session.execute(text("""
            UPDATE streamdeck_user_config SET last_connected_at = now()
            WHERE api_token_hash = :hash
        """), {"hash": token_hash})
        await session.commit()
        return dict(user_row), tenant_id


def _get_user(request: Request):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user

def _get_tenant(request: Request):
    tid = getattr(request.state, "tenant_id", None)
    if not tid:
        raise HTTPException(status_code=400, detail="Tenant context required")
    return tid.strip()

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

async def _get_user_or_token(request: Request):
    """Try session cookie auth first, then Bearer token."""
    user = getattr(request.state, "current_user", None)
    tenant_id = getattr(request.state, "tenant_id", None)
    if user:
        return user, (tenant_id or "").strip()
    # Fallback: Bearer header
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        u, t = await _get_user_from_token(auth_header[7:])
        if u:
            return u, t
    raise HTTPException(status_code=401, detail="Authentication required")




# ── In-memory context store (Redis upgrade path) ────────────────────────────
# For now, use a simple dict. Replace with Redis pub/sub when available.

_context_store: dict = {}  # user_id -> context payload
_context_events: dict = {}  # user_id -> list of asyncio.Event waiters


async def _publish_context(user_id: int, context: dict):
    """Store context and notify any waiting SSE connections."""
    _context_store[user_id] = context
    waiters = _context_events.get(user_id, [])
    for evt in waiters:
        evt.set()


# ── SSE Event Stream ─────────────────────────────────────────────────────────

@router.get("/events")
async def sse_events(request: Request, token: str = ""):
    """
    SSE stream for Stream Deck plugin.
    Auth: session cookie, Bearer header, or ?token= query param.
    """
    user = getattr(request.state, "current_user", None)
    user_id = None
    tenant_id = None

    if user:
        user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    else:
        # Try Bearer header
        auth_header = request.headers.get("authorization", "")
        bearer_token = auth_header.replace("Bearer ", "") if auth_header.startswith("Bearer ") else ""
        # Try query param
        tk = token or bearer_token
        if tk:
            user, tenant_id = await _get_user_from_token(tk)
            if user:
                user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    async def event_generator():
        # Send initial context if available
        current = _context_store.get(user_id)
        if current:
            yield f"data: {json.dumps(current)}\n\n"

        while True:
            # Create a waiter event
            evt = asyncio.Event()
            if user_id not in _context_events:
                _context_events[user_id] = []
            _context_events[user_id].append(evt)

            try:
                # Wait up to 15s, then send keepalive
                try:
                    await asyncio.wait_for(evt.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                # Context was updated
                ctx = _context_store.get(user_id, {})
                yield f"data: {json.dumps(ctx)}\n\n"

            finally:
                # Clean up waiter
                if user_id in _context_events:
                    try:
                        _context_events[user_id].remove(evt)
                    except ValueError:
                        pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Context Publishing ───────────────────────────────────────────────────────

@router.post("/context")
async def publish_context(body: ContextUpdate, request: Request):
    """
    Called by the browser when user navigates.
    Stores context and pushes to SSE subscribers.
    """
    user = getattr(request.state, "current_user", None)
    if not user:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            user, _ = await _get_user_from_token(auth_header[7:])
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    context = {
        "context_type": body.context_type,
        "user_id": user_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **body.payload,
    }

    await _publish_context(user_id, context)
    return {"ok": True, "context_type": body.context_type}


# ── Template Resolution ──────────────────────────────────────────────────────

@router.get("/template/{context_type}")
async def get_resolved_template(context_type: str, request: Request):
    """
    Returns the resolved template for the authenticated user and context.
    Resolution order: user override -> tenant default -> platform default.
    """
    user, tenant_id = await _get_user_or_token(request)
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    device_type = request.query_params.get("device_type", "mk2_15")

    async with AsyncSessionLocal() as session:
        # 1. Check user config for override
        row = (await session.execute(text("""
            SELECT context_assignments FROM streamdeck_user_config
            WHERE TRIM(tenant_id) = :tid AND user_id = :uid
            LIMIT 1
        """), {"tid": tenant_id, "uid": user_id})).mappings().first()

        override_id = None
        if row and row["context_assignments"]:
            assignments = row["context_assignments"] if isinstance(row["context_assignments"], dict) else json.loads(row["context_assignments"])
            override_id = assignments.get(context_type)

        if override_id:
            tmpl = (await session.execute(text("""
                SELECT * FROM streamdeck_templates WHERE id = CAST(:tid AS uuid)
            """), {"tid": override_id})).mappings().first()
            if tmpl:
                return dict(tmpl)

        # 2. Tenant default
        tmpl = (await session.execute(text("""
            SELECT * FROM streamdeck_templates
            WHERE TRIM(tenant_id) = :tid AND context_type = :ctx
              AND device_type = :dev AND is_default = true
            LIMIT 1
        """), {"tid": tenant_id, "ctx": context_type, "dev": device_type})).mappings().first()
        if tmpl:
            return dict(tmpl)

        # 3. Platform default (tenant_id IS NULL)
        tmpl = (await session.execute(text("""
            SELECT * FROM streamdeck_templates
            WHERE tenant_id IS NULL AND context_type = :ctx
              AND device_type = :dev AND is_default = true
            LIMIT 1
        """), {"ctx": context_type, "dev": device_type})).mappings().first()
        if tmpl:
            return dict(tmpl)

        raise HTTPException(status_code=404, detail=f"No template found for context={context_type}, device={device_type}")


# ── Template CRUD ────────────────────────────────────────────────────────────

@router.get("/templates")
async def list_templates(request: Request):
    """List all templates visible to the user (platform + tenant + own)."""
    user, tenant_id = await _get_user_or_token(request)
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT id, template_slug, display_name, context_type,
                   device_type, grid_rows, grid_cols, is_default,
                   created_by, tenant_id IS NULL AS is_platform,
                   created_at
            FROM streamdeck_templates
            WHERE tenant_id IS NULL
               OR TRIM(tenant_id) = :tid
            ORDER BY context_type, is_default DESC, display_name
        """), {"tid": tenant_id})).mappings().all()

    return [dict(r) for r in rows]


@router.post("/templates")
async def create_template(body: TemplateCreate, request: Request):
    """Create a new template. Optionally clone buttons from another template."""
    user, tenant_id = await _get_user_or_token(request)
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    buttons = body.buttons

    # Clone buttons from source if requested
    if body.clone_from:
        async with AsyncSessionLocal() as session:
            src = (await session.execute(text(
                "SELECT buttons FROM streamdeck_templates WHERE id = CAST(:sid AS uuid)"
            ), {"sid": body.clone_from})).mappings().first()
            if src:
                buttons = src["buttons"] if isinstance(src["buttons"], list) else json.loads(src["buttons"])

    async with AsyncSessionLocal() as session:
        result = await session.execute(text("""
            INSERT INTO streamdeck_templates
                (tenant_id, template_slug, display_name, context_type,
                 device_type, grid_rows, grid_cols, buttons, is_default, created_by)
            VALUES
                (:tid, :slug, :name, :ctx, :dev, :rows, :cols,
                 CAST(:buttons AS jsonb), false, :uid)
            RETURNING id, template_slug
        """), {
            "tid": tenant_id, "slug": body.template_slug, "name": body.display_name,
            "ctx": body.context_type, "dev": body.device_type,
            "rows": body.grid_rows, "cols": body.grid_cols,
            "buttons": json.dumps(buttons), "uid": user_id,
        })
        await session.commit()
        row = result.mappings().first()

    return {"id": str(row["id"]), "template_slug": row["template_slug"]}


@router.patch("/templates/{template_id}")
async def update_template(template_id: str, body: TemplateUpdate, request: Request):
    """Update a user-created template."""
    user, tenant_id = await _get_user_or_token(request)
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    async with AsyncSessionLocal() as session:
        # Verify ownership (can't edit platform defaults)
        tmpl = (await session.execute(text("""
            SELECT id, tenant_id, created_by FROM streamdeck_templates
            WHERE id = CAST(:tid AS uuid)
        """), {"tid": template_id})).mappings().first()

        if not tmpl:
            raise HTTPException(status_code=404, detail="Template not found")
        if tmpl["tenant_id"] is None:
            raise HTTPException(status_code=403, detail="Cannot edit platform default templates. Clone first.")

        updates = []
        params = {"tid": template_id}
        if body.display_name is not None:
            updates.append("display_name = :name")
            params["name"] = body.display_name
        if body.buttons is not None:
            updates.append("buttons = CAST(:buttons AS jsonb)")
            params["buttons"] = json.dumps(body.buttons)
        if body.is_default is not None:
            updates.append("is_default = :dflt")
            params["dflt"] = body.is_default
        updates.append("updated_at = now()")

        if updates:
            await session.execute(text(f"""
                UPDATE streamdeck_templates SET {', '.join(updates)}
                WHERE id = CAST(:tid AS uuid)
            """), params)
            await session.commit()

    return {"ok": True}


@router.delete("/templates/{template_id}")
async def delete_template(template_id: str, request: Request):
    """Delete a user-created template."""
    user, tenant_id = await _get_user_or_token(request)

    async with AsyncSessionLocal() as session:
        tmpl = (await session.execute(text("""
            SELECT tenant_id FROM streamdeck_templates
            WHERE id = CAST(:tid AS uuid)
        """), {"tid": template_id})).mappings().first()

        if not tmpl:
            raise HTTPException(status_code=404)
        if tmpl["tenant_id"] is None:
            raise HTTPException(status_code=403, detail="Cannot delete platform defaults")

        await session.execute(text(
            "DELETE FROM streamdeck_templates WHERE id = CAST(:tid AS uuid)"
        ), {"tid": template_id})
        await session.commit()

    return {"ok": True}


# ── Device Registration ──────────────────────────────────────────────────────

@router.post("/register")
async def register_device(body: RegisterDevice, request: Request):
    """
    Register a new Stream Deck device.
    Returns a one-time API token for the plugin.
    """
    user, tenant_id = await _get_user_or_token(request)
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    # Generate secure token
    raw_token = secrets.token_urlsafe(48)
    token_hash = _hash_token(raw_token)

    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            INSERT INTO streamdeck_user_config
                (tenant_id, user_id, device_serial, device_type,
                 api_token_hash, last_connected_at)
            VALUES
                (:tid, :uid, :serial, :dev, :hash, now())
            ON CONFLICT (tenant_id, user_id, device_serial)
            DO UPDATE SET api_token_hash = :hash, device_type = :dev,
                          last_connected_at = now()
        """), {
            "tid": tenant_id, "uid": user_id,
            "serial": body.device_serial, "dev": body.device_type,
            "hash": token_hash,
        })
        await session.commit()

    return {
        "token": raw_token,
        "device_type": body.device_type,
        "message": "Save this token — it will not be shown again.",
    }


# ── User Config ──────────────────────────────────────────────────────────────

@router.get("/config")
async def get_config(request: Request):
    """Returns the authenticated user's full Stream Deck config."""
    user, tenant_id = await _get_user_or_token(request)
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT * FROM streamdeck_user_config
            WHERE TRIM(tenant_id) = :tid AND user_id = :uid
        """), {"tid": tenant_id, "uid": user_id})).mappings().all()

    return [dict(r) for r in rows]


@router.patch("/config")
async def update_config(body: ConfigUpdate, request: Request):
    """Update user's Stream Deck config (context assignments, pinned tags)."""
    user, tenant_id = await _get_user_or_token(request)
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    async with AsyncSessionLocal() as session:
        updates = []
        params = {"tid": tenant_id, "uid": user_id}

        if body.context_assignments is not None:
            updates.append("context_assignments = CAST(:ca AS jsonb)")
            params["ca"] = json.dumps(body.context_assignments)
        if body.pinned_tags is not None:
            updates.append("pinned_tags = CAST(:pt AS jsonb)")
            params["pt"] = json.dumps(body.pinned_tags)
        if body.auto_advance is not None:
            updates.append("auto_advance = :aa")
            params["aa"] = body.auto_advance

        if updates:
            await session.execute(text(f"""
                UPDATE streamdeck_user_config SET {', '.join(updates)}
                WHERE TRIM(tenant_id) = :tid AND user_id = :uid
            """), params)
            await session.commit()

    return {"ok": True}


# ── Review-and-Advance Composite ─────────────────────────────────────────────

@router.post("/review-advance")
async def review_and_advance(body: ReviewAdvance, request: Request):
    """
    Composite endpoint: apply coding decision + advance to next document.
    Single round-trip for maximum coding velocity.
    """
    user, tenant_id = await _get_user_or_token(request)
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    # Get current context to know which doc we're on
    current_ctx = _context_store.get(user_id, {})
    doc_id = body.doc_id or current_ctx.get("doc_id")
    collection_id = current_ctx.get("collection_id")

    if not doc_id:
        raise HTTPException(status_code=400, detail="No active document in context")

    async with AsyncSessionLocal() as session:
        # 1. Apply coding decision if provided (not a skip)
        if not body.skip and doc_id:
            if body.review_status:
                await session.execute(text("""
                    UPDATE ediscovery_documents
                    SET review_status = :rs, reviewed_by = :uid, reviewed_at = now()
                    WHERE id = CAST(:did AS uuid)
                """), {"rs": body.review_status, "uid": user_id, "did": doc_id})

            if body.privilege_status:
                await session.execute(text("""
                    UPDATE ediscovery_documents
                    SET privilege_status = :ps
                    WHERE id = CAST(:did AS uuid)
                """), {"ps": body.privilege_status, "did": doc_id})

            await session.commit()

        # 2. Find next/prev document in queue
        if not collection_id:
            raise HTTPException(status_code=400, detail="No active collection in context")

        # Get current position
        doc_pos = current_ctx.get("doc_pos", 1)

        if body.direction == "next":
            next_doc = (await session.execute(text("""
                SELECT id, file_name, review_status, privilege_status
                FROM ediscovery_documents
                WHERE collection_id = CAST(:cid AS uuid)
                ORDER BY id
                OFFSET :offset LIMIT 1
            """), {"cid": collection_id, "offset": doc_pos})).mappings().first()
            new_pos = doc_pos + 1
        else:  # prev
            new_offset = max(0, doc_pos - 2)
            next_doc = (await session.execute(text("""
                SELECT id, file_name, review_status, privilege_status
                FROM ediscovery_documents
                WHERE collection_id = CAST(:cid AS uuid)
                ORDER BY id
                OFFSET :offset LIMIT 1
            """), {"cid": collection_id, "offset": new_offset})).mappings().first()
            new_pos = new_offset + 1

        if not next_doc:
            # End of queue
            new_ctx = {**current_ctx, "end_of_queue": True}
            await _publish_context(user_id, new_ctx)
            return {"ok": True, "end_of_queue": True, "doc_id": None}

        # 3. Get tags for next doc
        tags = (await session.execute(text("""
            SELECT tag_id FROM document_tags
            WHERE document_id = CAST(:did AS bigint)
              AND source_table = 'ediscovery'
        """), {"did": str(next_doc["id"])})).mappings().all()
        tag_ids = [str(t["tag_id"]) for t in tags]

        # 4. Build and publish new context
        new_ctx = {
            **current_ctx,
            "doc_id": str(next_doc["id"]),
            "doc_filename": next_doc["file_name"],
            "doc_pos": new_pos,
            "review_status": next_doc["review_status"],
            "privilege_status": next_doc["privilege_status"],
            "tags": tag_ids,
            "end_of_queue": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await _publish_context(user_id, new_ctx)

    return {"ok": True, "doc_id": str(next_doc["id"]), "doc_pos": new_pos}
