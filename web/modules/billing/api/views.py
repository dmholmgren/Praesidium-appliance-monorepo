"""HTMX view routes — serves billing templates with branding context."""
from datetime import date, timedelta
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.billing.brand_helper import get_brand

views = APIRouter(tags=["billing-views"])


def _jsonable(obj):
    import uuid, datetime
    if isinstance(obj, list): return [_jsonable(i) for i in obj]
    if isinstance(obj, dict): return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, uuid.UUID): return str(obj)
    if isinstance(obj, (datetime.datetime, datetime.date)): return obj.isoformat()
    return obj

templates = Jinja2Templates(directory=[
    "core/templates",
    "modules/billing/templates",
])


def _ctx(request, **kwargs):
    return {"request": request, "brand": get_brand(request), "page": "billing", **kwargs}


def _get_sync_db(tenant_id: str):
    """Get a sync TenantSession for the billing service layer."""
    from core.db.base import _SyncSessionLocal, TenantSession
    return TenantSession(_SyncSessionLocal(), tenant_id)


# ---------------------------------------------------------------------------
# Billing home — full page wrapper (extends base.html, has tab bar)
# ---------------------------------------------------------------------------

@views.get("/billing/", response_class=HTMLResponse)
@views.get("/billing", response_class=HTMLResponse)
async def billing_home(request: Request):
    user = getattr(request.state, "current_user", None)
    return templates.TemplateResponse(
        request,
        "billing/billing_home.html",
        _ctx(request, user=user, current_user=user, page="billing_home"),
    )


# ---------------------------------------------------------------------------
# Billing overview — three-column HTMX partial (loaded into billing_home body)
# ---------------------------------------------------------------------------

@views.get("/billing/overview", response_class=HTMLResponse)
async def billing_overview(request: Request):
    """
    HTMX partial — three-column billing overview.
    Loaded into #bil-body by billing_home.html on DOMContentLoaded.
    No data — all widgets load via hx-get.
    """
    user = getattr(request.state, "current_user", None)
    return templates.TemplateResponse(
        request,
        "billing/billing_overview.html",
        _ctx(request, user=user, current_user=user, page="billing_home"),
    )


@views.get("/billing/clients", response_class=HTMLResponse)
async def clients_page(request: Request):
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user = getattr(request.state, "current_user", None)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, client_name
                FROM clients
                WHERE trim(tenant_id) = trim(:tid)
                ORDER BY client_name
            """),
            {"tid": tenant_id},
        )
        clients = [dict(r._mapping) for r in result.fetchall()]
    return templates.TemplateResponse(
        request,
        "billing/clients.html",
        _ctx(request, clients=clients, user=user, current_user=user, page="clients"),
    )


@views.get("/billing/timesheet", response_class=HTMLResponse)
async def timesheet_page(request: Request):
    """Timesheet Reconciliation dashboard — session list, upload form, start."""
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user = getattr(request.state, "current_user", None)
    today = date.today()
    default_from = today.replace(day=1)
    default_to = today

    async with AsyncSessionLocal() as db:
        user_id = getattr(user, "id", 0) if user else 0
        rows = await db.execute(
            text("""
                SELECT id, date_from, date_to, status,
                       draft_count, approved_count, pushed_count,
                       created_at, completed_at, error_message
                FROM timesheet_sessions
                WHERE trim(tenant_id) = trim(:tid) AND user_id = :uid
                ORDER BY created_at DESC LIMIT 20
            """),
            {"tid": tenant_id, "uid": user_id},
        )
        sessions = [dict(r) for r in rows.mappings().fetchall()]

    return templates.TemplateResponse(
        request,
        "billing/timesheet_dashboard.html",
        _ctx(request, sessions=sessions, tenant_id=tenant_id,
             default_from=default_from.isoformat(),
             default_to=default_to.isoformat(),
             user=user, current_user=user, page="timesheet"),
    )


@views.post("/billing/timesheet/start")
async def timesheet_start(request: Request):
    """Start a reconciliation session — enqueues RQ job."""
    import uuid as _uuid, os, json
    from fastapi import UploadFile
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user = getattr(request.state, "current_user", None)
    user_id = getattr(user, "id", 0) if user else 0
    user_name = getattr(user, "full_name", "") or getattr(user, "username", "") if user else ""

    form = await request.form()
    date_from = str(form.get("date_from", ""))
    date_to = str(form.get("date_to", ""))

    try:
        df = date.fromisoformat(date_from)
        dt = date.fromisoformat(date_to)
    except ValueError:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/billing/timesheet", status_code=303)

    session_id = str(_uuid.uuid4())

    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""
                INSERT INTO timesheet_sessions
                  (id, tenant_id, user_id, date_from, date_to, status)
                VALUES (:id, :tid, :uid, :df, :dt, 'running')
            """),
            {"id": session_id, "tid": tenant_id, "uid": user_id,
             "df": df, "dt": dt},
        )
        await db.commit()

    # Read uploaded files — tag by drop zone field name
    file_data = []
    for field_name in ("phone_files", "imazing_files", "files"):
        for item in form.getlist(field_name):
            if hasattr(item, "filename") and item.filename:
                content = await item.read()
                # Tag with source type based on which drop zone
                if field_name == "imazing_files":
                    tagged_name = "imazing_" + item.filename
                elif field_name == "phone_files":
                    tagged_name = item.filename  # phone CSV detection is the default
                else:
                    tagged_name = item.filename
                file_data.append({
                    "filename": tagged_name,
                    "content_type": getattr(item, "content_type", ""),
                    "content_b64": content.hex(),
                })

    # Enqueue RQ job
    try:
        from redis import Redis
        from rq import Queue
        REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
        redis_conn = Redis.from_url(REDIS_URL)
        q = Queue("default", connection=redis_conn)
        q.enqueue(
            "jobs.timesheet_reconcile_job.run",
            session_id, tenant_id, user_id, user_name,
            df.isoformat(), dt.isoformat(), file_data,
            job_timeout=600,
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("Failed to enqueue timesheet job: %s", exc)
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("""UPDATE timesheet_sessions
                        SET status='failed', error_message=:err, completed_at=NOW()
                        WHERE id=:id"""),
                {"err": str(exc)[:300], "id": session_id},
            )
            await db.commit()

    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/billing/timesheet/{session_id}", status_code=303)


@views.get("/billing/timesheet/{session_id}", response_class=HTMLResponse)
async def timesheet_review(request: Request, session_id: str, filter: str = "pending"):
    """Review drafts for a reconciliation session."""
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user = getattr(request.state, "current_user", None)
    user_id = getattr(user, "id", 0) if user else 0

    async with AsyncSessionLocal() as db:
        session_row = await db.execute(
            text("""
                SELECT id, date_from, date_to, status,
                       draft_count, approved_count, pushed_count,
                       error_message, created_at, completed_at
                FROM timesheet_sessions
                WHERE id = :sid AND trim(tenant_id) = trim(:tid)
            """),
            {"sid": session_id, "tid": tenant_id},
        )
        session = session_row.mappings().fetchone()
        if not session:
            from fastapi.responses import RedirectResponse
            return RedirectResponse("/billing/timesheet", status_code=303)

        if filter == "all":
            status_filter = ""
        elif filter == "approved":
            status_filter = "AND status = 'approved'"
        elif filter == "rejected":
            status_filter = "AND status = 'rejected'"
        else:
            status_filter = "AND status = 'pending'"

        drafts_row = await db.execute(
            text(f"""
                SELECT id, entry_date, matter_id, matter_name,
                       hours, description, ai_narrative, source,
                       ai_confidence, status, source_detail,
                       reviewed_at, time_entry_id
                FROM timesheet_drafts
                WHERE session_id = :sid {status_filter}
                ORDER BY entry_date ASC, ai_confidence DESC
            """),
            {"sid": session_id},
        )
        drafts = [dict(r) for r in drafts_row.mappings().fetchall()]

        # Active matters for edit dropdown
        matters_row = await db.execute(
            text("""
                SELECT id, matter_name FROM matters
                WHERE trim(tenant_id) = trim(:tid)
                  AND LOWER(status) IN ('active', 'open')
                ORDER BY matter_name
            """),
            {"tid": tenant_id},
        )
        matters = {str(r["id"]): r["matter_name"]
                   for r in matters_row.mappings().fetchall()}

        pending_row = await db.execute(
            text("SELECT COUNT(*) AS cnt FROM timesheet_drafts WHERE session_id = :sid AND status = 'pending'"),
            {"sid": session_id},
        )
        pending_count = pending_row.scalar() or 0

    return templates.TemplateResponse(
        request,
        "billing/timesheet_review.html",
        _ctx(request, session=dict(session), drafts=drafts,
             tenant_id=tenant_id, filter=filter,
             matters=matters, pending_count=pending_count,
             user=user, current_user=user, page="timesheet"),
    )


@views.post("/billing/timesheet/{session_id}/approve/{draft_id}")
async def draft_approve(request: Request, session_id: str, draft_id: str):
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user = getattr(request.state, "current_user", None)
    user_id = getattr(user, "id", 0) if user else 0
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""UPDATE timesheet_drafts SET status='approved', reviewed_at=NOW(), reviewed_by=:uid
                    WHERE id=:did AND session_id=:sid"""),
            {"uid": user_id, "did": draft_id, "sid": session_id},
        )
        await db.execute(
            text("""UPDATE timesheet_sessions SET approved_count=(
                    SELECT COUNT(*) FROM timesheet_drafts WHERE session_id=:sid AND status='approved')
                    WHERE id=:sid"""),
            {"sid": session_id},
        )
        await db.commit()
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/billing/timesheet/{session_id}", status_code=303)


@views.post("/billing/timesheet/{session_id}/reject/{draft_id}")
async def draft_reject(request: Request, session_id: str, draft_id: str):
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user = getattr(request.state, "current_user", None)
    user_id = getattr(user, "id", 0) if user else 0
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""UPDATE timesheet_drafts SET status='rejected', reviewed_at=NOW(), reviewed_by=:uid
                    WHERE id=:did AND session_id=:sid"""),
            {"uid": user_id, "did": draft_id, "sid": session_id},
        )
        await db.commit()
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/billing/timesheet/{session_id}", status_code=303)


@views.post("/billing/timesheet/{session_id}/edit/{draft_id}")
async def draft_edit(request: Request, session_id: str, draft_id: str):
    import math
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user = getattr(request.state, "current_user", None)
    user_id = getattr(user, "id", 0) if user else 0
    form = await request.form()
    hours = float(form.get("hours", 0))
    hours = math.ceil(hours * 4) / 4
    description = str(form.get("description", ""))
    matter_id = str(form.get("matter_id", "")) or None
    matter_name = str(form.get("matter_name", "")) or None

    async with AsyncSessionLocal() as db:
        # If matter_id provided but no matter_name, look it up
        if matter_id and not matter_name:
            mr = await db.execute(
                text("SELECT matter_name FROM matters WHERE id = CAST(:mid AS uuid)"),
                {"mid": matter_id},
            )
            row = mr.mappings().fetchone()
            if row:
                matter_name = row["matter_name"]

        await db.execute(
            text("""UPDATE timesheet_drafts
                    SET hours=:hours, description=:desc, matter_id=CAST(:mid AS uuid),
                        matter_name=:mname, status='approved', reviewed_at=NOW(), reviewed_by=:uid
                    WHERE id=:did AND session_id=:sid"""),
            {"hours": hours, "desc": description, "mid": matter_id,
             "mname": matter_name, "uid": user_id, "did": draft_id, "sid": session_id},
        )
        await db.execute(
            text("""UPDATE timesheet_sessions SET approved_count=(
                    SELECT COUNT(*) FROM timesheet_drafts WHERE session_id=:sid AND status='approved')
                    WHERE id=:sid"""),
            {"sid": session_id},
        )
        await db.commit()
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/billing/timesheet/{session_id}", status_code=303)


@views.post("/billing/timesheet/{session_id}/push")
async def push_to_time_entries(request: Request, session_id: str):
    """Push approved drafts to time_entries — THIS is where audit fires."""
    import uuid as _uuid
    from core.audit import write_audit
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user = getattr(request.state, "current_user", None)
    user_id = getattr(user, "id", 0) if user else 0
    pushed = 0

    async with AsyncSessionLocal() as db:
        drafts_row = await db.execute(
            text("""SELECT id, entry_date, matter_id, hours, description,
                           ai_narrative, source, ai_confidence
                    FROM timesheet_drafts
                    WHERE session_id=:sid AND status='approved' AND time_entry_id IS NULL"""),
            {"sid": session_id},
        )
        approved = [dict(r) for r in drafts_row.mappings().fetchall()]

    for draft in approved:
        try:
            entry_id = str(_uuid.uuid4())
            narrative = draft.get("ai_narrative") or draft.get("description") or ""
            async with AsyncSessionLocal() as db2:
                await db2.execute(
                    text("""INSERT INTO time_entries
                            (id, tenant_id, matter_id, user_id, description,
                             hours, date, entry_date, billable, status,
                             source, ai_confidence, ai_original_description,
                             created_at, updated_at)
                            VALUES (:id, :tid, CAST(:mid AS uuid), :uid, :desc,
                                    :hours, :edate, :edate, true, 'draft',
                                    :source, :conf, :orig_desc, NOW(), NOW())"""),
                    {"id": entry_id, "tid": tenant_id,
                     "mid": draft.get("matter_id"), "uid": user_id,
                     "desc": narrative, "hours": draft["hours"],
                     "edate": draft["entry_date"],
                     "source": draft.get("source", "timesheet_reconciliation"),
                     "conf": draft.get("ai_confidence"),
                     "orig_desc": draft.get("description")},
                )
                await db2.execute(
                    text("UPDATE timesheet_drafts SET status='pushed', time_entry_id=:eid WHERE id=:did"),
                    {"eid": entry_id, "did": draft["id"]},
                )
                await db2.commit()
            pushed += 1
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Push draft %s failed: %s", draft["id"], exc)

    async with AsyncSessionLocal() as db3:
        await db3.execute(
            text("""UPDATE timesheet_sessions
                    SET pushed_count=:pushed, status='complete', completed_at=NOW()
                    WHERE id=:sid"""),
            {"pushed": pushed, "sid": session_id},
        )
        await db3.commit()

    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/billing/timesheet/{session_id}?filter=all", status_code=303)


@views.get("/billing/timesheet/{session_id}/status", response_class=HTMLResponse)
async def timesheet_status(request: Request, session_id: str):
    """HTMX poll — status badge."""
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    async with AsyncSessionLocal() as db:
        row = await db.execute(
            text("SELECT status, draft_count, approved_count FROM timesheet_sessions WHERE id=:sid AND trim(tenant_id)=trim(:tid)"),
            {"sid": session_id, "tid": tenant_id},
        )
        s = row.mappings().fetchone()
    if not s:
        return HTMLResponse('<span style="color:red;">Not found</span>')
    if s["status"] == "running":
        return HTMLResponse(
            f'<span style="padding:2px 8px; background:#DBEAFE; color:#1E40AF; border-radius:10px; font-size:11px;"'
            f' hx-get="/billing/timesheet/{session_id}/status" hx-trigger="every 3s" hx-swap="outerHTML">Running…</span>'
            f'<span style="font-size:11px; color:#64748b; margin-left:6px;">{s["draft_count"]} drafts</span>')
    if s["status"] == "complete":
        return HTMLResponse(
            f'<span style="padding:2px 8px; background:#DCFCE7; color:#166534; border-radius:10px; font-size:11px;">Complete</span>'
            f'<span style="font-size:11px; color:#64748b; margin-left:6px;">{s["draft_count"]} drafts · {s["approved_count"]} approved</span>')
    return HTMLResponse(f'<span style="padding:2px 8px; background:#FEE2E2; color:#991B1B; border-radius:10px; font-size:11px;">{s["status"]}</span>')


@views.post("/billing/timesheet/{session_id}/ai-escalate")
async def ai_escalate(request: Request, session_id: str):
    """Opt-in AI matching stub — no tokens burned."""
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    async with AsyncSessionLocal() as db:
        row = await db.execute(
            text("SELECT COUNT(*) AS cnt FROM timesheet_drafts WHERE session_id=:sid AND ai_confidence=0 AND status='pending'"),
            {"sid": session_id},
        )
        unmatched = row.scalar() or 0
    from fastapi.responses import JSONResponse
    return JSONResponse({"status": "stub", "message": "AI escalation not yet wired.", "unmatched_count": unmatched, "matched": 0})


@views.get("/billing/trust", response_class=HTMLResponse)
async def trust_page(request: Request):
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user = getattr(request.state, "current_user", None)
    async with AsyncSessionLocal() as session:
        # trust_ledger is a balance table: id, client_id, balance, last_reconciled_at
        result = await session.execute(
            text("""
                SELECT tl.id, tl.client_id,
                       tl.balance, tl.last_reconciled_at,
                       COALESCE(c.client_name, '') AS client_name
                FROM trust_ledger tl
                LEFT JOIN clients c ON tl.client_id = c.id
                                    AND trim(c.tenant_id) = trim(:tid)
                WHERE trim(tl.tenant_id) = trim(:tid)
                ORDER BY c.client_name
            """),
            {"tid": tenant_id},
        )
        ledgers = [_jsonable(dict(r._mapping)) for r in result.fetchall()]
        total_trust_balance = sum(float(l.get("balance") or 0) for l in ledgers)
    return templates.TemplateResponse(
        request,
        "billing/trust.html",
        _ctx(request, ledgers=ledgers, total_trust_balance=total_trust_balance,
             user=user, current_user=user, page="trust"),
    )


@views.get("/billing/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user = getattr(request.state, "current_user", None)
    async with AsyncSessionLocal() as session:
        clients_result = await session.execute(
            text("SELECT id, client_name FROM clients "
                 "WHERE trim(tenant_id) = trim(:tid) ORDER BY client_name"),
            {"tid": tenant_id},
        )
        clients = [dict(r._mapping) for r in clients_result.fetchall()]
        attorneys_result = await session.execute(
            text("SELECT ts_tk_id AS id, COALESCE(ts_name, ts_initials, ts_tk_id) AS name "
                 "FROM ts_timekeepers WHERE trim(tenant_id) = trim(:tid) ORDER BY name"),
            {"tid": tenant_id},
        )
        attorneys = [dict(r._mapping) for r in attorneys_result.fetchall()]
    return templates.TemplateResponse(
        request,
        "billing/reports.html",
        _ctx(request, clients=clients, attorneys=attorneys,
             user=user, current_user=user, page="reports"),
    )


@views.get("/billing/clients/{client_id}", response_class=HTMLResponse)
async def client_detail(request: Request, client_id: str):
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user = getattr(request.state, "current_user", None)
    async with AsyncSessionLocal() as session:
        client_result = await session.execute(
            text("SELECT * FROM clients WHERE id = :cid AND trim(tenant_id) = trim(:tid)"),
            {"cid": client_id, "tid": tenant_id},
        )
        client = client_result.mappings().fetchone()
        if not client:
            return HTMLResponse("Client not found", status_code=404)
        matters_result = await session.execute(
            text("""
                SELECT id, matter_name, matter_number, status
                FROM matters
                WHERE trim(tenant_id) = trim(:tid)
                  AND client_id = CAST(:cid AS uuid)
                ORDER BY
                    CASE status WHEN 'active' THEN 0 ELSE 1 END,
                    matter_name
            """),
            {"tid": tenant_id, "cid": client_id},
        )
        matters = [_jsonable(dict(r)) for r in matters_result.mappings().fetchall()]
    return templates.TemplateResponse(
        request,
        "billing/client_detail.html",
        _ctx(request, client=_jsonable(dict(client)), matters=matters,
             user=user, current_user=user, page="clients"),
    )


# ---------------------------------------------------------------------------
# Matter Detail — Level 3 of the billing drill-down hierarchy
# GET /billing/clients/{client_id}/matters/{matter_id}
# ---------------------------------------------------------------------------

@views.get("/billing/clients/{client_id}/matters/{matter_id}", response_class=HTMLResponse)
async def matter_detail(request: Request, client_id: str, matter_id: str):
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user = getattr(request.state, "current_user", None)

    async with AsyncSessionLocal() as session:
        # Load matter + client name in one query
        result = await session.execute(
            text("""
                SELECT
                    m.id::text          AS id,
                    m.matter_name,
                    m.matter_number,
                    m.status,
                    m.practice_area,
                    m.billing_type,
                    m.hourly_rate,
                    m.folder_path,
                    m.cause_number,
                    m.court,
                    m.judge,
                    m.jurisdiction,
                    m.open_date,
                    m.close_date,
                    m.sol_date,
                    m.notes,
                    m.client_id::text   AS client_id,
                    c.client_name
                FROM matters m
                JOIN clients c ON c.id = m.client_id
                WHERE m.id = CAST(:mid AS uuid)
                  AND m.client_id = CAST(:cid AS uuid)
                  AND trim(m.tenant_id) = trim(:tid)
            """),
            {"mid": matter_id, "cid": client_id, "tid": tenant_id},
        )
        row = result.mappings().fetchone()
        if not row:
            return HTMLResponse("Matter not found", status_code=404)

        matter = _jsonable(dict(row))

    return templates.TemplateResponse(
        request,
        "billing/matter_detail.html",
        _ctx(
            request,
            matter=matter,
            client_id=client_id,
            matter_id=matter_id,
            client_name=matter["client_name"],
            user=user,
            current_user=user,
            page="clients",
        ),
    )


@views.get("/billing/clients-search", response_class=HTMLResponse)
async def clients_search_partial(request: Request, search: str = ""):
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    async with AsyncSessionLocal() as session:
        if search:
            result = await session.execute(
                text("""
                    SELECT id, client_name FROM clients
                    WHERE trim(tenant_id) = trim(:tid)
                      AND client_name ILIKE :q
                    ORDER BY client_name LIMIT 50
                """),
                {"tid": tenant_id, "q": f"%{search}%"},
            )
        else:
            result = await session.execute(
                text("SELECT id, client_name FROM clients "
                     "WHERE trim(tenant_id) = trim(:tid) ORDER BY client_name LIMIT 50"),
                {"tid": tenant_id},
            )
        clients = [dict(r._mapping) for r in result.fetchall()]
    rows_html = "".join(
        f'<a href="/billing/clients/{c["id"]}" style="text-decoration:none; color:inherit;">'
        f'<div style="padding:10px 16px; border-bottom:1px solid #f1f5f9;">'
        f'{c["client_name"]}</div></a>'
        for c in clients
    )
    return HTMLResponse(rows_html or
        '<div style="padding:20px; text-align:center; color:#94a3b8;">No clients found.</div>')
