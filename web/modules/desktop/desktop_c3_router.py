"""
M-DESK C3 — Desktop Search + AI Drafting + Matter Selector API.

JSON endpoints for the VSTO Word/Excel add-in:

  GET  /api/v1/desktop/search          — unified search across DMS documents
  POST /api/v1/desktop/drafting/chat   — AI drafting chat (JSON, not HTMX)
  GET  /api/v1/desktop/active-matter   — get user's currently selected matter
  PUT  /api/v1/desktop/active-matter   — set user's currently selected matter

All routes JWT-authenticated via require_desktop_user.

Patent Pending - Series 2/3 - D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.desktop import jwt_service
from modules.desktop.checkout_router import require_desktop_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/desktop", tags=["m-desk-c3"])


# ═════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════

def _jsonable(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return obj


def _row_dict(row) -> dict:
    return {k: _jsonable(v) for k, v in dict(row).items()}


# ═════════════════════════════════════════════════════════════════════════
# GET /search — unified document search
# ═════════════════════════════════════════════════════════════════════════

@router.get("/search")
async def desktop_search(
    q: str = Query(..., min_length=1, description="Search query"),
    matter_id: Optional[str] = Query(None, description="Scope to matter UUID"),
    scope: str = Query("all", description="dms|ediscovery|all"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Search documents across DMS and optionally eDiscovery collections.

    Returns a unified result set with source indicator.
    """
    tid = claims.tenant_id
    search = f"%{q.strip()}%"
    results = []

    # ── DMS documents ───────────────────────────────────────────────────
    if scope in ("all", "dms"):
        matter_filter = ""
        params: dict[str, Any] = {
            "tid": tid, "search": search,
            "lim": limit, "off": offset,
        }
        if matter_id:
            matter_filter = "AND d.matter_id = CAST(:mid AS uuid)"
            params["mid"] = matter_id

        async with AsyncSessionLocal() as db:
            r = await db.execute(
                sa_text(f"""
                    SELECT
                      d.id::text            AS id,
                      d.filename,
                      d.title,
                      d.file_size,
                      d.mime_type,
                      d.document_type,
                      d.created_at,
                      d.storage_path,
                      m.matter_name,
                      m.matter_number,
                      m.id::text            AS matter_id,
                      c.client_name,
                      'dms'                 AS source
                    FROM documents d
                    LEFT JOIN matters m ON m.id = d.matter_id
                    LEFT JOIN clients c ON c.id = m.client_id
                    WHERE TRIM(d.tenant_id) = :tid
                      AND COALESCE(d.status, 'active') != 'deleted'
                      AND (
                          d.filename ILIKE :search
                          OR d.title ILIKE :search
                          OR d.original_filename ILIKE :search
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM documents child
                          WHERE child.parent_doc_id = d.id
                            AND TRIM(child.tenant_id) = :tid
                      )
                      {matter_filter}
                    ORDER BY d.created_at DESC
                    LIMIT :lim OFFSET :off
                """),
                params,
            )
            results.extend([_row_dict(row) for row in r.mappings().all()])

    # ── eDiscovery documents ────────────────────────────────────────────
    if scope in ("all", "ediscovery"):
        edisco_params: dict[str, Any] = {
            "tid": tid, "search": search,
            "lim": limit, "off": offset,
        }
        edisco_matter_filter = ""
        if matter_id:
            edisco_matter_filter = "AND ec.matter_id = CAST(:mid AS uuid)"
            edisco_params["mid"] = matter_id

        async with AsyncSessionLocal() as db:
            r = await db.execute(
                sa_text(f"""
                    SELECT
                      ed.id::text           AS id,
                      ed.file_name          AS filename,
                      ed.bates_number       AS title,
                      ed.file_size,
                      ed.mime_type,
                      'ediscovery'          AS document_type,
                      ed.created_at,
                      ed.file_path          AS storage_path,
                      m.matter_name,
                      m.matter_number,
                      m.id::text            AS matter_id,
                      c.client_name,
                      'ediscovery'          AS source
                    FROM ediscovery_documents ed
                    JOIN ediscovery_collections ec ON ec.id = ed.collection_id
                    LEFT JOIN matters m ON m.id = ec.matter_id
                    LEFT JOIN clients c ON c.id = m.client_id
                    WHERE TRIM(ed.tenant_id) = :tid
                      AND (
                          ed.file_name ILIKE :search
                          OR ed.bates_number ILIKE :search
                          OR ed.extracted_text ILIKE :search
                      )
                      {edisco_matter_filter}
                    ORDER BY ed.created_at DESC
                    LIMIT :lim OFFSET :off
                """),
                edisco_params,
            )
            results.extend([_row_dict(row) for row in r.mappings().all()])

    return {
        "query": q,
        "scope": scope,
        "matter_id": matter_id,
        "results": results,
        "total": len(results),
    }


# ═════════════════════════════════════════════════════════════════════════
# POST /drafting/chat — AI drafting (JSON response)
# ═════════════════════════════════════════════════════════════════════════

class DraftingChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    matter_id: Optional[str] = None
    document_type: Optional[str] = None
    selected_text: Optional[str] = None
    document_content: Optional[str] = None
    conversation_history: list[dict] = Field(default_factory=list)


@router.post("/drafting/chat")
async def desktop_drafting_chat(
    body: DraftingChatRequest,
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """AI drafting chat endpoint for the VSTO client.

    Accepts user message + optional matter context + selected text from Word.
    Returns AI-generated response as JSON with the drafted content.
    """
    tid = claims.tenant_id

    # ── Build matter context ────────────────────────────────────────────
    matter_context = ""
    if body.matter_id:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                sa_text("""
                    SELECT m.matter_number, m.matter_name, m.matter_type,
                           m.practice_area, m.status, m.notes,
                           c.client_name
                    FROM matters m
                    LEFT JOIN clients c ON c.id = m.client_id
                    WHERE m.id = CAST(:mid AS uuid)
                      AND TRIM(m.tenant_id) = :tid
                """),
                {"mid": body.matter_id, "tid": tid},
            )
            row = r.fetchone()
            if row:
                matter_context = (
                    f"Matter: {row.matter_number or ''} {row.matter_name}\n"
                    f"Client: {row.client_name or 'N/A'}\n"
                    f"Type: {row.matter_type or 'N/A'}\n"
                    f"Practice Area: {row.practice_area or 'N/A'}\n"
                )
                if row.notes:
                    matter_context += f"Notes: {row.notes[:500]}\n"

    # ── Build system prompt ─────────────────────────────────────────────
    system_parts = [
        "You are Praesidium AI, a legal drafting assistant embedded in "
        "Microsoft Word. You help attorneys draft, revise, and improve "
        "legal documents. Always produce professional, court-ready prose.",
    ]
    if matter_context:
        system_parts.append(f"\nActive Matter Context:\n{matter_context}")
    if body.document_type:
        system_parts.append(
            f"\nDocument Type: {body.document_type.replace('_', ' ').title()}"
        )
    if body.selected_text:
        system_parts.append(
            f"\nThe attorney has selected the following text in their document "
            f"for you to work with:\n---\n{body.selected_text[:3000]}\n---"
        )
    if body.document_content:
        # Truncate to ~40K chars to stay within token limits
        doc_text = body.document_content[:40000]
        system_parts.append(
            f"\nThe full content of the active document in Word is provided below "
            f"for context. Use it to understand the document structure, style, and "
            f"content when drafting or revising.\n"
            f"---DOCUMENT START---\n{doc_text}\n---DOCUMENT END---"
        )

    system_prompt = "\n".join(system_parts)

    # ── Build messages ──────────────────────────────────────────────────
    messages = []
    for msg in body.conversation_history[-10:]:  # last 10 turns
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": body.message})

    # ── Call Anthropic API ──────────────────────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        # Try credentials vault
        try:
            async with AsyncSessionLocal() as db:
                r = await db.execute(
                    sa_text("""
                        SELECT decrypted_value FROM credentials_vault
                        WHERE TRIM(tenant_id) = :tid
                          AND provider = 'anthropic'
                          AND key_type = 'api_key'
                        LIMIT 1
                    """),
                    {"tid": tid},
                )
                row = r.fetchone()
                if row:
                    api_key = row[0]
        except Exception:
            pass

    if not api_key:
        return JSONResponse(
            status_code=503,
            content={
                "error": "ai_unavailable",
                "message": "No Anthropic API key configured for this tenant.",
            },
        )

    try:
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 4096,
                    "system": system_prompt,
                    "messages": messages,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        # Extract text from response
        ai_text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                ai_text += block.get("text", "")

        # ── Log to ai_api_calls if table exists ─────────────────────────
        try:
            usage = data.get("usage", {})
            async with AsyncSessionLocal() as db:
                await db.execute(
                    sa_text("""
                        INSERT INTO ai_api_calls (
                            id, tenant_id, user_id, module, action,
                            model, input_tokens, output_tokens,
                            matter_id, created_at
                        ) VALUES (
                            :id, :tid, :uid, 'desktop_drafting', 'chat',
                            :model, :inp, :out,
                            CAST(:mid AS uuid), NOW()
                        )
                    """),
                    {
                        "id": str(uuid.uuid4()),
                        "tid": tid,
                        "uid": claims.user_id,
                        "model": data.get("model", "claude-sonnet-4-20250514"),
                        "inp": usage.get("input_tokens", 0),
                        "out": usage.get("output_tokens", 0),
                        "mid": body.matter_id,
                    },
                )
                await db.commit()
        except Exception as exc:
            logger.warning("[m-desk] ai_api_calls log failed: %s", exc)

        return {
            "response": ai_text,
            "model": data.get("model"),
            "usage": data.get("usage"),
            "matter_id": body.matter_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except httpx.HTTPStatusError as exc:
        logger.error("[m-desk] Anthropic API error: %s", exc.response.text)
        return JSONResponse(
            status_code=502,
            content={
                "error": "ai_error",
                "message": f"AI service returned {exc.response.status_code}",
            },
        )
    except Exception as exc:
        logger.error("[m-desk] drafting chat error: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": "internal", "message": str(exc)},
        )


# ═════════════════════════════════════════════════════════════════════════
# Active Matter — per-user matter selector for time tracking
# ═════════════════════════════════════════════════════════════════════════

class SetActiveMatterRequest(BaseModel):
    matter_id: Optional[str] = None


@router.get("/active-matter")
async def get_active_matter(
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Return the user's currently active matter (stored in user user_preferences)."""
    tid = claims.tenant_id
    uid = claims.user_id

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            sa_text("""
                SELECT
                  u.user_preferences->>'active_matter_id' AS active_matter_id,
                  u.user_preferences->>'active_matter_set_at' AS set_at,
                  m.matter_name, m.matter_number,
                  c.client_name
                FROM users u
                LEFT JOIN matters m
                  ON m.id = CAST(u.user_preferences->>'active_matter_id' AS uuid)
                LEFT JOIN clients c ON c.id = m.client_id
                WHERE u.id = :uid
                  AND TRIM(u.tenant_id) = :tid
            """),
            {"uid": uid, "tid": tid},
        )
        row = r.mappings().first()

    if not row or not row.get("active_matter_id"):
        return {"active_matter": None}

    return {
        "active_matter": {
            "matter_id": row["active_matter_id"],
            "matter_name": row.get("matter_name"),
            "matter_number": row.get("matter_number"),
            "client_name": row.get("client_name"),
            "set_at": row.get("set_at"),
        }
    }


@router.put("/active-matter")
async def set_active_matter(
    body: SetActiveMatterRequest,
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Set or clear the user's active matter.

    Stored in users.user_preferences JSONB so it persists across sessions.
    The VSTO status bar shows the active matter; all AI drafting and
    time tracking defaults to this matter.
    """
    tid = claims.tenant_id
    uid = claims.user_id

    if body.matter_id:
        # Verify matter belongs to tenant
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                sa_text("""
                    SELECT m.matter_name, m.matter_number, c.client_name
                    FROM matters m
                    LEFT JOIN clients c ON c.id = m.client_id
                    WHERE m.id = CAST(:mid AS uuid)
                      AND TRIM(m.tenant_id) = :tid
                """),
                {"mid": body.matter_id, "tid": tid},
            )
            matter = r.mappings().first()
            if not matter:
                raise HTTPException(404, "Matter not found")

        # Update user user_preferences
        now = datetime.now(timezone.utc).isoformat()
        async with AsyncSessionLocal() as db:
            await db.execute(
                sa_text("""
                    UPDATE users
                    SET user_preferences = COALESCE(user_preferences, '{}'::jsonb)
                        || jsonb_build_object(
                            'active_matter_id', CAST(:mid AS text),
                            'active_matter_set_at', CAST(:now AS text)
                        )
                    WHERE id = :uid AND TRIM(tenant_id) = :tid
                """),
                {"mid": body.matter_id, "now": now, "uid": uid, "tid": tid},
            )
            await db.commit()

        return {
            "active_matter": {
                "matter_id": body.matter_id,
                "matter_name": matter["matter_name"],
                "matter_number": matter["matter_number"],
                "client_name": matter["client_name"],
                "set_at": now,
            }
        }
    else:
        # Clear active matter
        async with AsyncSessionLocal() as db:
            await db.execute(
                sa_text("""
                    UPDATE users
                    SET user_preferences = COALESCE(user_preferences, '{}'::jsonb)
                        - 'active_matter_id'
                        - 'active_matter_set_at'
                    WHERE id = :uid AND TRIM(tenant_id) = :tid
                """),
                {"uid": uid, "tid": tid},
            )
            await db.commit()

        return {"active_matter": None}
