"""
modules/dashboard/routes/ai_chat_route.py — v4
User AI Chat SSE Route with:
  - Chat history persistence (chat_sessions + chat_messages)
  - Tool result emission (so frontend gets file paths from draft_document)
  - Session management endpoints (list, clear, load)
  - Web search as tenant-configurable fallback (v4)

POST /api/ai-chat            — SSE streaming chat (creates/reuses session)
GET  /api/ai-chat/sessions   — list recent sessions
GET  /api/ai-chat/sessions/{id}/messages — load messages for a session
POST /api/ai-chat/sessions/{id}/clear   — clear session (deletes staged files)
"""

import json
import logging
import os
import uuid as _uuid

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse
from fastapi.responses import JSONResponse
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.ai_chat")

router = APIRouter(tags=["ai-chat"])

MCP_USER_URL = "https://mcp-user.praesidium-legal.com/mcp"
MCP_ADMIN_URL = "https://mcp-admin.praesidium-legal.com/mcp"


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _user(request: Request):
    return getattr(request.state, "current_user", None)


def _user_id(request: Request) -> int:
    u = _user(request)
    return int(getattr(u, "id", 0) or 0) if u else 0


def _is_admin(user) -> bool:
    role = getattr(user, "role", "")
    return role in ("admin", "platform_admin", "super_admin")


async def _get_api_key(tenant_id: str) -> str | None:
    from cryptography.fernet import Fernet
    import base64
    secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    key_bytes = (secret[:32]).encode().ljust(32, b"0")
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    f = Fernet(fernet_key)
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(
            "SELECT encrypted_key FROM credentials_vault "
            "WHERE tenant_id = :tid AND provider = 'anthropic' AND key_type = 'api_key'"
        ), {"tid": tenant_id})).fetchone()
    if not row:
        return None
    return f.decrypt(row[0].encode()).decode()


async def _resolve_tenant_slug(tenant_id: str) -> str:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(
            "SELECT slug FROM tenants WHERE TRIM(id) = :tid"
        ), {"tid": tenant_id.strip()})).fetchone()
    return row[0] if row else tenant_id.strip()


async def _is_web_search_enabled(tenant_id: str, is_admin: bool) -> bool:
    """Check if web search is enabled.

    Resolution order:
    1. feature_overrides row with feature_flag = 'ai_web_search' → use enabled_globally
    2. No row → default: enabled for admins, disabled for standard users
    """
    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(text(
                "SELECT enabled_globally FROM feature_overrides "
                "WHERE feature_flag = 'ai_web_search'"
            ))).fetchone()
        if row is not None:
            return bool(row[0])
    except Exception:
        # Table may not exist or query failed — fall through to default
        pass
    # Default: admins get web search, standard users don't (conservative)
    return is_admin


def _build_system_prompt(user, context: dict | None = None,
                         tenant_slug: str = "", web_search_enabled: bool = False) -> str:
    name = getattr(user, "full_name", None) or getattr(user, "username", "User")
    role = getattr(user, "role", "attorney")
    t = tenant_slug or "default"

    base = f"""You are Praesidium AI, an intelligent assistant for {name} ({role}).
You have access to the firm's practice management tools via MCP.
You can query matters, clients, documents, timesheets, invoices, trust accounts, and eDiscovery collections.
You can also browse matter files, draft documents, and upload files.

CRITICAL: You MUST pass tenant="{t}" to EVERY MCP tool call. Without it, tools return empty results.

Be concise and professional. Use firm-specific data when answering questions.
When you use a tool, explain what you found in plain language.
When you draft a document, confirm the filename and that it is staged for review. The UI picks up drafted documents automatically from the tool result — you do not need to repeat the file path."""

    # ── Web search instructions ──────────────────────────────────
    if web_search_enabled:
        base += """

WEB SEARCH:
You have access to web search. Use it as a FALLBACK — always check firm data via MCP tools first.
Use web search when:
- The question requires current legal developments, regulatory updates, or recent case law
- MCP tools returned no results and the question is about external information
- The user explicitly asks you to search the web or look something up online
- You need to verify a citation, statute, or rule number
- The question is about opposing counsel, judges, or parties not in the firm's database
Do NOT use web search for questions that can be answered from the firm's own data (matters, documents, timesheets, invoices, etc.)."""

    if context:
        page = context.get("page", "")
        matter_id = context.get("matter_id", "")
        matter_name = context.get("matter_name", "")

        if matter_id and matter_name:
            base += f"""

ACTIVE MATTER CONTEXT:
- Matter: {matter_name}
- Matter UUID: {matter_id}
- Tenant: {t}

RULES:
1. Pass tenant="{t}" to EVERY tool call
2. Use matter_id="{matter_id}" directly — never search by name
3. Example: browse_matter_files(matter_id="{matter_id}", tenant="{t}")
4. Example: draft_document(matter_id="{matter_id}", tenant="{t}", ...)
5. Example: query_documents(matter_id="{matter_id}", tenant="{t}")

DRAFTING WORKFLOW (when asked to draft a document):
1. FIRST: browse_matter_files(matter_id="{matter_id}", tenant="{t}") to see existing files
2. Look for CLAs, loan agreements, term sheets, prior drafts, executed documents
3. Extract deal terms from source documents
4. Draft using extracted terms and the firm's standard forms
5. Save: draft_document(matter_id="{matter_id}", tenant="{t}", document_type="...", instructions="...")
6. Confirm the draft filename and that it is staged in the Output Documents panel — the UI extracts it from the tool result automatically, so do NOT repeat the raw file path
Never draft from scratch when source documents exist in the matter folder."""

            doc_type = context.get("document_type", "")

            # Layout editing context
            base += f"""

DASHBOARD LAYOUT EDITING:
You can edit the user's matter dashboard layout. The layout API endpoints are:
- GET /api/v1/layouts/matter_dashboard/{matter_id} — get current layout
- PUT /api/v1/layouts/matter_dashboard/{matter_id} — save layout (body: {{"layout": {{...}}}})
- DELETE /api/v1/layouts/matter_dashboard/{matter_id} — reset to default
- POST /api/v1/layouts/ai-edit — AI-driven edit (body: {{"context_type": "matter_dashboard", "context_id": "{matter_id}", "instruction": "..."}})
- GET /api/v1/widgets/available — list all available widgets

When the user asks to customize their dashboard (e.g. "hide the drop zone", "make the terms full width",
"add email summary"), call the ai-edit endpoint with their instruction. The endpoint will modify the layout
and save it. Tell the user to refresh to see changes.

Available widget categories: matter, billing, calendar, dms, ediscovery, firm, intelligence, workspace, collaboration.
"""
            if doc_type:
                base += f"\nRequested document type: {doc_type.replace('_', ' ').title()}"

        elif page == "billing":
            base += f"\n\nThe user is in Billing. Pass tenant=\"{t}\" to all tool calls."
        elif page == "dms":
            base += f"\n\nThe user is in Documents. Pass tenant=\"{t}\" to all tool calls."
        elif page == "ediscovery":
            base += f"\n\nThe user is in eDiscovery. Pass tenant=\"{t}\" to all tool calls."

    return base


# ═══════════════════════════════════════════════════════════════
# Session Management
# ═══════════════════════════════════════════════════════════════

@router.get("/api/ai-chat/sessions")
async def list_sessions(request: Request, limit: int = 20, matter_id: str = ""):
    """List recent chat sessions for the current user."""
    user = _user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    tid = _tid(request)
    uid = _user_id(request)

    async with AsyncSessionLocal() as session:
        params = {"tid": tid, "uid": uid, "lim": min(limit, 50)}
        matter_filter = ""
        if matter_id:
            matter_filter = "AND cs.matter_id = CAST(:mid AS uuid)"
            params["mid"] = matter_id
        rows = await session.execute(text(f"""
            SELECT cs.id::text, cs.title, cs.context_page, cs.document_type,
                   cs.message_count, cs.status, cs.created_at, cs.updated_at,
                   m.matter_name, m.matter_number
            FROM chat_sessions cs
            LEFT JOIN matters m ON cs.matter_id = m.id
            WHERE TRIM(cs.tenant_id) = :tid AND cs.user_id = :uid
              AND cs.status != 'cleared'
              {matter_filter}
            ORDER BY cs.updated_at DESC
            LIMIT :lim
        """), params)
        results = []
        for r in rows.mappings().fetchall():
            d = dict(r)
            d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
            d["updated_at"] = d["updated_at"].isoformat() if d.get("updated_at") else None
            results.append(d)
    return JSONResponse(results)


@router.get("/api/ai-chat/sessions/{session_id}/messages")
async def get_session_messages(request: Request, session_id: str):
    """Load all messages for a chat session."""
    user = _user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    tid = _tid(request)

    async with AsyncSessionLocal() as session:
        # Verify ownership
        owner = await session.execute(text("""
            SELECT user_id FROM chat_sessions
            WHERE id = CAST(:sid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"sid": session_id, "tid": tid})
        row = owner.fetchone()
        if not row or row[0] != _user_id(request):
            return JSONResponse({"error": "Session not found"}, status_code=404)

        msgs = await session.execute(text("""
            SELECT id::text, role, content, tool_calls, created_at
            FROM chat_messages
            WHERE session_id = CAST(:sid AS uuid)
            ORDER BY created_at
        """), {"sid": session_id})
        results = []
        for r in msgs.mappings().fetchall():
            d = dict(r)
            d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
            results.append(d)
    return JSONResponse(results)


@router.post("/api/ai-chat/sessions/{session_id}/clear")
async def clear_session(request: Request, session_id: str):
    """Clear a chat session — marks it as cleared and deletes staged drafts."""
    user = _user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    tid = _tid(request)

    async with AsyncSessionLocal() as session:
        # Verify ownership
        owner = await session.execute(text("""
            SELECT user_id FROM chat_sessions
            WHERE id = CAST(:sid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"sid": session_id, "tid": tid})
        row = owner.fetchone()
        if not row or row[0] != _user_id(request):
            return JSONResponse({"error": "Session not found"}, status_code=404)

        # Get un-promoted drafts for this session
        drafts = await session.execute(text("""
            SELECT storage_path FROM drafting_outputs
            WHERE session_id = CAST(:sid AS uuid)
              AND TRIM(tenant_id) = :tid
              AND promoted_at IS NULL AND deleted_at IS NULL
        """), {"sid": session_id, "tid": tid})
        deleted_files = 0
        import pathlib
        for d in drafts.fetchall():
            p = pathlib.Path(d[0])
            if p.is_file():
                try:
                    p.unlink()
                    deleted_files += 1
                except OSError:
                    pass

        # Soft-delete drafts
        await session.execute(text("""
            UPDATE drafting_outputs SET deleted_at = NOW()
            WHERE session_id = CAST(:sid AS uuid)
              AND TRIM(tenant_id) = :tid
              AND promoted_at IS NULL AND deleted_at IS NULL
        """), {"sid": session_id, "tid": tid})

        # Mark session as cleared
        await session.execute(text("""
            UPDATE chat_sessions SET status = 'cleared', cleared_at = NOW()
            WHERE id = CAST(:sid AS uuid)
        """), {"sid": session_id})
        await session.commit()

    return JSONResponse({"status": "ok", "files_deleted": deleted_files})


# ═══════════════════════════════════════════════════════════════
# Main Chat Endpoint
# ═══════════════════════════════════════════════════════════════

@router.post("/api/ai-chat")
async def user_ai_chat(request: Request):
    import httpx

    user = _user(request)
    if not user:
        async def err():
            yield f"data: {json.dumps({'type': 'error', 'text': 'Not authenticated'})}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    tid = _tid(request)
    api_key = await _get_api_key(tid)
    if not api_key:
        async def err():
            yield f"data: {json.dumps({'type': 'error', 'text': 'No API key configured.'})}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    body = await request.json()
    messages = body.get("messages", [])
    context = body.get("context", {})
    session_id = body.get("session_id", "")

    if not messages:
        async def err():
            yield f"data: {json.dumps({'type': 'error', 'text': 'No messages provided'})}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    # ── Persist session and user message ─────────────────────────
    uid = _user_id(request)
    user_msg = messages[-1].get("content", "") if messages else ""

    async with AsyncSessionLocal() as db:
        if not session_id:
            # Create new session
            row = await db.execute(text("""
                INSERT INTO chat_sessions (tenant_id, user_id, title, context_page,
                    matter_id, document_type)
                VALUES (:tid, :uid, :title, :page,
                    CASE WHEN :mid = '' THEN NULL ELSE CAST(:mid AS uuid) END,
                    :dtype)
                RETURNING id::text
            """), {
                "tid": tid, "uid": uid,
                "title": user_msg[:100] if user_msg else "New chat",
                "page": context.get("page", ""),
                "mid": context.get("matter_id", ""),
                "dtype": context.get("document_type", ""),
            })
            session_id = row.scalar()
            await db.commit()

        # Save user message
        await db.execute(text("""
            INSERT INTO chat_messages (session_id, role, content)
            VALUES (CAST(:sid AS uuid), 'user', :content)
        """), {"sid": session_id, "content": user_msg})
        await db.execute(text("""
            UPDATE chat_sessions SET message_count = message_count + 1,
                updated_at = NOW() WHERE id = CAST(:sid AS uuid)
        """), {"sid": session_id})
        await db.commit()

    # ── Resolve web search availability ──────────────────────────
    admin = _is_admin(user)
    web_search_enabled = await _is_web_search_enabled(tid, admin)

    # ── Build API request ────────────────────────────────────────
    mcp_servers = [{"type": "url", "url": MCP_USER_URL, "name": "praesidium-app"}]
    tools = [{"type": "mcp_toolset", "mcp_server_name": "praesidium-app"}]

    if admin:
        mcp_servers.append({"type": "url", "url": MCP_ADMIN_URL, "name": "praesidium-admin"})
        tools.append({"type": "mcp_toolset", "mcp_server_name": "praesidium-admin"})

    # Web search tool — Anthropic built-in, no MCP server needed
    if web_search_enabled:
        tools.append({"type": "web_search_20250305", "name": "web_search"})

    tenant_slug = await _resolve_tenant_slug(tid) if tid else ""
    system_prompt = _build_system_prompt(
        user, context, tenant_slug=tenant_slug,
        web_search_enabled=web_search_enabled,
    )

    api_body = {
        "model": getattr(request.state, "mobile_model_override", "claude-sonnet-4-20250514"),
        "max_tokens": 16384,
        "stream": True,
        "system": system_prompt,
        "messages": messages,
        "mcp_servers": mcp_servers,
        "tools": tools,
    }

    async def generate():
        full_text = ""
        tool_calls_log = []
        current_block_type = None
        current_tool_name = None
        tool_result_text = ""

        # Emit session_id so frontend can track it
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
                async with client.stream(
                    "POST",
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "anthropic-beta": "mcp-client-2025-11-20",
                        "content-type": "application/json",
                    },
                    json=api_body,
                ) as resp:
                    if resp.status_code != 200:
                        error_body = await resp.aread()
                        try:
                            err_json = json.loads(error_body)
                            err_msg = err_json.get("error", {}).get("message", f"HTTP {resp.status_code}")
                        except Exception:
                            err_msg = f"HTTP {resp.status_code}"
                        yield f"data: {json.dumps({'type': 'error', 'text': err_msg})}\n\n"
                        return

                    buffer = ""
                    async for chunk in resp.aiter_text():
                        buffer += chunk
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line or line.startswith(":"):
                                continue
                            if line.startswith("data: "):
                                data_str = line[6:]
                                if data_str == "[DONE]":
                                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                                    break
                                try:
                                    evt = json.loads(data_str)
                                    evt_type = evt.get("type", "")

                                    if evt_type == "content_block_start":
                                        block = evt.get("content_block", {})
                                        block_type = block.get("type", "")
                                        current_block_type = block_type
                                        tool_result_text = ""

                                        if block_type in ("tool_use", "mcp_tool_use"):
                                            current_tool_name = block.get("name", "unknown")
                                            yield f"data: {json.dumps({'type': 'tool_start', 'tool': current_tool_name})}\n\n"
                                            tool_calls_log.append({"name": current_tool_name})

                                        elif block_type == "server_tool_use":
                                            # Web search tool invocation
                                            current_tool_name = block.get("name", "web_search")
                                            yield f"data: {json.dumps({'type': 'tool_start', 'tool': 'web_search'})}\n\n"
                                            tool_calls_log.append({"name": "web_search"})

                                        elif block_type in ("mcp_tool_result", "server_tool_result"):
                                            # MCP tool result or web search result
                                            rc = block.get("content", "")
                                            if isinstance(rc, list):
                                                parts = []
                                                for item in rc:
                                                    if isinstance(item, dict) and item.get("text"):
                                                        parts.append(item["text"])
                                                    elif isinstance(item, str):
                                                        parts.append(item)
                                                tool_result_text = "\n".join(parts)
                                            elif isinstance(rc, str):
                                                tool_result_text = rc
                                            else:
                                                tool_result_text = json.dumps(rc)

                                    elif evt_type == "content_block_delta":
                                        delta = evt.get("delta", {})
                                        delta_type = delta.get("type", "")

                                        if delta_type == "text_delta":
                                            txt = delta.get("text", "")
                                            full_text += txt
                                            yield f"data: {json.dumps({'type': 'text', 'text': txt})}\n\n"

                                    elif evt_type == "content_block_stop":
                                        if current_block_type in ("tool_use", "mcp_tool_use", "server_tool_use"):
                                            yield f"data: {json.dumps({'type': 'block_stop'})}\n\n"
                                        elif current_block_type in ("mcp_tool_result", "server_tool_result"):
                                            # Emit tool result so frontend can parse file paths
                                            yield f"data: {json.dumps({'type': 'tool_result', 'content': tool_result_text})}\n\n"
                                        else:
                                            yield f"data: {json.dumps({'type': 'block_stop'})}\n\n"
                                        current_block_type = None

                                    elif evt_type == "message_stop":
                                        yield f"data: {json.dumps({'type': 'done'})}\n\n"

                                    elif evt_type == "error":
                                        yield f"data: {json.dumps({'type': 'error', 'text': evt.get('error', {}).get('message', 'Unknown error')})}\n\n"

                                except json.JSONDecodeError:
                                    pass

        except httpx.TimeoutException:
            yield f"data: {json.dumps({'type': 'error', 'text': 'Request timed out'})}\n\n"
        except Exception as e:
            log.exception("AI chat stream error")
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"

        # ── Persist assistant message ────────────────────────────
        if full_text or tool_calls_log:
            try:
                async with AsyncSessionLocal() as db:
                    await db.execute(text("""
                        INSERT INTO chat_messages (session_id, role, content, tool_calls)
                        VALUES (CAST(:sid AS uuid), 'assistant', :content, :tools)
                    """), {
                        "sid": session_id,
                        "content": full_text,
                        "tools": json.dumps(tool_calls_log) if tool_calls_log else None,
                    })
                    await db.execute(text("""
                        UPDATE chat_sessions SET message_count = message_count + 1,
                            updated_at = NOW() WHERE id = CAST(:sid AS uuid)
                    """), {"sid": session_id})
                    await db.commit()
            except Exception as e:
                log.warning("Failed to persist assistant message: %s", e)

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
