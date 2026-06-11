"""
Bill Run Selector API v3 — unified time_entries as single source of truth.

GET /api/v1/billing/bill-run-selector?q=&start=&end=
  Queries time_entries (which now includes migrated ts_slips data).
  Groups by matter_id, returns matters with unbilled WIP.

POST /api/v1/billing/bill-runs-selective
  Creates a bill run from selected matter_ids.
"""
from __future__ import annotations
import logging
from datetime import date
from decimal import Decimal
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing", tags=["billing-bill-run-selector"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()


@router.get("/bill-run-selector")
async def bill_run_selector(request: Request, q: str = "", start: str = "", end: str = ""):
    """Return matters with unbilled WIP from time_entries."""
    tid = _tid(request)

    has_dates = bool(start and end)
    try:
        ps = date.fromisoformat(start) if start else None
        pe = date.fromisoformat(end) if end else None
    except ValueError:
        ps = pe = None

    date_clause = "AND te.date BETWEEN :ps AND :pe" if has_dates else ""
    search_clause = ""
    if q and q.strip():
        search_clause = "AND (m.matter_name ILIKE :q OR m.matter_number ILIKE :q OR c.client_name ILIKE :q)"

    sql = f"""
        SELECT te.matter_id,
               m.matter_name,
               m.matter_number,
               c.client_name,
               c.id AS client_id,
               COUNT(te.id) AS slip_count,
               COALESCE(SUM(te.hours), 0) AS total_hours,
               COALESCE(SUM(COALESCE(te.amount, te.hours * te.rate)), 0) AS total_wip,
               MIN(te.date) AS first_slip,
               MAX(te.date) AS last_slip
        FROM time_entries te
        JOIN matters m ON m.id = te.matter_id
            AND TRIM(m.tenant_id) = TRIM(te.tenant_id)
        LEFT JOIN clients c ON c.id = m.client_id
            AND TRIM(c.tenant_id) = TRIM(m.tenant_id)
        WHERE TRIM(te.tenant_id) = :tid
          AND te.status = 'draft'
          AND te.billable = true
          AND te.invoice_id IS NULL
          {date_clause}
          {search_clause}
        GROUP BY te.matter_id, m.matter_name, m.matter_number,
                 c.client_name, c.id
        ORDER BY total_wip DESC
        LIMIT 200
    """

    params = {"tid": tid}
    if has_dates:
        params["ps"] = ps
        params["pe"] = pe
    if q and q.strip():
        params["q"] = f"%{q.strip()}%"

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text(sql), params)).fetchall()

    matters = []
    for r in rows:
        matters.append({
            "matter_id": str(r.matter_id),
            "matter_name": r.matter_name or "Unknown",
            "matter_number": r.matter_number or "",
            "client_name": r.client_name or "Unknown",
            "client_id": str(r.client_id) if r.client_id else None,
            "slip_count": r.slip_count,
            "total_hours": float(r.total_hours),
            "total_wip": float(r.total_wip),
            "first_slip": r.first_slip.isoformat() if r.first_slip else None,
            "last_slip": r.last_slip.isoformat() if r.last_slip else None,
        })

    return JSONResponse({
        "matters": matters,
        "total_results": len(matters),
        "total_wip": sum(m["total_wip"] for m in matters),
        "total_slips": sum(m["slip_count"] for m in matters),
        "period_start": ps.isoformat() if ps else None,
        "period_end": pe.isoformat() if pe else None,
        "query": q,
    })


@router.post("/bill-runs-selective")
async def create_selective_bill_run(request: Request):
    """Create a bill run from selected matter_ids (time_entries only)."""
    tid = _tid(request)
    user = getattr(request.state, "current_user", None)
    uid = user.id if user and hasattr(user, "id") else 0

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid body"}, status_code=400)

    period_start = date.fromisoformat(body["period_start"])
    period_end = date.fromisoformat(body["period_end"])
    selected_ids = body.get("matter_ids", [])
    # Backward compat: also accept ts_client_ids (ignored now, but don't break)
    if not selected_ids:
        selected_ids = body.get("ts_client_ids", [])
    run_name = body.get("run_name") or f"Bill Run \u2014 {period_start.strftime('%B %Y')}"
    notes = body.get("notes") or None

    if not selected_ids:
        return JSONResponse({"error": "No matters selected"}, status_code=400)

    async with AsyncSessionLocal() as db:
        row = (await db.execute(sa_text("""
            INSERT INTO bill_runs
                (tenant_id, run_name, billing_period_start, billing_period_end,
                 status, created_by_id, notes)
            VALUES (:tid, :name, :start, :end, 'draft', :by, :notes)
            RETURNING id
        """), {
            "tid": tid, "name": run_name,
            "start": period_start, "end": period_end,
            "by": uid, "notes": notes,
        })).fetchone()
        bill_run_id = row[0]

        total_matters = 0
        total_fees = Decimal("0")

        for mid in selected_ids:
            matter_row = (await db.execute(sa_text("""
                SELECT te.matter_id,
                       m.matter_name, m.matter_number,
                       m.client_id, c.client_name,
                       COUNT(te.id) AS slip_count,
                       COALESCE(SUM(te.hours), 0) AS total_hours,
                       COALESCE(SUM(COALESCE(te.amount, te.hours * te.rate)), 0) AS fee_total
                FROM time_entries te
                JOIN matters m ON m.id = te.matter_id
                    AND TRIM(m.tenant_id) = TRIM(te.tenant_id)
                LEFT JOIN clients c ON c.id = m.client_id
                    AND TRIM(c.tenant_id) = TRIM(m.tenant_id)
                WHERE TRIM(te.tenant_id) = :tid
                  AND te.matter_id = CAST(:mid AS uuid)
                  AND te.status = 'draft'
                  AND te.billable = true
                  AND te.invoice_id IS NULL
                  AND te.date BETWEEN :ps AND :pe
                GROUP BY te.matter_id, m.matter_name, m.matter_number,
                         m.client_id, c.client_name
            """), {"tid": tid, "mid": str(mid), "ps": period_start, "pe": period_end})).fetchone()

            if not matter_row or not matter_row.fee_total:
                continue

            fee = Decimal(str(matter_row.fee_total))

            # Prior balance from open invoices
            prior = (await db.execute(sa_text("""
                SELECT COALESCE(SUM(balance_due), 0)
                FROM invoices
                WHERE TRIM(tenant_id) = :tid
                  AND matter_id = CAST(:mid AS uuid)
                  AND status NOT IN ('paid', 'void')
            """), {"tid": tid, "mid": str(mid)})).scalar() or 0

            await db.execute(sa_text("""
                INSERT INTO bill_run_matters
                    (tenant_id, bill_run_id, matter_id, client_id,
                     client_name, matter_name, matter_number,
                     status, fee_total, prior_balance, net_total)
                VALUES (:tid, :brid, CAST(:mid AS uuid), CAST(:cid AS uuid),
                        :cname, :mname, :mnum,
                        'pending', :fees, :prior, :net)
            """), {
                "tid": tid, "brid": bill_run_id,
                "mid": str(matter_row.matter_id),
                "cid": str(matter_row.client_id) if matter_row.client_id else None,
                "cname": matter_row.client_name,
                "mname": matter_row.matter_name,
                "mnum": matter_row.matter_number,
                "fees": fee, "prior": prior,
                "net": fee + Decimal(str(prior)),
            })
            total_matters += 1
            total_fees += fee

        await db.execute(sa_text("""
            UPDATE bill_runs
            SET total_matters = :tm, total_fees = :tf, status = 'reviewing'
            WHERE id = :brid AND tenant_id = :tid
        """), {"tm": total_matters, "tf": total_fees, "brid": bill_run_id, "tid": tid})

        await db.commit()

    return JSONResponse({
        "id": bill_run_id,
        "run_name": run_name,
        "status": "reviewing",
        "total_matters": total_matters,
        "total_fees": float(total_fees),
    }, status_code=201)
