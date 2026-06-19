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
from core.services import comms_jmap as _cj
from core.services import stalwart_mailbox as sm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/comms", tags=["communications"])

TENANT_ID = "986c0fee-1390-43bb-ad28-8cd1db6de53f"

def _tid(r):
    return (getattr(r.state, "tenant_id", "") or "").strip() or TENANT_ID

def _uid(r):
    user = getattr(r.state, "current_user", None)
    return getattr(user, "id", None) if user else None


def _email(r):
    user = getattr(r.state, "current_user", None)
    return getattr(user, "email", None) if user else None


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
    folder: str = Query("inbox"),
    mailbox: Optional[str] = Query(None),
    matter_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=200),
    sort: str = Query("newest"),
    source: str = Query("firm"),
    time: str = Query("current"),
):
    # Live JMAP-backed list. source=firm|personal|combined, time=current|historic.
    email = _email(request)
    if not email:
        return JSONResponse({"messages": [], "total": 0, "page": page,
                             "page_size": page_size, "pages": 0})
    return JSONResponse(await _cj.view_messages(
        email, folder=folder, time=time, search=search, page=page,
        page_size=page_size, sort=sort, source=source))


# ── Single message detail (full body) ─────────────────────────────

@router.get("/email/messages/{message_id}")
async def email_message_detail(request: Request, message_id: str,
                               source: str = Query("firm")):
    # Full message body via live JMAP.
    email = _email(request)
    if not email:
        raise HTTPException(401, "No authenticated user")
    d = await _cj.view_message_detail(email, message_id, source=source)
    if not d:
        raise HTTPException(404, "Message not found")
    return JSONResponse(d)


# ── Conversation thread ───────────────────────────────────────────

@router.get("/email/thread/{conversation_id}")
async def email_thread(request: Request, conversation_id: str,
                       source: str = Query("firm")):
    # Conversation thread grouped by JMAP threadId.
    email = _email(request)
    if not email:
        return JSONResponse({"conversation_id": conversation_id, "messages": [], "count": 0})
    return JSONResponse(await _cj.view_thread(email, conversation_id, source=source))


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
    """File an email to a matter (the 'letter in the file'):
      1. JMAP projection -> message also appears in Matters/<matter> over IMAP.
      2. Durable copy -> the .eml lands in the matter's DMS 09-Email/ folder with
         a dms_documents row (source='email_file').
    Operates on a live JMAP message id; source selects the firm/personal box.
    """
    import os as _os, hashlib as _hashlib, re as _re
    body = await request.json()
    email_id = body.get("email_id")
    matter_id = body.get("matter_id")
    source = (body.get("source") or "firm").lower()
    if not email_id or not matter_id:
        raise HTTPException(400, "email_id and matter_id required")
    tid = _tid(request)
    user_email = _email(request)
    if not user_email:
        raise HTTPException(401, "No authenticated user")

    # Resolve matter -> client/matter names (DMS path + folder label).
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.matter_name, m.matter_number, c.client_name
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id AND TRIM(c.tenant_id) = :tid
            WHERE m.id = CAST(:mid AS uuid) AND TRIM(m.tenant_id) = :tid
        """), {"mid": matter_id, "tid": tid})
        mrow = r.mappings().fetchone()
    if not mrow:
        raise HTTPException(404, "Matter not found")

    # Resolve the mailbox connector backing this message's source.
    conn = await _cj._connector_for(user_email, source)
    if not conn or not conn.ok:
        raise HTTPException(400, f"No mailbox connector for source={source}")

    out = {"ok": True, "projected": False, "dms": False, "source": source}

    # (1) JMAP projection into Matters/<matter>.
    try:
        from core.services import matter_mail_folders as mmf
        folder = mmf.folder_name(mrow["matter_number"] or "",
                                 mrow["matter_name"] or "")
        parent_id = await sm.find_or_create_mailbox(
            conn.account_id, "Matters", auth=conn.auth, url=conn.jmap_url)
        child_id = await sm.find_or_create_mailbox(
            conn.account_id, folder, parent_id=parent_id,
            auth=conn.auth, url=conn.jmap_url)
        proj = await sm.add_message_to_mailbox(
            conn.account_id, email_id, child_id,
            auth=conn.auth, url=conn.jmap_url)
        out["projected"] = bool(proj.get("success"))
        if not proj.get("success"):
            out["projection_error"] = str(proj.get("error"))
    except Exception as e:
        logger.warning("[email/file] projection failed: %s", e)
        out["projection_error"] = str(e)

    # (2) Durable .eml copy into DMS 09-Email/ + dms_documents row.
    try:
        eml = await sm.download_eml(conn.account_id, email_id,
                                    auth=conn.auth, url=conn.jmap_url)
        if not eml:
            raise RuntimeError("empty .eml download")
        detail = await sm.get_message(conn.account_id, email_id,
                                      auth=conn.auth, url=conn.jmap_url)
        subj = (detail or {}).get("subject") or "message"
        recv = (detail or {}).get("receivedAt") or ""
        ts = _re.sub(r"[^0-9]", "", recv)[:14] or "00000000"
        safe = (_re.sub(r"[^\w\-. ]", "_", subj).strip()[:80]) or "message"

        root = _os.path.join("/mnt/praesidium", tid, "matters",
                             mrow["client_name"] or "_", mrow["matter_name"])
        email_dir = _os.path.join(root, "09-Email")
        _os.makedirs(email_dir, exist_ok=True)
        fpath = _os.path.join(email_dir, f"{ts}_{safe}.eml")
        n = 1
        while _os.path.exists(fpath):
            fpath = _os.path.join(email_dir, f"{ts}_{safe}-{n}.eml"); n += 1
        with open(fpath, "wb") as fh:
            fh.write(eml)
        fhash = _hashlib.sha256(eml).hexdigest()
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                INSERT INTO dms_documents
                    (tenant_id, file_path, folder_root, file_hash,
                     file_size_bytes, modified_at, ocr_status,
                     extraction_status, source)
                VALUES (:tid, :fp, :root, :hash, :sz, NOW(),
                        'not_applicable', 'pending', 'email_file')
                ON CONFLICT DO NOTHING
            """), {"tid": tid, "fp": fpath, "root": root,
                   "hash": fhash, "sz": len(eml)})
            await session.commit()
        out["dms"] = True
        out["dms_path"] = fpath
    except Exception as e:
        logger.warning("[email/file] dms copy failed: %s", e)
        out["dms_error"] = str(e)

    out["message"] = "Filed to matter" if (out["projected"] or out["dms"]) \
        else "Filing failed"
    return JSONResponse(out)


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

@router.get("/email/sources")
async def email_sources(request: Request):
    """Which mailbox sources the logged-in user can view (firm always;
    personal/combined only if they have a personal connector configured)."""
    email = _email(request)
    if not email:
        return JSONResponse({"sources": ["firm"], "has_personal": False})
    from core.services import mail_connectors as _mc
    has_personal = False
    personal_color = _mc.DEFAULT_PERSONAL_COLOR
    try:
        data = await _mc._load_personal_row(email)
        has_personal = bool(data)
        if data:
            personal_color = (data.get("config") or {}).get("color") or personal_color
    except Exception as exc:
        logger.warning("[email/sources] %s", exc)
    sources = ["firm"] + (["personal", "combined"] if has_personal else [])
    return JSONResponse({"sources": sources, "has_personal": has_personal,
                         "personal_color": personal_color})


@router.get("/email/stats")
async def email_stats(request: Request, source: str = Query("firm")):
    # Counts derived from live JMAP mailbox totals (summed across sources).
    email = _email(request)
    if not email:
        return JSONResponse({"inbox_total": 0, "inbox_unread": 0, "sent_total": 0})
    return JSONResponse(await _cj.view_stats(email, source=source))


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
