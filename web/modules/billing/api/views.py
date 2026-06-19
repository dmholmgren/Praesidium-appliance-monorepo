"""HTMX view routes — serves billing templates with branding context."""
from datetime import date, timedelta
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.billing.brand_helper import get_brand
from core.services.nav_context import get_nav_context

views = APIRouter(tags=["billing-views"])


def _jsonable(obj):
    import uuid, datetime
    from decimal import Decimal
    if isinstance(obj, list): return [_jsonable(i) for i in obj]
    if isinstance(obj, dict): return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, uuid.UUID): return str(obj)
    if isinstance(obj, (datetime.datetime, datetime.date)): return obj.isoformat()
    if isinstance(obj, Decimal): return float(obj)
    return obj

templates = Jinja2Templates(directory=[
    "core/templates",
    "modules/billing/templates",
])


def _ctx(request, **kwargs):
    # Inject the DB-driven module tab strip (ui_tabs via tab_service). Graceful:
    # [] on any failure → layout.html falls back to its hardcoded tabs.
    try:
        from core.services.tab_service import get_tabs_sync
        _role = getattr(getattr(request.state, "current_user", None), "role", None)
        _tid = (getattr(request.state, "tenant_id", "") or "").strip()
        _tabs = get_tabs_sync("billing", _tid, _role)
    except Exception:
        _tabs = []
    return {"request": request, "brand": get_brand(request), "page": "billing",
            "module_tabs": _tabs, "active_tab": kwargs.get("bill_tab"), **kwargs}


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
    # Sticky default: fresh navigation with no matter_id key → redirect to the
    # user's active matter (topbar pick). The billing-home React bundle reads
    # matter_id from the URL; explicit ?matter_id= is left as-is (URL is truth).
    if "matter_id" not in request.query_params:
        from core.services.active_matter import read_active_matter
        _uid = getattr(getattr(request.state, "current_user", None), "id", None)
        _tid = (getattr(request.state, "tenant_id", "") or "").strip()
        _am = await read_active_matter(_uid, _tid)
        if _am:
            from fastapi.responses import RedirectResponse
            from urllib.parse import urlencode
            _qs = dict(request.query_params)
            _qs["matter_id"] = _am["matter_id"]
            return RedirectResponse(url="/billing?" + urlencode(_qs), status_code=303)
    user = getattr(request.state, "current_user", None)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(
        request,
        "billing/billing_home_react.html",
        {**_ctx(request, user=user, current_user=user, page="billing_home", bill_tab="overview"), **nav_ctx},
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
        _ctx(request, user=user, current_user=user, page="billing_home", bill_tab="overview"),
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
        _ctx(request, clients=clients, user=user, current_user=user, page="clients", bill_tab="clients"),
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
             user=user, current_user=user, page="timesheet", bill_tab="timesheet"),
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
             user=user, current_user=user, page="timesheet", bill_tab="timesheet"),
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
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(
        request,
        "billing/billing_trust_react.html",
        {**_ctx(request, user=user, current_user=user, page="trust", bill_tab="trust"), **nav_ctx},
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
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(
        request,
        "billing/billing_reports_react.html",
        {**_ctx(request, user=user, current_user=user, page="reports", bill_tab="reports"), **nav_ctx},
    )


@views.get("/billing/clients/{client_id}", response_class=HTMLResponse)
async def client_detail(request: Request, client_id: str):
    # Guard: reject non-UUID paths like 'new' that match this route
    import re as _re
    if not _re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', client_id, _re.I):
        return HTMLResponse("Not found", status_code=404)
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
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(
        request,
        "billing/billing_client_detail_react.html",
        {**_ctx(request, client=_jsonable(dict(client)),
                user=user, current_user=user, page="clients"), **nav_ctx},
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

    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse(
        request,
        "billing/billing_matter_detail_react.html",
        {**_ctx(request, matter=matter, client_id=client_id, matter_id=matter_id,
                client_name=matter["client_name"],
                user=user, current_user=user, page="clients"), **nav_ctx},
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
# ── patch_views.py ─────────────────────────────────────────────────────────
# Append to the END of modules/billing/api/views.py
# Adds:  GET/POST /billing/matters/new
#        GET/POST /billing/clients/new
# ───────────────────────────────────────────────────────────────────────────


# ---------------------------------------------------------------------------
# New Matter — full-page form  (GET renders, POST creates & returns JSON)
# GET  /billing/matters/new
# POST /billing/matters/new
# ---------------------------------------------------------------------------

@views.get("/billing/matters/new", response_class=HTMLResponse)
async def new_matter_form(
    request: Request,
    client_name: str = "",
    client_id: str = "",
    matter_type: str = "",
    matter_name: str = "",
    court: str = "",
    case_number: str = "",
    matter_number: str = "",
):
    """Render the New Matter creation form. Query params pre-populate fields."""
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user = getattr(request.state, "current_user", None)

    # Fetch timekeepers for the attorney dropdown
    timekeepers = []
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT ts_tk_id, ts_name, ts_initials
                FROM ts_timekeepers
                WHERE trim(tenant_id) = trim(:tid)
                ORDER BY ts_name
            """),
            {"tid": tenant_id},
        )
        timekeepers = [dict(r._mapping) for r in result.fetchall()]

        # Generate next matter number hint
        from datetime import date as _date
        year = _date.today().year
        prefix = f"{year}-"
        num_result = await session.execute(
            text("""
                SELECT matter_number FROM matters
                WHERE trim(tenant_id) = trim(:tid)
                  AND matter_number LIKE :prefix
                ORDER BY matter_number DESC LIMIT 1
            """),
            {"tid": tenant_id, "prefix": f"{prefix}%"},
        )
        last_row = num_result.fetchone()
        if last_row and last_row[0]:
            try:
                seq = int(last_row[0].split("-")[1]) + 1
            except (IndexError, ValueError):
                seq = 1
        else:
            seq = 1
        next_matter_number = f"{prefix}{seq:04d}"

    prefill = {
        "client_name": client_name,
        "client_id": client_id,
        "matter_type": matter_type,
        "matter_name": matter_name,
        "court": court,
        "case_number": case_number,
        "matter_number": matter_number,
    }

    return templates.TemplateResponse(
        request,
        "billing/matter_new.html",
        _ctx(
            request,
            prefill=prefill,
            timekeepers=timekeepers,
            next_matter_number=next_matter_number,
            today=_date.today().isoformat(),
            user=user,
            current_user=user,
            page="billing",
            bill_tab="overview",
        ),
    )


@views.post("/billing/matters/new")
async def create_matter_post(request: Request):
    """
    Create a new matter via JSON POST.
    Returns {matter_id, client_id} on success or {error} on failure.
    """
    from fastapi.responses import JSONResponse
    import uuid as _uuid
    from datetime import date as _date

    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user = getattr(request.state, "current_user", None)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body."}, status_code=400)

    client_id   = (body.get("client_id") or "").strip()
    matter_name = (body.get("matter_name") or "").strip()

    if not client_id:
        return JSONResponse({"error": "Client is required."}, status_code=400)
    if not matter_name:
        return JSONResponse({"error": "Matter name is required."}, status_code=400)

    # Auto-generate matter number if not provided
    matter_number = (body.get("matter_number") or "").strip()
    if not matter_number:
        year = _date.today().year
        prefix = f"{year}-"
        async with AsyncSessionLocal() as session:
            num_result = await session.execute(
                text("""
                    SELECT matter_number FROM matters
                    WHERE trim(tenant_id) = trim(:tid)
                      AND matter_number LIKE :prefix
                    ORDER BY matter_number DESC LIMIT 1
                """),
                {"tid": tenant_id, "prefix": f"{prefix}%"},
            )
            last_row = num_result.fetchone()
            if last_row and last_row[0]:
                try:
                    seq = int(last_row[0].split("-")[1]) + 1
                except (IndexError, ValueError):
                    seq = 1
            else:
                seq = 1
            matter_number = f"{prefix}{seq:04d}"

    matter_id = str(_uuid.uuid4())

    async with AsyncSessionLocal() as session:
        # Verify client exists
        client_check = await session.execute(
            text("SELECT id FROM clients WHERE id = CAST(:cid AS uuid) AND trim(tenant_id) = trim(:tid)"),
            {"cid": client_id, "tid": tenant_id},
        )
        if not client_check.fetchone():
            return JSONResponse({"error": "Client not found."}, status_code=404)

        await session.execute(
            text("""
                INSERT INTO matters
                    (id, tenant_id, client_id, matter_name, matter_number,
                     practice_area, billing_type, hourly_rate,
                     court, cause_number, judge, jurisdiction,
                     open_date, sol_date, notes, status)
                VALUES
                    (CAST(:id AS uuid), :tid, CAST(:cid AS uuid), :mname, :mnum,
                     :pa, :bt, :rate,
                     :court, :cause, :judge, :juris,
                     :odate, :sdate, :notes, 'active')
            """),
            {
                "id":    matter_id,
                "tid":   tenant_id,
                "cid":   client_id,
                "mname": matter_name,
                "mnum":  matter_number,
                "pa":    body.get("practice_area") or None,
                "bt":    body.get("billing_type") or "hourly",
                "rate":  float(body["hourly_rate"]) if body.get("hourly_rate") else None,
                "court": body.get("court") or None,
                "cause": body.get("cause_number") or None,
                "judge": body.get("judge") or None,
                "juris": body.get("jurisdiction") or None,
                "odate": _date.fromisoformat(body["open_date"]) if body.get("open_date") else _date.today(),
                "sdate": _date.fromisoformat(body["sol_date"]) if body.get("sol_date") else None,
                "notes": body.get("notes") or None,
            },
        )
        await session.commit()

        # Look up client_name for folder path
        cn_row = await session.execute(
            text("SELECT client_name FROM clients WHERE id = CAST(:cid AS uuid)"),
            {"cid": client_id},
        )
        cn = cn_row.fetchone()
        client_name_for_folder = cn[0] if cn else "Unknown"

    # ── Seed matter folder structure via folder_seeder ───────────────────
    import logging as _log
    try:
        from modules.dms.jobs.folder_seeder import seed_matter_folders
        seed_result = seed_matter_folders(
            tenant_id=tenant_id,
            matter_id=matter_id,
            matter_type=body.get("practice_area") or body.get("matter_type"),
            client_name=client_name_for_folder,
            matter_name=matter_name,
            matter_number=matter_number,
        )
        folder_rel = f"{client_name_for_folder}/{matter_name}"
        if seed_result.get("path"):
            # Update folder_path on the matter
            async with AsyncSessionLocal() as s2:
                await s2.execute(
                    text("UPDATE matters SET folder_path = :fp WHERE id = CAST(:mid AS uuid)"),
                    {"fp": folder_rel, "mid": matter_id},
                )
                # Register praesidium root in matter_folders
                await s2.execute(
                    text("""
                        INSERT INTO matter_folders
                            (id, tenant_id, matter_id, folder_path, disk_root, file_count, added_at)
                        VALUES
                            (gen_random_uuid(), :tid, CAST(:mid AS uuid), :fp, :dr, 0, NOW())
                        ON CONFLICT (matter_id, folder_path) DO NOTHING
                    """),
                    {"tid": tenant_id, "mid": matter_id,
                     "fp": folder_rel, "dr": seed_result["path"]},
                )
                await s2.commit()
        _log.getLogger(__name__).info(
            "Seeded matter folder via folder_seeder: %s (created=%d, existing=%d, type=%s)",
            seed_result.get("path"), seed_result.get("created", 0),
            seed_result.get("existing", 0), seed_result.get("matter_type_resolved"),
        )
    except Exception as exc:
        _log.getLogger(__name__).warning("Failed to seed matter folder: %s", exc)

    return JSONResponse({
        "matter_id": matter_id,
        "client_id": client_id,
        "matter_number": matter_number,
        "folder_path": folder_rel,
        "status": "created",
    })


# ---------------------------------------------------------------------------
# New Client — full-page form (GET renders, POST creates & returns JSON)
# GET  /billing/clients/new
# POST /billing/clients/new
# ---------------------------------------------------------------------------

@views.get("/billing/clients/new", response_class=HTMLResponse)
async def new_client_form(
    request: Request,
    client_name: str = "",
    client_type: str = "",
):
    """Render the New Client creation form."""
    user = getattr(request.state, "current_user", None)
    prefill = {
        "client_name": client_name,
        "client_type": client_type,
    }
    return templates.TemplateResponse(
        request,
        "billing/client_new.html",
        _ctx(request, prefill=prefill, user=user, current_user=user,
             page="clients", bill_tab="clients"),
    )


@views.post("/billing/clients/new")
async def create_client_post(request: Request):
    """Create a new client via JSON POST. Returns {client_id}."""
    from fastapi.responses import JSONResponse
    import uuid as _uuid

    tenant_id = getattr(request.state, "tenant_id", "").strip()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body."}, status_code=400)

    client_name = (body.get("client_name") or "").strip()
    if not client_name:
        return JSONResponse({"error": "Client name is required."}, status_code=400)

    client_id = str(_uuid.uuid4())

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO clients
                    (id, tenant_id, client_name, client_type,
                     client_number, primary_contact,
                     email, phone, address1, address2,
                     city, state, zip_code, notes)
                VALUES
                    (CAST(:id AS uuid), :tid, :name, :ctype,
                     :cnum, :contact,
                     :email, :phone, :addr1, :addr2,
                     :city, :state, :zip, :notes)
            """),
            {
                "id":      client_id,
                "tid":     tenant_id,
                "name":    client_name,
                "ctype":   body.get("client_type") or "individual",
                "cnum":    body.get("client_number") or None,
                "contact": body.get("primary_contact") or None,
                "email":   body.get("email") or None,
                "phone":   body.get("phone") or None,
                "addr1":   body.get("address1") or None,
                "addr2":   body.get("address2") or None,
                "city":    body.get("city") or None,
                "state":   body.get("state") or None,
                "zip":     body.get("zip_code") or None,
                "notes":   body.get("notes") or None,
            },
        )
        await session.commit()

    return JSONResponse({
        "client_id": client_id,
        "status": "created",
    })