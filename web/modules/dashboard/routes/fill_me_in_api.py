"""
modules/dashboard/routes/fill_me_in_api.py — Fill Me In
AI-powered briefing of what happened while you were away.

GET  /api/v1/fill-me-in          — raw counts + recent items (fast, no AI)
POST /api/v1/fill-me-in/summary  — AI-generated 3-5 sentence briefing (SSE stream)

Query params:
  window: 1h | 4h | 8h | 12h | 24h | 48h | 7d | since_login  (default: since_login)
"""

import json
import logging
import os
import uuid as _uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.fill_me_in")
router = APIRouter(prefix="/api/v1/fill-me-in", tags=["fill-me-in"])

WINDOW_MAP = {
    "1h":  timedelta(hours=1),
    "2h":  timedelta(hours=2),
    "4h":  timedelta(hours=4),
    "8h":  timedelta(hours=8),
    "12h": timedelta(hours=12),
    "24h": timedelta(hours=24),
    "48h": timedelta(hours=48),
    "7d":  timedelta(days=7),
}



# ═══════════════════════════════════════════════════════════════
# Redis cache for AI summary — 5 min TTL per user per window
# ═══════════════════════════════════════════════════════════════
import redis as _redis

_FMI_CACHE = True
_FMI_REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
_FMI_SUMMARY_TTL = 300  # 5 minutes

def _fmi_cache_key(tid: str, uid: int, window: str) -> str:
    return f"fmi:summary:v1:{tid.strip()}:{uid}:{window}"

def _fmi_cache_get(tid: str, uid: int, window: str) -> str | None:
    try:
        r = _redis.from_url(_FMI_REDIS_URL, decode_responses=True, socket_connect_timeout=1)
        return r.get(_fmi_cache_key(tid, uid, window))
    except Exception:
        return None

def _fmi_cache_set(tid: str, uid: int, window: str, text: str):
    try:
        r = _redis.from_url(_FMI_REDIS_URL, decode_responses=True, socket_connect_timeout=1)
        r.setex(_fmi_cache_key(tid, uid, window), _FMI_SUMMARY_TTL, text)
    except Exception:
        pass

def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _user(request: Request):
    return getattr(request.state, "current_user", None)


def _user_id(request: Request) -> int:
    u = _user(request)
    return int(getattr(u, "id", 0) or 0) if u else 0


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


async def _resolve_since(request: Request, window: str) -> datetime:
    """Resolve the time window to a UTC datetime."""
    if window == "since_login":
        tid = _tid(request)
        uid = _user_id(request)
        async with AsyncSessionLocal() as session:
            row = (await session.execute(text(
                "SELECT last_login FROM users "
                "WHERE TRIM(tenant_id) = :tid AND id = :uid"
            ), {"tid": tid, "uid": uid})).fetchone()
        if row and row[0]:
            ll = row[0]
            if ll.tzinfo is None:
                ll = ll.replace(tzinfo=timezone.utc)
            return ll
        # No last_login — fall back to 24h
        return datetime.now(timezone.utc) - timedelta(hours=24)

    delta = WINDOW_MAP.get(window)
    if not delta:
        delta = timedelta(hours=24)
    return datetime.now(timezone.utc) - delta



def _naive(dt: datetime) -> datetime:
    """Strip timezone for naive timestamp columns (deadlines, tasks)."""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt

async def _gather_data(tid: str, uid: int, since: datetime) -> dict:
    """Gather all data sources for the briefing."""
    data = {}
    # Pass native datetime to asyncpg — no isoformat strings

    async with AsyncSessionLocal() as session:
        # ── New emails ──────────────────────────────────────────
        email_rows = (await session.execute(text("""
            SELECT e.subject, e.from_display, e.from_email,
                   e.received_at, e.routing_status, e.importance,
                   m.matter_name, m.matter_number, m.id as matter_id
            FROM email_routing_queue e
            LEFT JOIN matters m ON e.matched_matter_id = m.id
            WHERE TRIM(e.tenant_id) = :tid
              AND e.attorney_user_id = :uid
              AND e.received_at >= :since
            ORDER BY e.received_at DESC
            LIMIT 50
        """), {"tid": tid, "uid": uid, "since": since})).mappings().fetchall()

        data["emails"] = {
            "count": len(email_rows),
            "unrouted": sum(1 for e in email_rows if e["routing_status"] in (None, "pending", "unmatched")),
            "high_importance": sum(1 for e in email_rows if e["importance"] == "high"),
            "items": [
                {
                    "subject": r["subject"],
                    "from": r["from_display"] or r["from_email"],
                    "received": r["received_at"].isoformat() if r["received_at"] else None,
                    "matter": r["matter_name"],
                    "matter_id": str(r["matter_id"]) if r.get("matter_id") else None,
                    "importance": r["importance"],
                }
                for r in email_rows[:20]  # Cap detail items for AI prompt size
            ],
        }

        # ── New documents ───────────────────────────────────────
        doc_rows = (await session.execute(text("""
            SELECT d.title, d.original_filename, d.document_type,
                   d.created_at, m.matter_name, m.matter_number, m.id as matter_id
            FROM documents d
            LEFT JOIN matters m ON d.matter_id = m.id
            WHERE TRIM(d.tenant_id) = :tid
              AND d.created_at >= :since
            ORDER BY d.created_at DESC
            LIMIT 50
        """), {"tid": tid, "since": since})).mappings().fetchall()

        data["documents"] = {
            "count": len(doc_rows),
            "items": [
                {
                    "name": r["title"] or r["original_filename"],
                    "type": r["document_type"],
                    "matter": r["matter_name"],
                    "matter_id": str(r["matter_id"]) if r.get("matter_id") else None,
                    "created": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in doc_rows[:20]
            ],
        }

        # ── Upcoming deadlines (within window OR next 7 days) ───
        deadline_horizon = max(since + timedelta(days=7),
                               datetime.now(timezone.utc) + timedelta(days=7))
        deadline_rows = (await session.execute(text("""
            SELECT d.title, d.deadline_date, d.deadline_type, d.is_sol,
                   d.completed_at, m.matter_name, m.matter_number, m.id as matter_id
            FROM deadlines d
            LEFT JOIN matters m ON d.matter_id = m.id
            WHERE TRIM(d.tenant_id) = :tid
              AND d.deadline_date >= :since
              AND d.deadline_date <= :horizon
              AND d.completed_at IS NULL
            ORDER BY d.deadline_date ASC
            LIMIT 30
        """), {"tid": tid, "since": _naive(since),
               "horizon": _naive(deadline_horizon)})).mappings().fetchall()

        data["deadlines"] = {
            "count": len(deadline_rows),
            "sol_count": sum(1 for d in deadline_rows if d["is_sol"]),
            "items": [
                {
                    "title": r["title"],
                    "date": r["deadline_date"].isoformat() if r["deadline_date"] else None,
                    "type": r["deadline_type"],
                    "is_sol": r["is_sol"],
                    "matter": r["matter_name"],
                    "matter_id": str(r["matter_id"]) if r.get("matter_id") else None,
                }
                for r in deadline_rows[:15]
            ],
        }

        # ── Tasks created or updated ────────────────────────────
        task_rows = (await session.execute(text("""
            SELECT t.title, t.status, t.priority, t.due_date,
                   t.created_at, t.updated_at,
                   m.matter_name, m.matter_number, m.id as matter_id
            FROM tasks t
            LEFT JOIN matters m ON t.matter_id = m.id
            WHERE TRIM(t.tenant_id) = :tid
              AND (t.created_at >= :since
                   OR t.updated_at >= :since)
            ORDER BY t.updated_at DESC
            LIMIT 30
        """), {"tid": tid, "since": _naive(since)})).mappings().fetchall()

        new_tasks = [t for t in task_rows
                     if t["created_at"] and t["created_at"] >= _naive(since)]
        data["tasks"] = {
            "count": len(task_rows),
            "new_count": len(new_tasks),
            "overdue": sum(1 for t in task_rows
                          if t["due_date"]
                          and t["due_date"] < datetime.now().replace(tzinfo=None)
                          and t["status"] not in ("completed", "done")),
            "items": [
                {
                    "title": r["title"],
                    "status": r["status"],
                    "priority": r["priority"],
                    "due": r["due_date"].isoformat() if r["due_date"] else None,
                    "matter": r["matter_name"],
                    "matter_id": str(r["matter_id"]) if r.get("matter_id") else None,
                }
                for r in task_rows[:15]
            ],
        }

        # ── eDiscovery collections activity ─────────────────────
        edisco_rows = (await session.execute(text("""
            SELECT collection_name, status, total_docs, processed_docs, updated_at
            FROM ediscovery_collections
            WHERE TRIM(tenant_id) = :tid
              AND updated_at >= :since
            ORDER BY updated_at DESC
            LIMIT 10
        """), {"tid": tid, "since": since})).mappings().fetchall()

        data["ediscovery"] = {
            "count": len(edisco_rows),
            "items": [
                {
                    "name": r["collection_name"],
                    "status": r["status"],
                    "total": r["total_docs"],
                    "processed": r["processed_docs"],
                }
                for r in edisco_rows
            ],
        }

    return data


# ═══════════════════════════════════════════════════════════════
# GET /api/v1/fill-me-in — raw data (fast, no AI)
# ═══════════════════════════════════════════════════════════════

@router.get("")
async def fill_me_in_raw(
    request: Request,
    window: str = Query("since_login", regex="^(1h|2h|4h|8h|12h|24h|48h|7d|since_login)$"),
):
    user = _user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    tid = _tid(request)
    uid = _user_id(request)
    since = await _resolve_since(request, window)
    data = await _gather_data(tid, uid, since)

    return JSONResponse({
        "window": window,
        "since": since.isoformat(),
        "now": datetime.now(timezone.utc).isoformat(),
        **data,
    })


# ═══════════════════════════════════════════════════════════════
# POST /api/v1/fill-me-in/summary — AI briefing (SSE)
# ═══════════════════════════════════════════════════════════════

@router.post("/summary")
async def fill_me_in_summary(request: Request):
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
            yield f"data: {json.dumps({'type': 'error', 'text': 'No API key configured'})}\n\n"
        return StreamingResponse(err(), media_type="text/event-stream")

    body = await request.json()
    window = body.get("window", "since_login")
    if window not in WINDOW_MAP and window != "since_login":
        window = "since_login"

    uid = _user_id(request)
    since = await _resolve_since(request, window)
    data = await _gather_data(tid, uid, since)

    user_name = getattr(user, "full_name", None) or getattr(user, "username", "Attorney")

    # ── Build prompt ─────────────────────────────────────────────
    system_prompt = f"""You are Praesidium AI, briefing {user_name} on what happened while they were away.

RULES:
- Use bullet points for each actionable or noteworthy item. One bullet per item, terse — 2-second read each.
- Lead with the most urgent item (SOL deadlines, overdue tasks, high-importance emails).
- Mention specific matter names and sender names.
- Maximum 10 bullets. Maximum 5 sub-bullets.
- Exclude ingestion activity, production imports, and new file additions from the briefing.
- If nothing significant happened, write one plain sentence (no bullet): "No urgent deadlines or overdue tasks requiring immediate attention."
- Never say "here's your briefing" or similar preamble. Start with the first bullet.
- Start each bullet with a relevant emoji: 📧 email, ⚠️ deadline/urgent, 📋 task, 📄 document, ⚖️ legal/court, 🔍 eDiscovery, 📁 filing, 💰 billing.
- NEVER use markdown formatting. No asterisks, no bold markers, no underscores. Plain text only."""

    # Build a concise data summary for Claude
    parts = []
    since_label = since.strftime("%B %d at %I:%M %p") if window == "since_login" else f"the last {window}"
    parts.append(f"Time window: since {since_label}")

    e = data["emails"]
    if e["count"]:
        parts.append(f"EMAILS: {e['count']} new ({e['unrouted']} unrouted, {e['high_importance']} high-importance)")
        for item in e["items"][:10]:
            parts.append(f"  - From {item['from']}: \"{item['subject']}\"" +
                        (f" [{item['matter']}]" if item["matter"] else "") +
                        (f" ⚡HIGH" if item["importance"] == "high" else ""))
    else:
        parts.append("EMAILS: none")

    d = data["documents"]
    if d["count"]:
        parts.append(f"DOCUMENTS: {d['count']} new")
        for item in d["items"][:8]:
            parts.append(f"  - {item['name']}" +
                        (f" [{item['matter']}]" if item["matter"] else ""))
    else:
        parts.append("DOCUMENTS: none")

    dl = data["deadlines"]
    if dl["count"]:
        parts.append(f"DEADLINES: {dl['count']} upcoming ({dl['sol_count']} SOL)")
        for item in dl["items"][:8]:
            parts.append(f"  - {item['title']} — {item['date']}" +
                        (f" [{item['matter']}]" if item["matter"] else "") +
                        (" ⚠️SOL" if item["is_sol"] else ""))
    else:
        parts.append("DEADLINES: none upcoming")

    t = data["tasks"]
    if t["count"]:
        parts.append(f"TASKS: {t['count']} updated ({t['new_count']} new, {t['overdue']} overdue)")
        for item in t["items"][:8]:
            parts.append(f"  - {item['title']} [{item['status']}]" +
                        (f" [{item['matter']}]" if item["matter"] else "") +
                        (" ⚠️OVERDUE" if item.get("priority") == "urgent" else ""))
    else:
        parts.append("TASKS: none")

    ed = data["ediscovery"]
    if ed["count"]:
        parts.append(f"eDISCOVERY: {ed['count']} collections updated")
        for item in ed["items"][:5]:
            parts.append(f"  - {item['name']}: {item['status']} ({item['processed']}/{item['total']} docs)")

    user_prompt = "\n".join(parts)

    # ── Stream from Claude ───────────────────────────────────────
    api_body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 512,
        "stream": True,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    async def generate():
        # Emit raw counts first so UI can show them immediately
        yield f"data: {json.dumps({'type': 'counts', 'data': {k: v.get('count', 0) if isinstance(v, dict) else v for k, v in data.items()}})}\n\n"

        _accumulated_text = ""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                async with client.stream(
                    "POST",
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=api_body,
                ) as resp:
                    if resp.status_code != 200:
                        error_body = await resp.aread()
                        try:
                            err_msg = json.loads(error_body).get("error", {}).get("message", f"HTTP {resp.status_code}")
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
                                    break
                                try:
                                    evt = json.loads(data_str)
                                    evt_type = evt.get("type", "")
                                    if evt_type == "content_block_delta":
                                        delta = evt.get("delta", {})
                                        if delta.get("type") == "text_delta":
                                            _accumulated_text += delta['text']
                                            yield f"data: {json.dumps({'type': 'text', 'text': delta['text']})}\n\n"
                                    elif evt_type == "message_stop":
                                        pass
                                except json.JSONDecodeError:
                                    pass

        except httpx.TimeoutException:
            yield f"data: {json.dumps({'type': 'error', 'text': 'AI request timed out'})}\n\n"
        except Exception as e:
            log.exception("Fill Me In AI error")
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
