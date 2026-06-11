#!/usr/bin/env python3
"""
Communications Center API — provider-agnostic unified comms.
Channels: email (EWS/Graph/Gmail), SMS (Twilio — future), PBX (future).
All data flows through email_routing_queue (email channel) and future
comms_queue tables for SMS/PBX.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/comms", tags=["communications"])

TENANT_ID = "986c0fee-1390-43bb-ad28-8cd1db6de53f"

def _tid(r):
    return (getattr(r.state, "tenant_id", "") or "").strip() or TENANT_ID

def _uid(r):
    user = getattr(r.state, "current_user", None)
    return getattr(user, "id", None) if user else None


def _fix_row(d):
    """Fix non-JSON-serializable types from DB rows."""
    from decimal import Decimal
    for k, v in list(d.items()):
        if isinstance(v, Decimal):
            d[k] = float(v)
        elif hasattr(v, 'isoformat'):
            d[k] = v.isoformat()
        elif hasattr(v, 'hex') and not isinstance(v, (str, bytes)):
            d[k] = str(v)
    return d

# ── Channel registry (provider-agnostic) ──────────────────────────

@router.get("/channels")
async def list_channels(request: Request):
    """List available communication channels for tenant.
    Reads from tenant_connectors + connector_entity_map."""
    tid = _tid(request)
    uid = _uid(request)
    async with AsyncSessionLocal() as session:
        # Get connectors
        r = await session.execute(sa_text("""
            SELECT id::text, connector, connector_type, is_active, status,
                   config->>'ews_url' as ews_url,
                   last_sync_at::text
            FROM tenant_connectors
            WHERE TRIM(tenant_id) = :tid
              AND connector_type IN ('exchange','office365','google_workspace')
              AND is_active = true
        """), {"tid": tid})
        connectors = [dict(row) for row in r.mappings().fetchall()]

        # Get mailboxes mapped to current user
        r2 = await session.execute(sa_text("""
            SELECT entity_email, entity_display, entity_type, mapped_user_id
            FROM connector_entity_map
            WHERE TRIM(tenant_id) = :tid
              AND entity_type = 'mailbox'
              AND is_active = true
            ORDER BY entity_display
        """), {"tid": tid})
        mailboxes = [dict(row) for row in r2.mappings().fetchall()]

        # Email stats
        r3 = await session.execute(sa_text("""
            SELECT
              COUNT(*) as total,
              COUNT(*) FILTER (WHERE routing_status = 'pending') as pending,
              COUNT(*) FILTER (WHERE routing_status = 'matched') as matched,
              COUNT(*) FILTER (WHERE filed_to_dms = true OR filing_status = 'filed') as filed,
              COUNT(*) FILTER (WHERE is_read = false OR is_read IS NULL) as unread
            FROM email_routing_queue
            WHERE TRIM(tenant_id) = :tid
        """), {"tid": tid})
        stats = dict(r3.mappings().fetchone())

    channels = []

    # Email channel (from connectors)
    for c in connectors:
        provider = c["connector"]  # 'exchange', 'office365', 'google_workspace'
        channels.append({
            "channel_type": "email",
            "provider": provider,
            "connector_id": c["id"],
            "label": "Email" + (" (Exchange)" if provider == "exchange" else " (Office 365)" if provider == "office365" else " (Google)" if provider == "google_workspace" else ""),
            "icon": "email",
            "is_active": c["is_active"],
            "status": c["status"],
            "last_sync": c["last_sync_at"],
            "stats": stats,
            "mailboxes": [m for m in mailboxes],  # all for now; filter by user later
        })

    # SMS placeholder
    channels.append({
        "channel_type": "sms",
        "provider": None,
        "connector_id": None,
        "label": "SMS",
        "icon": "sms",
        "is_active": False,
        "status": "not_configured",
        "stats": {"total": 0},
        "mailboxes": [],
    })

    # PBX placeholder
    channels.append({
        "channel_type": "pbx",
        "provider": None,
        "connector_id": None,
        "label": "Phone",
        "icon": "phone",
        "is_active": False,
        "status": "not_configured",
        "stats": {"total": 0},
        "mailboxes": [],
    })

    return JSONResponse({
        "channels": channels,
        "user_mailboxes": [m for m in mailboxes if m["mapped_user_id"] == uid] if uid else mailboxes,
    })


# ── Email inbox / folder views ────────────────────────────────────

@router.get("/email/messages")
async def email_messages(
    request: Request,
    folder: str = Query("inbox", description="inbox|sent|filed|pending|matched|all"),
    mailbox: Optional[str] = Query(None, description="Filter by mailbox email"),
    matter_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=200),
    sort: str = Query("newest", description="newest|oldest"),
):
    """Paginated email message list with folder/mailbox/matter filters."""
    tid = _tid(request)
    offset = (page - 1) * page_size
    order = "DESC" if sort == "newest" else "ASC"

    wheres = ["TRIM(e.tenant_id) = :tid"]
    params = {"tid": tid, "lim": page_size, "off": offset}

    # Folder filters
    if folder == "inbox":
        # Operational inbox: last 60 days + ALL unread regardless of age. Don't touch unread.
        wheres.append("""(
            e.received_at >= NOW() - INTERVAL '60 days'
            OR e.is_read = false
            OR e.is_read IS NULL
        )""")
    elif folder == "historic":
        # Archival view: everything older than 60 days (immutable stream, nothing ever leaves)
        wheres.append("e.received_at < NOW() - INTERVAL '60 days'")
    elif folder == "sent":
        # sent emails have the user's mailbox in from_email
        uid = _uid(request)
        if uid:
            wheres.append("""e.from_email IN (
                SELECT entity_email FROM connector_entity_map
                WHERE TRIM(tenant_id) = :tid AND mapped_user_id = :uid AND entity_type = 'mailbox'
            )""")
            params["uid"] = uid
    elif folder == "filed":
        wheres.append("(e.filed_to_dms = true OR e.filing_status = 'filed')")
    elif folder == "pending":
        wheres.append("e.routing_status = 'pending'")
    elif folder == "matched":
        wheres.append("e.routing_status = 'matched'")
    elif folder == "drafts":
        wheres.append("1=0")  # placeholder

    if mailbox:
        # Show emails where mailbox is in from_email, to_emails, or cc_emails
        wheres.append("""(
            e.from_email = :mbx
            OR e.to_emails::text ILIKE '%%' || :mbx || '%%'
            OR e.cc_emails::text ILIKE '%%' || :mbx || '%%'
        )""")
        params["mbx"] = mailbox

    if matter_id:
        wheres.append("(e.matched_matter_id = CAST(:mid AS uuid) OR e.filed_matter_id = CAST(:mid AS uuid))")
        params["mid"] = matter_id

    if search:
        wheres.append("""(
            e.subject ILIKE '%%' || :q || '%%'
            OR e.from_display ILIKE '%%' || :q || '%%'
            OR e.from_email ILIKE '%%' || :q || '%%'
            OR e.body_preview ILIKE '%%' || :q || '%%'
        )""")
        params["q"] = search

    where_clause = " AND ".join(wheres)

    async with AsyncSessionLocal() as session:
        # Count
        rc = await session.execute(sa_text(f"SELECT COUNT(*) FROM email_routing_queue e WHERE {where_clause}"), params)
        total = rc.scalar() or 0

        # Messages
        r = await session.execute(sa_text(f"""
            SELECT
                e.id::text,
                e.subject,
                e.from_email,
                e.from_display,
                e.to_emails,
                e.cc_emails,
                e.received_at::text,
                e.body_preview,
                e.has_attachments,
                e.attachment_names,
                e.attachment_count,
                e.routing_status,
                e.matched_matter_id::text,
                e.match_confidence,
                e.match_signals,
                e.is_read,
                e.filed_to_dms,
                e.filing_status,
                e.filed_matter_id::text,
                e.importance,
                e.conversation_id,
                e.conversation_topic,
                m.matter_name,
                c.client_name
            FROM email_routing_queue e
            LEFT JOIN matters m ON (e.matched_matter_id = m.id OR e.filed_matter_id = m.id) AND TRIM(m.tenant_id) = :tid
            LEFT JOIN clients c ON m.client_id = c.id AND TRIM(c.tenant_id) = :tid
            WHERE {where_clause}
            ORDER BY e.received_at {order}
            LIMIT :lim OFFSET :off
        """), params)
        messages = [_fix_row(dict(row)) for row in r.mappings().fetchall()]

    return JSONResponse({
        "messages": messages,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    })


# ── Single message detail (full body) ─────────────────────────────

@router.get("/email/messages/{message_id}")
async def email_message_detail(request: Request, message_id: str):
    """Full email detail with body_text."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT
                e.*,
                e.id::text as id_str,
                e.matched_matter_id::text as matched_matter_id_str,
                e.filed_matter_id::text as filed_matter_id_str,
                m.matter_name,
                c.client_name,
                e.received_at::text as received_at_str
            FROM email_routing_queue e
            LEFT JOIN matters m ON (e.matched_matter_id = m.id OR e.filed_matter_id = m.id) AND TRIM(m.tenant_id) = :tid
            LEFT JOIN clients c ON m.client_id = c.id AND TRIM(c.tenant_id) = :tid
            WHERE e.id = CAST(:eid AS uuid) AND TRIM(e.tenant_id) = :tid
        """), {"eid": message_id, "tid": tid})
        row = r.mappings().fetchone()
    if not row:
        raise HTTPException(404, "Message not found")
    d = _fix_row(dict(row))
    return JSONResponse(d)


# ── Conversation thread ───────────────────────────────────────────

@router.get("/email/thread/{conversation_id}")
async def email_thread(request: Request, conversation_id: str):
    """Get all messages in a conversation thread."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT
                e.id::text, e.subject, e.from_email, e.from_display,
                e.to_emails, e.cc_emails, e.received_at::text,
                e.body_preview, e.body_text, e.has_attachments,
                e.attachment_names, e.routing_status,
                e.matched_matter_id::text, e.is_read, e.importance,
                m.matter_name
            FROM email_routing_queue e
            LEFT JOIN matters m ON e.matched_matter_id = m.id AND TRIM(m.tenant_id) = :tid
            WHERE TRIM(e.tenant_id) = :tid AND e.conversation_id = :cid
            ORDER BY e.received_at ASC
        """), {"tid": tid, "cid": conversation_id})
        msgs = [_fix_row(dict(row)) for row in r.mappings().fetchall()]
    return JSONResponse({"conversation_id": conversation_id, "messages": msgs, "count": len(msgs)})


# ── Search matters (for filing) ───────────────────────────────────

@router.get("/matters/search")
async def search_matters_for_filing(request: Request, q: str = Query(..., min_length=2)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.id::text, m.matter_name, m.matter_number, c.client_name
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id AND TRIM(c.tenant_id) = :tid
            WHERE TRIM(m.tenant_id) = :tid
              AND (m.matter_name ILIKE '%%' || :q || '%%' OR m.matter_number ILIKE '%%' || :q || '%%'
                   OR c.client_name ILIKE '%%' || :q || '%%')
            ORDER BY m.matter_name
            LIMIT 20
        """), {"tid": tid, "q": q})
        return JSONResponse({"results": [dict(row) for row in r.mappings().fetchall()]})


# ── File email to matter ──────────────────────────────────────────

@router.post("/email/file")
async def file_email_to_matter(request: Request):
    """File an email to a matter's DMS Email folder."""
    body = await request.json()
    email_id = body.get("email_id")
    matter_id = body.get("matter_id")
    if not email_id or not matter_id:
        raise HTTPException(400, "email_id and matter_id required")
    tid = _tid(request)
    uid = _uid(request)

    async with AsyncSessionLocal() as session:
        # Get email
        r = await session.execute(sa_text("""
            SELECT staging_path, subject, from_email, received_at
            FROM email_routing_queue
            WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"eid": email_id, "tid": tid})
        email_row = r.mappings().fetchone()
        if not email_row:
            raise HTTPException(404, "Email not found")

        # Update routing
        await session.execute(sa_text("""
            UPDATE email_routing_queue
            SET filed_to_dms = true, filing_status = 'filed',
                filed_matter_id = CAST(:mid AS uuid),
                filed_at = NOW(), filed_by = :uid,
                matched_matter_id = COALESCE(matched_matter_id, CAST(:mid AS uuid)),
                routing_status = 'matched'
            WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"eid": email_id, "mid": matter_id, "uid": uid, "tid": tid})
        await session.commit()

    return JSONResponse({"ok": True, "message": "Filed to matter"})


# ── Skip / reject email ──────────────────────────────────────────

@router.post("/email/skip")
async def skip_email(request: Request):
    body = await request.json()
    email_id = body.get("email_id")
    if not email_id:
        raise HTTPException(400, "email_id required")
    tid = _tid(request)

    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            UPDATE email_routing_queue
            SET routing_status = 'skipped'
            WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"eid": email_id, "tid": tid})
        await session.commit()

    return JSONResponse({"ok": True})


# ── Email stats summary ──────────────────────────────────────────

@router.get("/email/stats")
async def email_stats(request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT
              COUNT(*) as total,
              COUNT(*) FILTER (WHERE routing_status = 'pending') as pending,
              COUNT(*) FILTER (WHERE routing_status = 'matched') as matched,
              COUNT(*) FILTER (WHERE routing_status = 'skipped') as skipped,
              COUNT(*) FILTER (WHERE filed_to_dms = true OR filing_status = 'filed') as filed,
              COUNT(*) FILTER (WHERE is_read = false OR is_read IS NULL) as unread,
              COUNT(DISTINCT conversation_id) FILTER (WHERE conversation_id IS NOT NULL) as threads,
              COUNT(DISTINCT from_email) as unique_senders,
              MIN(received_at)::text as earliest,
              MAX(received_at)::text as latest
            FROM email_routing_queue
            WHERE TRIM(tenant_id) = :tid
        """), {"tid": tid})
        return JSONResponse(_fix_row(dict(r.mappings().fetchone())))


# ── DMS folder tree (for drag-drop filing) ────────────────────────

@router.get("/email/dms-folders")
async def dms_folder_tree(request: Request, matter_id: str = Query(...)):
    """Return folder tree with files for a matter's DMS root."""
    import os as _os
    tid = _tid(request)
    PROOT = "/mnt/praesidium"
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.matter_name, c.client_name
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id AND TRIM(c.tenant_id) = :tid
            WHERE m.id = CAST(:mid AS uuid) AND TRIM(m.tenant_id) = :tid
        """), {"mid": matter_id, "tid": tid})
        row = r.mappings().fetchone()
    if not row:
        raise HTTPException(404, "Matter not found")
    root = _os.path.join(PROOT, tid, "matters", row["client_name"], row["matter_name"])
    if not _os.path.isdir(root):
        return JSONResponse({"folders": [], "root": root})

    def _fmt_size(n):
        if n < 1024: return f"{n} B"
        if n < 1048576: return f"{n/1024:.1f} KB"
        return f"{n/1048576:.1f} MB"

    folders = []
    for entry in sorted(_os.listdir(root)):
        fp = _os.path.join(root, entry)
        if _os.path.isdir(fp):
            subs = []
            files = []
            for child in sorted(_os.listdir(fp)):
                cp = _os.path.join(fp, child)
                if _os.path.isdir(cp):
                    # Sub-subfolder with its files
                    sub_files = []
                    try:
                        for sf in sorted(_os.listdir(cp)):
                            sfp = _os.path.join(cp, sf)
                            if _os.path.isfile(sfp):
                                st = _os.stat(sfp)
                                sub_files.append({"name": sf, "size": st.st_size, "size_fmt": _fmt_size(st.st_size),
                                    "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                                    "path": _os.path.relpath(sfp, root)})
                    except: pass
                    subs.append({"name": child, "path": _os.path.relpath(cp, root), "files": sub_files[:50]})
                elif _os.path.isfile(cp):
                    st = _os.stat(cp)
                    files.append({"name": child, "size": st.st_size, "size_fmt": _fmt_size(st.st_size),
                        "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                        "path": _os.path.relpath(cp, root)})
            folders.append({"name": entry, "path": entry, "subfolders": subs, "files": files[:100]})
    return JSONResponse({"folders": folders, "root": root, "matter_name": row["matter_name"], "matter_id": matter_id})


@router.get("/email/dms-file")
async def serve_dms_file(request: Request, matter_id: str = Query(...), file_path: str = Query(...)):
    """Serve a file from a matter's DMS folder for viewing."""
    import os as _os, mimetypes
    from fastapi.responses import FileResponse
    tid = _tid(request)
    PROOT = "/mnt/praesidium"
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.matter_name, c.client_name
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id AND TRIM(c.tenant_id) = :tid
            WHERE m.id = CAST(:mid AS uuid) AND TRIM(m.tenant_id) = :tid
        """), {"mid": matter_id, "tid": tid})
        row = r.mappings().fetchone()
    if not row:
        raise HTTPException(404, "Matter not found")
    root = _os.path.join(PROOT, tid, "matters", row["client_name"], row["matter_name"])
    full = _os.path.realpath(_os.path.join(root, file_path))
    if not full.startswith(_os.path.realpath(root)):
        raise HTTPException(403, "Path traversal denied")
    if not _os.path.isfile(full):
        raise HTTPException(404, "File not found")
    mt = mimetypes.guess_type(full)[0] or "application/octet-stream"
    return FileResponse(full, media_type=mt, filename=_os.path.basename(full))





# ── Autofile job state (in-memory, single-worker) ──
import threading, uuid as _uuid, time as _time

_autofile_jobs = {}  # job_id -> {status, progress, total, phase, results, started_at, finished_at}
_autofile_lock = threading.Lock()


def _run_autofile_bg(job_id: str, tid: str, user_obj):
    """Background thread: auto-route then file."""
    import asyncio, json as _json
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_autofile_async(job_id, tid, user_obj))
    except Exception as exc:
        logger.exception("Autofile bg error: %s", exc)
        with _autofile_lock:
            _autofile_jobs[job_id]["status"] = "error"
            _autofile_jobs[job_id]["error"] = str(exc)
    finally:
        loop.close()


async def _run_autofile_async(job_id: str, tid: str, user_obj):
    """Async autofile pipeline with progress updates."""
    import json as _json

    def _update(fields):
        with _autofile_lock:
            _autofile_jobs[job_id].update(fields)

    _update({"phase": "counting", "status": "running"})

    # Count what we have
    async with AsyncSessionLocal() as session:
        counts = (await session.execute(sa_text("""
            SELECT
              COUNT(*) FILTER (WHERE routing_status = 'pending' AND is_read = true) as read_pending,
              COUNT(*) FILTER (WHERE routing_status = 'pending' AND (is_read = false OR is_read IS NULL)) as unread_pending
            FROM email_routing_queue
            WHERE TRIM(tenant_id) = :tid
        """), {"tid": tid})).mappings().fetchone()

    read_pending = counts["read_pending"] or 0
    unread_pending = counts["unread_pending"] or 0
    _update({"phase": "routing", "total": read_pending, "progress": 0,
             "skipped_unread": unread_pending, "detail": f"Auto-routing {read_pending} read emails..."})

    # ── Phase 1: Auto-route ──
    try:
        from modules.tenant_admin.email_sync_api import auto_route_emails

        class _FakeState:
            tenant_id = tid
            current_user = user_obj

        class _FakeReq:
            state = _FakeState()
            async def json(self):
                return {}

        route_resp = await auto_route_emails(_FakeReq(), user=user_obj)
        route_data = _json.loads(route_resp.body)
        route_results = route_data.get("results", {})
        total_matched = sum(v for k, v in route_results.items() if k != "unmatched")
        _update({"phase": "routing_done", "route_results": route_results,
                 "detail": f"Routed {total_matched}, {route_results.get('unmatched', 0)} unmatched"})
    except Exception as e:
        logger.exception("Auto-route phase failed: %s", e)
        _update({"route_results": {"error": str(e)}, "phase": "routing_done",
                 "detail": f"Route error: {e}"})

    # ── Phase 2: File matched emails ──
    async with AsyncSessionLocal() as session:
        to_file = (await session.execute(sa_text("""
            SELECT id::text, matched_matter_id::text
            FROM email_routing_queue
            WHERE TRIM(tenant_id) = :tid
              AND routing_status = 'matched'
              AND (filing_status IS NULL OR filing_status = 'pending')
              AND matched_matter_id IS NOT NULL
              AND is_read = true
            ORDER BY received_at
            LIMIT 500
        """), {"tid": tid})).fetchall()

    file_total = len(to_file)
    _update({"phase": "filing", "file_total": file_total, "filed": 0, "file_errors": 0,
             "detail": f"Filing {file_total} matched emails to DMS..."})

    if file_total > 0:
        from modules.tenant_admin.email_sync_api import file_email_to_matter as _file_fn

        filed = 0
        errors = 0
        for i, row in enumerate(to_file):
            try:
                class _FileReq:
                    state = _FakeState()
                    async def json(self_inner):
                        return {"email_id": row[0], "matter_id": row[1]}

                resp = await _file_fn(_FileReq(), user=user_obj)
                data = _json.loads(resp.body)
                if data.get("ok"):
                    filed += 1
                else:
                    errors += 1
            except Exception as exc:
                errors += 1
                logger.warning("File error %s: %s", row[0], exc)

            # Update progress every 5 emails
            if (i + 1) % 5 == 0 or i == file_total - 1:
                _update({"filed": filed, "file_errors": errors, "progress": i + 1,
                         "detail": f"Filed {filed}/{file_total} ({errors} errors)"})

    # ── Done ──
    _update({"status": "done", "phase": "complete", "finished_at": datetime.now(timezone.utc).isoformat(),
             "detail": f"Complete: {_autofile_jobs[job_id].get('filed', 0)} filed"})


@router.post("/email/autofile-batch")
async def autofile_batch(request: Request):
    """Kick off autofile as a background thread. Returns job_id for polling."""
    tid = _tid(request)
    user_obj = getattr(request.state, "current_user", None)

    job_id = str(_uuid.uuid4())[:8]
    with _autofile_lock:
        _autofile_jobs[job_id] = {
            "status": "starting",
            "phase": "init",
            "progress": 0,
            "total": 0,
            "filed": 0,
            "file_errors": 0,
            "file_total": 0,
            "route_results": {},
            "skipped_unread": 0,
            "detail": "Starting...",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "error": None,
        }

    t = threading.Thread(target=_run_autofile_bg, args=(job_id, tid, user_obj), daemon=True)
    t.start()

    return JSONResponse({"ok": True, "job_id": job_id, "message": "Autofile started"})


@router.get("/email/autofile-status")
async def autofile_status(request: Request, job_id: str = ""):
    """Poll autofile job progress."""
    if not job_id:
        return JSONResponse({"error": "job_id required"}, status_code=400)
    with _autofile_lock:
        job = _autofile_jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse(job)


@router.post("/email/mark-read")
async def mark_email_read(request: Request):
    """Mark one or more emails as read or unread."""
    body = await request.json()
    email_ids = body.get("email_ids", [])
    is_read = body.get("is_read", True)
    if not email_ids:
        raise HTTPException(400, "email_ids required")
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        for eid in email_ids:
            await session.execute(sa_text(
                "UPDATE email_routing_queue SET is_read = :rd WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid"
            ), {"rd": is_read, "eid": eid, "tid": tid})
        await session.commit()
    return JSONResponse({"ok": True, "count": len(email_ids), "is_read": is_read})


@router.get("/meetings/upcoming")
async def get_upcoming_meetings(request: Request, limit: int = 6):
    """Upcoming meetings from exchange_calendar_events."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        return JSONResponse({"meetings": []})
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                sa_text("""SELECT id, subject, start_time, end_time, location, organizer, attendees,
                         mailbox, is_all_day, categories
                  FROM exchange_calendar_events
                  WHERE TRIM(tenant_id) = TRIM(:tid)
                    AND start_time >= NOW()
                  ORDER BY start_time ASC
                  LIMIT :lim"""),
                {"tid": tenant_id, "lim": limit}
            )
            rows = result.fetchall()
            meetings = []
            for r in rows:
                att = r.attendees if r.attendees else []
                if isinstance(att, str):
                    import json as _json
                    try: att = _json.loads(att)
                    except: att = []
                meetings.append({
                    "id": str(r.id),
                    "subject": r.subject,
                    "start_time": r.start_time.isoformat() if r.start_time else None,
                    "end_time": r.end_time.isoformat() if r.end_time else None,
                    "location": r.location,
                    "organizer": r.organizer,
                    "attendees_count": len(att) if isinstance(att, list) else 0,
                    "mailbox": r.mailbox,
                })
            return JSONResponse({"meetings": meetings})
    except Exception as e:
        return JSONResponse({"meetings": [], "error": str(e)})


@router.get("/meetings/recent")
async def get_recent_meetings(request: Request, limit: int = 10):
    """Recent past meetings from exchange_calendar_events."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        return JSONResponse({"meetings": []})
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                sa_text("""SELECT id, subject, start_time, end_time, location, organizer, attendees,
                         mailbox, is_all_day, categories
                  FROM exchange_calendar_events
                  WHERE TRIM(tenant_id) = TRIM(:tid)
                    AND start_time < NOW()
                    AND start_time > NOW() - INTERVAL '30 days'
                  ORDER BY start_time DESC
                  LIMIT :lim"""),
                {"tid": tenant_id, "lim": limit}
            )
            rows = result.fetchall()
            meetings = []
            for r in rows:
                att = r.attendees if r.attendees else []
                if isinstance(att, str):
                    import json as _json
                    try: att = _json.loads(att)
                    except: att = []
                meetings.append({
                    "id": str(r.id),
                    "subject": r.subject,
                    "start_time": r.start_time.isoformat() if r.start_time else None,
                    "end_time": r.end_time.isoformat() if r.end_time else None,
                    "location": r.location,
                    "organizer": r.organizer,
                    "attendees_count": len(att) if isinstance(att, list) else 0,
                    "mailbox": r.mailbox,
                })
            return JSONResponse({"meetings": meetings})
    except Exception as e:
        return JSONResponse({"meetings": [], "error": str(e)})
