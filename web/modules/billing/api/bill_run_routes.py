"""
modules/billing/api/bill_run_routes.py
Bill Run Workflow — create, review, adjust, finalize bill runs.

Permission gates (Phase 2):
  - bill_run.view      → list, detail, prebill review, reports
  - bill_run.create    → create new bill run
  - bill_run.edit      → adjust slips, approve/skip matters
  - bill_run.finalize  → finalize bill run → create invoices
  - bill_run.delete    → (reserved, not yet implemented)

Wire into billing __init__.py:
    from modules.billing.api.bill_run_routes import router as bill_run_router
    app.include_router(bill_run_router)
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from core.auth.dependencies import require_permission
from core.auth.permissions import PermissionResult
from modules.dashboard.services.auth_helper import get_current_user
from core.services.nav_context import get_nav_context
from modules.billing.brand_helper import get_brand

log = logging.getLogger(__name__)
router = APIRouter(tags=["bill-run"])


def _templates(request: Request):
    from modules.billing.api.views import templates
    return templates


def _tid(request: Request) -> str:
    return getattr(request.state, "tenant_id", "").strip()


def _uid(user) -> int:
    return user.id if not isinstance(user, dict) else user.get("id", 0)


# ── Bill Run List ─────────────────────────────────────────────────────────────

@router.get("/billing/run-bills", response_class=HTMLResponse)
async def bill_run_list(
    request: Request,
    user=Depends(get_current_user),
    perm: PermissionResult = Depends(require_permission("bill_run", "view")),
):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        runs = (
            await db.execute(
                text("""
                    SELECT br.*, u.full_name AS created_by_name,
                           fu.full_name AS finalized_by_name
                    FROM bill_runs br
                    JOIN users u ON u.id = br.created_by_id
                    LEFT JOIN users fu ON fu.id = br.finalized_by_id
                    WHERE br.tenant_id = :tid
                    ORDER BY br.created_at DESC
                """),
                {"tid": tid},
            )
        ).fetchall()

    nav = await get_nav_context(request)
    t = _templates(request)
    return t.TemplateResponse(
        request,
        "billing/billing_bill_runs_react.html",
        {"user": user, "current_user": user, "brand": get_brand(request), "page": "billing", "bill_tab": "run_bills", **nav},
    )


# ── Create Bill Run ───────────────────────────────────────────────────────────

@router.post("/billing/run-bills", response_class=HTMLResponse)
async def create_bill_run(
    request: Request,
    period_start: str = Form(...),
    period_end: str = Form(...),
    run_name: str = Form(""),
    notes: str = Form(""),
    user=Depends(get_current_user),
    perm: PermissionResult = Depends(require_permission("bill_run", "create")),
):
    from modules.billing.services.bill_run_service import create_bill_run as create_run

    tid = _tid(request)
    result = await create_run(
        tenant_id=tid,
        period_start=date.fromisoformat(period_start),
        period_end=date.fromisoformat(period_end),
        created_by_id=_uid(user),
        run_name=run_name.strip() or None,
        notes=notes.strip() or None,
    )
    return RedirectResponse(f"/billing/run-bills/{result['id']}", status_code=303)


# ── Bill Run Detail (Review Dashboard) ────────────────────────────────────────

@router.get("/billing/run-bills/{bill_run_id}", response_class=HTMLResponse)
async def bill_run_detail(
    request: Request,
    bill_run_id: int,
    user=Depends(get_current_user),
    perm: PermissionResult = Depends(require_permission("bill_run", "view")),
):
    from modules.billing.services.bill_run_service import get_bill_run

    tid = _tid(request)
    data = await get_bill_run(tid, bill_run_id)
    if not data:
        return RedirectResponse("/billing/run-bills", status_code=303)

    nav = await get_nav_context(request)
    t = _templates(request)
    return t.TemplateResponse(
        request,
        "billing/billing_bill_run_detail_react.html",
        {
            "bill_run": data["bill_run"],
            "page": "billing",
            "page": "billing",
            "user": user,
            "current_user": user,
            "brand": get_brand(request),
            **nav,
        },
    )


# ── Prebill Slip Review (per-matter) ─────────────────────────────────────────

@router.get(
    "/billing/run-bills/{bill_run_id}/matter/{brm_id}",
    response_class=HTMLResponse,
)
async def prebill_review(
    request: Request,
    bill_run_id: int,
    brm_id: int,
    user=Depends(get_current_user),
    perm: PermissionResult = Depends(require_permission("bill_run", "view")),
):
    from modules.billing.services.bill_run_service import get_prebill_slips

    tid = _tid(request)
    data = await get_prebill_slips(tid, brm_id)

    nav = await get_nav_context(request)
    t = _templates(request)
    return t.TemplateResponse(
        request,
        "billing/billing_prebill_review_react.html",
        {
            "bill_run_id": bill_run_id,
            "page": "billing",
            "page": "billing",
            "brm_id": brm_id,
            "user": user,
            "current_user": user,
            "brand": get_brand(request),
            **nav,
        },
    )


# ── Apply Adjustment (write-off, edit) ────────────────────────────────────────

@router.post("/api/billing/bill-run/adjust")
async def adjust_slip(
    request: Request,
    user=Depends(get_current_user),
    perm: PermissionResult = Depends(require_permission("bill_run", "edit")),
):
    from modules.billing.services.bill_run_service import apply_adjustment

    tid = _tid(request)
    body = await request.json()

    adj_id = await apply_adjustment(
        tenant_id=tid,
        bill_run_matter_id=int(body["bill_run_matter_id"]),
        adjustment_type=body["adjustment_type"],
        adjusted_by_id=_uid(user),
        time_entry_id=body.get("time_entry_id"),
        ts_slip_id=body.get("ts_slip_id"),
        original_value=body.get("original_value"),
        adjusted_value=body.get("adjusted_value"),
        original_hours=body.get("original_hours"),
        adjusted_hours=body.get("adjusted_hours"),
        original_narrative=body.get("original_narrative"),
        adjusted_narrative=body.get("adjusted_narrative"),
        reason=body.get("reason"),
    )
    return JSONResponse({"adjustment_id": adj_id})


# ── Approve / Skip Matter ────────────────────────────────────────────────────

@router.post("/billing/run-bills/{bill_run_id}/matter/{brm_id}/approve")
async def approve_matter_route(
    request: Request,
    bill_run_id: int,
    brm_id: int,
    user=Depends(get_current_user),
    perm: PermissionResult = Depends(require_permission("bill_run", "edit")),
):
    from modules.billing.services.bill_run_service import approve_matter

    tid = _tid(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    await approve_matter(tid, brm_id, _uid(user), body.get("notes"))
    return JSONResponse({"status": "approved"})


@router.post("/billing/run-bills/{bill_run_id}/matter/{brm_id}/skip")
async def skip_matter_route(
    request: Request,
    bill_run_id: int,
    brm_id: int,
    user=Depends(get_current_user),
    perm: PermissionResult = Depends(require_permission("bill_run", "edit")),
):
    from modules.billing.services.bill_run_service import skip_matter

    tid = _tid(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    await skip_matter(tid, brm_id, body.get("reason", ""))
    return JSONResponse({"status": "skipped"})


# ── Finalize Bill Run ─────────────────────────────────────────────────────────

@router.post("/billing/run-bills/{bill_run_id}/finalize")
async def finalize_bill_run_route(
    request: Request,
    bill_run_id: int,
    user=Depends(get_current_user),
    perm: PermissionResult = Depends(require_permission("bill_run", "finalize")),
):
    from modules.billing.services.bill_run_service import finalize_bill_run

    tid = _tid(request)
    result = await finalize_bill_run(tid, bill_run_id, _uid(user))
    return JSONResponse(result)


# ── Report Runner ─────────────────────────────────────────────────────────────

@router.get("/billing/reports/run", response_class=HTMLResponse)
async def report_runner(
    request: Request,
    user=Depends(get_current_user),
    perm: PermissionResult = Depends(require_permission("billing", "view")),
):
    nav = await get_nav_context(request)
    t = _templates(request)
    return t.TemplateResponse(
        request,
        "billing/report_runner.html",
        {"user": user, "current_user": user, "today": date.today(), "brand": get_brand(request), "bill_tab": "run_bills", **nav},
    )


@router.get("/api/billing/reports/execute")
async def execute_report(
    request: Request,
    report_id: str = "wip_summary",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    client_id: Optional[str] = None,
    user=Depends(get_current_user),
    perm: PermissionResult = Depends(require_permission("billing", "view")),
):
    """Execute a billing report and return JSON results."""
    tid = _tid(request)

    reports = {
        "wip_summary": {
            "name": "WIP Summary",
            "sql": """
                SELECT m.matter_number, m.matter_name, c.client_name,
                       COUNT(te.id) AS entries,
                       SUM(te.hours) AS hours,
                       SUM(COALESCE(te.amount, te.hours * te.rate)) AS wip_value
                FROM time_entries te
                JOIN matters m ON m.id = te.matter_id
                LEFT JOIN clients c ON c.id = m.client_id
                WHERE TRIM(te.tenant_id) = :tid
                  AND te.status = 'draft' AND te.billable = true
                  AND te.invoice_id IS NULL
                  AND (:start IS NULL OR te.date >= CAST(:start AS date))
                  AND (:end IS NULL OR te.date <= CAST(:end AS date))
                GROUP BY m.matter_number, m.matter_name, c.client_name
                ORDER BY wip_value DESC
            """,
        },
        "ts_wip_summary": {
            "name": "Timeslips WIP Summary",
            "sql": """
                SELECT tc.client_code AS matter_number,
                       tc.ts_name AS matter_name,
                       tc.client_code AS client_name,
                       COUNT(ts.id) AS entries,
                       SUM(ts.hours) AS hours,
                       SUM(ts.wip_value) AS wip_value
                FROM ts_slips ts
                JOIN ts_clients tc ON tc.ts_client_id = ts.source_client_id
                    AND TRIM(tc.tenant_id) = TRIM(ts.tenant_id)
                WHERE TRIM(ts.tenant_id) = :tid
                  AND ts.billed = false
                  AND ts.wip_value > 0
                  AND (:start IS NULL OR ts.slip_date >= CAST(:start AS date))
                  AND (:end IS NULL OR ts.slip_date <= CAST(:end AS date))
                GROUP BY tc.client_code, tc.ts_name
                ORDER BY wip_value DESC
            """,
        },
        "ar_aging": {
            "name": "Accounts Receivable Aging",
            "sql": """
                SELECT i.invoice_number, i.invoice_date, i.due_date,
                       m.matter_number, m.matter_name, c.client_name,
                       i.total, i.amount_paid, i.balance_due,
                       CURRENT_DATE - i.due_date AS days_past_due,
                       CASE
                         WHEN CURRENT_DATE - i.due_date <= 0 THEN 'Current'
                         WHEN CURRENT_DATE - i.due_date <= 30 THEN '1-30'
                         WHEN CURRENT_DATE - i.due_date <= 60 THEN '31-60'
                         WHEN CURRENT_DATE - i.due_date <= 90 THEN '61-90'
                         ELSE '90+'
                       END AS aging_bucket
                FROM invoices i
                LEFT JOIN matters m ON m.id = i.matter_id
                LEFT JOIN clients c ON c.id = i.client_id
                WHERE TRIM(i.tenant_id) = :tid
                  AND i.status NOT IN ('paid', 'void', 'draft')
                  AND i.balance_due > 0
                ORDER BY i.due_date
            """,
        },
        "timekeeper_productivity": {
            "name": "Timekeeper Productivity",
            "sql": """
                SELECT te.timekeeper_name,
                       COUNT(te.id) AS entries,
                       SUM(te.hours) AS total_hours,
                       SUM(CASE WHEN te.billable = true THEN te.hours ELSE 0 END) AS billable_hours,
                       SUM(CASE WHEN te.billable = false THEN te.hours ELSE 0 END) AS nonbillable_hours,
                       ROUND(
                         100.0 * SUM(CASE WHEN te.billable = true THEN te.hours ELSE 0 END) /
                         NULLIF(SUM(te.hours), 0), 1
                       ) AS utilization_pct,
                       SUM(COALESCE(te.amount, te.hours * te.rate)) AS total_value
                FROM time_entries te
                WHERE TRIM(te.tenant_id) = :tid
                  AND (:start IS NULL OR te.date >= CAST(:start AS date))
                  AND (:end IS NULL OR te.date <= CAST(:end AS date))
                GROUP BY te.timekeeper_name
                ORDER BY total_hours DESC
            """,
        },
        "client_revenue": {
            "name": "Client Revenue Summary",
            "sql": """
                SELECT c.client_name, c.client_number,
                       COUNT(DISTINCT i.id) AS invoice_count,
                       SUM(i.total) AS total_billed,
                       SUM(i.amount_paid) AS total_paid,
                       SUM(i.balance_due) AS total_outstanding
                FROM invoices i
                LEFT JOIN clients c ON c.id = i.client_id
                WHERE TRIM(i.tenant_id) = :tid
                  AND i.status != 'void'
                  AND (:start IS NULL OR i.invoice_date >= CAST(:start AS date))
                  AND (:end IS NULL OR i.invoice_date <= CAST(:end AS date))
                GROUP BY c.client_name, c.client_number
                ORDER BY total_billed DESC
            """,
        },
        "writeoff_report": {
            "name": "Write-Off Report",
            "sql": """
                SELECT pa.created_at, pa.adjustment_type, pa.reason,
                       pa.original_value, pa.adjusted_value,
                       (pa.original_value - COALESCE(pa.adjusted_value, 0)) AS writeoff_amount,
                       brm.client_name, brm.matter_name, brm.matter_number,
                       u.full_name AS adjusted_by
                FROM prebill_adjustments pa
                JOIN bill_run_matters brm ON brm.id = pa.bill_run_matter_id
                JOIN users u ON u.id = pa.adjusted_by_id
                WHERE TRIM(pa.tenant_id) = :tid
                  AND pa.adjustment_type = 'write_off'
                ORDER BY pa.created_at DESC
            """,
        },
        "trust_balance": {
            "name": "Trust Account Balances",
            "sql": """
                SELECT tl.client_id, c.client_name,
                       tl.matter_id, m.matter_name, m.matter_number,
                       SUM(tl.amount) AS balance
                FROM trust_ledger tl
                LEFT JOIN clients c ON c.id = tl.client_id
                LEFT JOIN matters m ON m.id = tl.matter_id
                WHERE TRIM(tl.tenant_id) = :tid
                GROUP BY tl.client_id, c.client_name, tl.matter_id,
                         m.matter_name, m.matter_number
                HAVING SUM(tl.amount) != 0
                ORDER BY c.client_name
            """,
        },
    }

    report = reports.get(report_id)
    if not report:
        return JSONResponse({"error": "Unknown report"}, status_code=400)

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                text(report["sql"]),
                {
                    "tid": tid,
                    "start": start_date or None,
                    "end": end_date or None,
                },
            )
        ).fetchall()

    columns = [r._mapping.keys() for r in rows[:1]]
    cols = list(columns[0]) if columns else []
    data = [dict(r._mapping) for r in rows]

    # Convert Decimal / date to JSON-safe types
    for row in data:
        for k, v in row.items():
            if hasattr(v, "as_integer_ratio"):
                row[k] = float(v)
            elif hasattr(v, "isoformat"):
                row[k] = v.isoformat()

    return JSONResponse({
        "report_name": report["name"],
        "report_id": report_id,
        "columns": cols,
        "data": data,
        "row_count": len(data),
    })
