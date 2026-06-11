"""Billing Trust & Reports JSON APIs.

GET /api/v1/billing/trust         -> trust ledger data
GET /api/v1/billing/reports/meta  -> clients + attorneys for filter dropdowns
GET /api/v1/billing/reports       -> report data by category
"""
from __future__ import annotations
import logging
from decimal import Decimal
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing", tags=["billing-trust-reports"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()

def _ser_row(row_dict):
    """Serialize a row dict — Decimal->float, date->str."""
    import datetime
    out = {}
    for k, v in row_dict.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif hasattr(v, 'isoformat'):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


# ── Trust ──────────────────────────────────────────────────────────────────
@router.get("/trust")
async def trust_api(request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        rows = await db.execute(sa_text("""
            SELECT tl.id::text, tl.client_id::text,
                   tl.balance, tl.last_reconciled_at,
                   COALESCE(c.client_name, '') AS client_name
            FROM trust_ledger tl
            LEFT JOIN clients c ON tl.client_id = c.id
                AND trim(c.tenant_id) = trim(:tid)
            WHERE trim(tl.tenant_id) = trim(:tid)
            ORDER BY c.client_name
        """), {"tid": tid})

        ledgers = [_ser_row(dict(r)) for r in rows.mappings()]
        total = sum(float(l.get("balance") or 0) for l in ledgers)
        active = sum(1 for l in ledgers if float(l.get("balance") or 0) > 0)

    return JSONResponse({
        "ledgers": ledgers,
        "total_balance": total,
        "active_count": active,
        "account_count": len(ledgers),
    })


# ── Reports Meta (filter dropdowns) ───────────────────────────────────────
@router.get("/reports/meta")
async def reports_meta_api(request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        cr = await db.execute(sa_text("""
            SELECT id::text, client_name FROM clients
            WHERE trim(tenant_id) = trim(:tid) ORDER BY client_name
        """), {"tid": tid})
        clients = [dict(r) for r in cr.mappings()]

        ar = await db.execute(sa_text("""
            SELECT ts_tk_id AS id, COALESCE(ts_name, ts_initials, ts_tk_id) AS name
            FROM ts_timekeepers WHERE trim(tenant_id) = trim(:tid) ORDER BY name
        """), {"tid": tid})
        attorneys = [dict(r) for r in ar.mappings()]

    return JSONResponse({"clients": clients, "attorneys": attorneys})


# ── Reports Data ──────────────────────────────────────────────────────────
@router.get("/reports")
async def reports_data_api(
    request: Request,
    category: str = "wip",
    date_from: str = "",
    date_to: str = "",
    client_id: str = "",
    attorney_id: str = "",
):
    tid = _tid(request)
    result = {"category": category, "columns": [], "rows": [], "summary": {}}

    async with AsyncSessionLocal() as db:
        if category == "wip":
            q = """
                SELECT COALESCE(c.client_name, 'Unknown') AS client,
                       COALESCE(tk.ts_name, s.source_tk_id) AS attorney,
                       SUM(s.hours) AS hours,
                       SUM(s.wip_value) AS wip_value,
                       COUNT(*) AS entries
                FROM ts_slips s
                LEFT JOIN ts_timekeepers tk ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                LEFT JOIN ts_clients tc ON tc.ts_client_id::text = s.source_client_id
                    AND tc.tenant_id = s.tenant_id
                LEFT JOIN matters m ON m.matter_number = tc.ts_raw->>'nickname2'
                    AND trim(m.tenant_id) = trim(:tid)
                LEFT JOIN clients c ON m.client_id = c.id
                WHERE trim(s.tenant_id) = trim(:tid) AND s.billed = false
            """
            params = {"tid": tid}
            if date_from:
                q += " AND s.slip_date >= :df"
                params["df"] = date_from
            if date_to:
                q += " AND s.slip_date <= :dt"
                params["dt"] = date_to
            if attorney_id:
                q += " AND s.source_tk_id = :atty"
                params["atty"] = attorney_id
            q += " GROUP BY client, attorney ORDER BY wip_value DESC"

            rows = await db.execute(sa_text(q), params)
            data = [_ser_row(dict(r)) for r in rows.mappings()]
            result["columns"] = ["client", "attorney", "hours", "wip_value", "entries"]
            result["rows"] = data
            result["summary"] = {
                "total_hours": round(sum(float(r.get("hours") or 0) for r in data), 1),
                "total_wip": round(sum(float(r.get("wip_value") or 0) for r in data), 2),
            }

        elif category == "ar_aging":
            rows = await db.execute(sa_text("""
                SELECT ti.invoice_num,
                       ti.net_due AS balance,
                       ti.created_at::date AS invoice_date,
                       CURRENT_DATE - ti.created_at::date AS age_days,
                       CASE
                         WHEN CURRENT_DATE - ti.created_at::date <= 30 THEN 'Current'
                         WHEN CURRENT_DATE - ti.created_at::date <= 60 THEN '31-60'
                         WHEN CURRENT_DATE - ti.created_at::date <= 90 THEN '61-90'
                         ELSE '90+'
                       END AS bucket
                FROM ts_invoices ti
                WHERE trim(ti.tenant_id) = trim(:tid)
                  AND ti.paid_in_full = false AND ti.net_due > 0
                ORDER BY age_days DESC
            """), {"tid": tid})
            data = [_ser_row(dict(r)) for r in rows.mappings()]
            result["columns"] = ["invoice_num", "balance", "invoice_date", "age_days", "bucket"]
            result["rows"] = data
            result["summary"] = {
                "total_ar": round(sum(float(r.get("balance") or 0) for r in data), 2),
                "invoice_count": len(data),
            }

        elif category == "collections":
            q = """
                SELECT tp.payment_date::date AS date,
                       tp.amount,
                       tp.invoice_num,
                       tp.payment_method
                FROM ts_payments tp
                WHERE trim(tp.tenant_id) = trim(:tid)
            """
            params = {"tid": tid}
            if date_from:
                q += " AND tp.payment_date >= :df"
                params["df"] = date_from
            if date_to:
                q += " AND tp.payment_date <= :dt"
                params["dt"] = date_to
            q += " ORDER BY tp.payment_date DESC LIMIT 200"
            rows = await db.execute(sa_text(q), params)
            data = [_ser_row(dict(r)) for r in rows.mappings()]
            result["columns"] = ["date", "amount", "invoice_num", "payment_method"]
            result["rows"] = data
            result["summary"] = {
                "total_collected": round(sum(abs(float(r.get("amount") or 0)) for r in data), 2),
                "payment_count": len(data),
            }

        elif category == "slip":
            q = """
                SELECT s.slip_date::date AS date,
                       COALESCE(tk.ts_name, s.source_tk_id) AS attorney,
                       s.hours, s.rate,
                       CASE WHEN s.billed THEN s.billed_value ELSE s.wip_value END AS value,
                       CASE WHEN s.billed THEN 'Billed' ELSE 'WIP' END AS status,
                       LEFT(s.narrative, 120) AS narrative
                FROM ts_slips s
                LEFT JOIN ts_timekeepers tk ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                WHERE trim(s.tenant_id) = trim(:tid)
            """
            params = {"tid": tid}
            if date_from:
                q += " AND s.slip_date >= :df"
                params["df"] = date_from
            if date_to:
                q += " AND s.slip_date <= :dt"
                params["dt"] = date_to
            if attorney_id:
                q += " AND s.source_tk_id = :atty"
                params["atty"] = attorney_id
            q += " ORDER BY s.slip_date DESC LIMIT 500"
            rows = await db.execute(sa_text(q), params)
            data = [_ser_row(dict(r)) for r in rows.mappings()]
            result["columns"] = ["date", "attorney", "hours", "rate", "value", "status", "narrative"]
            result["rows"] = data

        elif category == "invoice":
            q = """
                SELECT ti.invoice_num,
                       ti.created_at::date AS date,
                       ti.net_due AS amount,
                       CASE WHEN ti.paid_in_full THEN 'Paid' ELSE 'Open' END AS status
                FROM ts_invoices ti
                WHERE trim(ti.tenant_id) = trim(:tid)
            """
            params = {"tid": tid}
            if date_from:
                q += " AND ti.created_at >= :df"
                params["df"] = date_from
            if date_to:
                q += " AND ti.created_at <= :dt"
                params["dt"] = date_to
            q += " ORDER BY ti.created_at DESC LIMIT 200"
            rows = await db.execute(sa_text(q), params)
            data = [_ser_row(dict(r)) for r in rows.mappings()]
            result["columns"] = ["invoice_num", "date", "amount", "status"]
            result["rows"] = data

        else:
            result["rows"] = []
            result["columns"] = []

    return JSONResponse(result)