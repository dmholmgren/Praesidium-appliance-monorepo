"""
Timekeeper & Rate Management API + Time Entry Detail/Edit API.

GET  /api/v1/billing/timekeepers-rates     -> all timekeepers with rates + matter assignments
POST /api/v1/billing/timekeepers-rates     -> set/update a rate
GET  /api/v1/billing/timekeepers-rates/matters -> matter assignments for a timekeeper
POST /api/v1/billing/timekeepers-rates/assign  -> assign timekeeper to matter
DELETE /api/v1/billing/timekeepers-rates/assign -> remove assignment

GET  /api/v1/billing/time-entry/{entry_id}  -> full time entry detail (ts_slips or time_entries)
PUT  /api/v1/billing/time-entry/{entry_id}  -> update time entry

GET  /api/v1/billing/ar-detail              -> detailed AR report data
GET  /api/v1/billing/invoice-detail/{inv_id} -> full invoice detail
"""
from __future__ import annotations
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing", tags=["billing-timekeepers-rates"])

def _tid(r):
    return (getattr(r.state, "tenant_id", "") or "").strip()

def _ser(val):
    if isinstance(val, Decimal): return float(val)
    if isinstance(val, (date, datetime)): return val.isoformat()
    return val

def _ser_row(row_dict):
    return {k: _ser(v) for k, v in row_dict.items()}


# -- Timekeepers + Rates ---

@router.get("/timekeepers-rates")
async def timekeepers_rates(request: Request):
    """All timekeepers with their current default rate and matter-specific rates."""
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        tk_rows = await db.execute(sa_text("""
            SELECT ts_tk_id, ts_name, ts_initials, ts_raw
            FROM ts_timekeepers
            WHERE trim(tenant_id) = trim(:tid)
            ORDER BY ts_name
        """), {"tid": tid})
        timekeepers = []
        for r in tk_rows.mappings():
            tk = _ser_row(dict(r))
            tk["matters"] = []
            tk["default_rate"] = None
            timekeepers.append(tk)

        rate_rows = await db.execute(sa_text("""
            SELECT rc.id, rc.scope, rc.user_id, rc.client_id, rc.matter_id,
                   rc.hourly_rate, rc.effective_date, rc.end_date, rc.is_active
            FROM rate_cards rc
            WHERE trim(rc.tenant_id) = trim(:tid) AND rc.is_active = true
            ORDER BY rc.effective_date DESC
        """), {"tid": tid})
        rates = [_ser_row(dict(r)) for r in rate_rows.mappings()]

        assign_rows = await db.execute(sa_text("""
            SELECT tma.id, tma.timekeeper_id, tma.matter_id,
                   tma.role, tma.hourly_rate_override,
                   m.matter_name, m.matter_number,
                   c.client_name
            FROM timekeeper_matter_assignments tma
            JOIN matters m ON m.id = tma.matter_id
            LEFT JOIN clients c ON c.id = m.client_id AND trim(c.tenant_id) = trim(m.tenant_id)
            WHERE trim(tma.tenant_id) = trim(:tid)
            ORDER BY c.client_name, m.matter_name
        """), {"tid": tid})
        assignments = [_ser_row(dict(r)) for r in assign_rows.mappings()]

        matter_rows = await db.execute(sa_text("""
            SELECT m.id::text AS id, m.matter_name, m.matter_number,
                   c.client_name, c.id::text AS client_id
            FROM matters m
            LEFT JOIN clients c ON c.id = m.client_id AND trim(c.tenant_id) = trim(m.tenant_id)
            WHERE trim(m.tenant_id) = trim(:tid) AND m.status = 'active'
            ORDER BY c.client_name, m.matter_name
        """), {"tid": tid})
        matters = [_ser_row(dict(r)) for r in matter_rows.mappings()]

        user_rows = await db.execute(sa_text("""
            SELECT id, username, full_name
            FROM users
            WHERE trim(tenant_id) = trim(:tid)
        """), {"tid": tid})
        users = [dict(r) for r in user_rows.mappings()]

    return JSONResponse({
        "timekeepers": timekeepers,
        "rates": rates,
        "assignments": assignments,
        "matters": matters,
        "users": users,
    })


@router.post("/timekeepers-rates")
async def set_timekeeper_rate(request: Request):
    """Set or update a rate for a timekeeper."""
    tid = _tid(request)
    body = await request.json()
    tk_id = body.get("timekeeper_id", "").strip()
    scope = body.get("scope", "timekeeper")
    hourly_rate = float(body.get("hourly_rate", 0))
    matter_id = body.get("matter_id") or None
    client_id = body.get("client_id") or None
    effective = body.get("effective_date") or date.today().isoformat()

    if hourly_rate <= 0:
        return JSONResponse({"error": "Rate must be positive"}, status_code=400)

    async with AsyncSessionLocal() as db:
        user_match = await db.execute(sa_text("""
            SELECT u.id FROM users u
            JOIN ts_timekeepers tk ON LOWER(u.full_name) = LOWER(tk.ts_name)
                AND trim(tk.tenant_id) = trim(u.tenant_id)
            WHERE tk.ts_tk_id = :tk_id AND trim(tk.tenant_id) = trim(:tid)
            LIMIT 1
        """), {"tk_id": tk_id, "tid": tid})
        user_row = user_match.fetchone()
        rate_user_id = user_row[0] if user_row else None

        if scope == "timekeeper" and rate_user_id:
            await db.execute(sa_text("""
                UPDATE rate_cards SET is_active = false, end_date = :eff
                WHERE trim(tenant_id) = trim(:tid)
                  AND scope = 'timekeeper' AND user_id = :uid AND is_active = true
            """), {"tid": tid, "uid": rate_user_id, "eff": effective})
        elif scope == "matter" and rate_user_id and matter_id:
            await db.execute(sa_text("""
                UPDATE rate_cards SET is_active = false, end_date = :eff
                WHERE trim(tenant_id) = trim(:tid)
                  AND scope = 'matter' AND user_id = :uid
                  AND matter_id = CAST(:mid AS uuid) AND is_active = true
            """), {"tid": tid, "uid": rate_user_id, "mid": matter_id, "eff": effective})

        await db.execute(sa_text("""
            INSERT INTO rate_cards
                (tenant_id, scope, user_id, client_id, matter_id,
                 hourly_rate, effective_date, is_active)
            VALUES
                (:tid, :scope, :uid, :cid, :mid,
                 :rate, :eff, true)
        """), {
            "tid": tid, "scope": scope, "uid": rate_user_id,
            "cid": int(client_id) if client_id else None,
            "mid": matter_id, "rate": hourly_rate, "eff": effective,
        })
        await db.commit()

    return JSONResponse({"status": "ok", "rate": hourly_rate, "scope": scope})


@router.post("/timekeepers-rates/assign")
async def assign_timekeeper(request: Request):
    """Assign a timekeeper to a matter with optional rate override."""
    tid = _tid(request)
    body = await request.json()
    tk_id = body.get("timekeeper_id", "").strip()
    matter_id = body.get("matter_id", "").strip()
    role = body.get("role", "attorney")
    rate_override = body.get("hourly_rate_override")

    if not tk_id or not matter_id:
        return JSONResponse({"error": "timekeeper_id and matter_id required"}, status_code=400)

    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            INSERT INTO timekeeper_matter_assignments
                (tenant_id, timekeeper_id, matter_id, role, hourly_rate_override)
            VALUES (:tid, :tk, CAST(:mid AS uuid), :role, :rate)
            ON CONFLICT (tenant_id, timekeeper_id, matter_id) DO UPDATE
            SET role = EXCLUDED.role, hourly_rate_override = EXCLUDED.hourly_rate_override
        """), {
            "tid": tid, "tk": tk_id, "mid": matter_id,
            "role": role, "rate": float(rate_override) if rate_override else None,
        })
        await db.commit()

    return JSONResponse({"status": "assigned"})


@router.delete("/timekeepers-rates/assign")
async def remove_assignment(request: Request):
    """Remove a timekeeper-matter assignment."""
    tid = _tid(request)
    body = await request.json()
    assign_id = body.get("assignment_id")

    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            DELETE FROM timekeeper_matter_assignments
            WHERE id = :aid AND trim(tenant_id) = trim(:tid)
        """), {"aid": assign_id, "tid": tid})
        await db.commit()

    return JSONResponse({"status": "removed"})


# -- Time Entry Detail + Edit ---

@router.get("/time-entry/{entry_id}")
async def time_entry_detail(request: Request, entry_id: str, source: str = "ts"):
    """Get full detail for a time entry."""
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        if source == "ts":
            row = await db.execute(sa_text("""
                SELECT s.id, s.source_slip_id, s.slip_date, s.hours, s.rate,
                       s.wip_value, s.billed_value, s.billed, s.narrative,
                       s.source_client_id, s.source_tk_id, s.invoice_num,
                       COALESCE(tk.ts_name, tk.ts_initials, 'Unknown') AS tk_name,
                       COALESCE(tc.ts_name, 'Unknown') AS client_name,
                       tc.client_code, tc.matter_code
                FROM ts_slips s
                LEFT JOIN ts_timekeepers tk ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                LEFT JOIN ts_clients tc ON s.source_client_id = tc.ts_client_id
                    AND trim(tc.tenant_id) = trim(:tid)
                WHERE s.id = :eid AND trim(s.tenant_id) = trim(:tid)
            """), {"eid": int(entry_id), "tid": tid})
        else:
            row = await db.execute(sa_text("""
                SELECT te.id::text, te.entry_date AS slip_date, te.hours,
                       te.description AS narrative, te.billable, te.status,
                       te.source, te.ai_confidence, te.matter_id::text,
                       m.matter_name, c.client_name
                FROM time_entries te
                LEFT JOIN matters m ON m.id = te.matter_id
                LEFT JOIN clients c ON c.id = m.client_id
                WHERE te.id = CAST(:eid AS uuid) AND trim(te.tenant_id) = trim(:tid)
            """), {"eid": entry_id, "tid": tid})

        entry = row.mappings().fetchone()
        if not entry:
            return JSONResponse({"error": "Not found"}, status_code=404)

    return JSONResponse({"entry": _ser_row(dict(entry)), "source": source})


@router.put("/time-entry/{entry_id}")
async def update_time_entry(request: Request, entry_id: str, source: str = "ts"):
    """Update a time entry (ts_slips or time_entries)."""
    tid = _tid(request)
    body = await request.json()

    async with AsyncSessionLocal() as db:
        if source == "ts":
            updates = {}
            if "hours" in body: updates["hours"] = float(body["hours"])
            if "rate" in body: updates["rate"] = float(body["rate"])
            if "narrative" in body: updates["narrative"] = body["narrative"]
            if "slip_date" in body: updates["slip_date"] = body["slip_date"]
            if "source_tk_id" in body: updates["source_tk_id"] = body["source_tk_id"]

            if "hours" in updates or "rate" in updates:
                h = updates.get("hours") or float(body.get("current_hours", 0))
                r = updates.get("rate") or float(body.get("current_rate", 0))
                val = round(h * r, 2)
                billed_check = await db.execute(sa_text(
                    "SELECT billed FROM ts_slips WHERE id = :eid AND trim(tenant_id) = trim(:tid)"
                ), {"eid": int(entry_id), "tid": tid})
                brow = billed_check.fetchone()
                if brow and brow[0]:
                    updates["billed_value"] = val
                else:
                    updates["wip_value"] = val

            if not updates:
                return JSONResponse({"error": "No fields to update"}, status_code=400)

            set_clause = ", ".join(f"{k} = :{k}" for k in updates)
            await db.execute(sa_text(f"""
                UPDATE ts_slips SET {set_clause}
                WHERE id = :eid AND trim(tenant_id) = trim(:tid)
            """), {**updates, "eid": int(entry_id), "tid": tid})
        else:
            updates = {}
            if "hours" in body: updates["hours"] = float(body["hours"])
            if "description" in body: updates["description"] = body["description"]
            if "entry_date" in body: updates["entry_date"] = body["entry_date"]
            if "billable" in body: updates["billable"] = bool(body["billable"])
            if "status" in body: updates["status"] = body["status"]

            if not updates:
                return JSONResponse({"error": "No fields to update"}, status_code=400)

            set_clause = ", ".join(f"{k} = :{k}" for k in updates)
            await db.execute(sa_text(f"""
                UPDATE time_entries SET {set_clause}, updated_at = NOW()
                WHERE id = CAST(:eid AS uuid) AND trim(tenant_id) = trim(:tid)
            """), {**updates, "eid": entry_id, "tid": tid})

        await db.commit()

    return JSONResponse({"status": "updated"})


# -- AR Detail Report ---

@router.get("/ar-detail")
async def ar_detail_report(request: Request, bucket: Optional[str] = None):
    """Full AR detail with invoice-level breakdown."""
    tid = _tid(request)
    bucket_filter = ""
    if bucket == "0-30":
        bucket_filter = "AND CURRENT_DATE - created_at::date <= 30"
    elif bucket == "31-60":
        bucket_filter = "AND CURRENT_DATE - created_at::date BETWEEN 31 AND 60"
    elif bucket == "61-90":
        bucket_filter = "AND CURRENT_DATE - created_at::date BETWEEN 61 AND 90"
    elif bucket == "91-120":
        bucket_filter = "AND CURRENT_DATE - created_at::date BETWEEN 91 AND 120"
    elif bucket == "120+":
        bucket_filter = "AND CURRENT_DATE - created_at::date > 120"

    async with AsyncSessionLocal() as db:
        rows = await db.execute(sa_text(f"""
            SELECT ti.source_invoice_id, ti.invoice_num, ti.net_due,
                   ti.created_at, ti.paid_in_full,
                   CURRENT_DATE - ti.created_at::date AS age_days,
                   COALESCE(tc.ts_name, 'Unknown') AS client_name,
                   tc.client_code
            FROM ts_invoices ti
            LEFT JOIN ts_clients tc ON ti.source_client_id = tc.ts_client_id
                AND trim(tc.tenant_id) = trim(ti.tenant_id)
            WHERE trim(ti.tenant_id) = trim(:tid)
              AND ti.paid_in_full = false AND ti.net_due > 0
              {bucket_filter}
            ORDER BY ti.created_at ASC
        """), {"tid": tid})
        invoices = [_ser_row(dict(r)) for r in rows.mappings()]
        total = sum(i["net_due"] for i in invoices)

    return JSONResponse({
        "invoices": invoices,
        "total_ar": total,
        "count": len(invoices),
        "bucket": bucket,
    })


# -- Invoice Detail ---

@router.get("/invoice-detail/{inv_id}")
async def invoice_detail(request: Request, inv_id: str):
    """Full invoice detail with line items."""
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        row = await db.execute(sa_text("""
            SELECT ti.*, COALESCE(tc.ts_name, 'Unknown') AS client_name,
                   tc.client_code
            FROM ts_invoices ti
            LEFT JOIN ts_clients tc ON ti.source_client_id = tc.ts_client_id
                AND trim(tc.tenant_id) = trim(ti.tenant_id)
            WHERE ti.source_invoice_id = :iid AND trim(ti.tenant_id) = trim(:tid)
        """), {"iid": inv_id, "tid": tid})
        invoice = row.mappings().fetchone()

        if invoice:
            slip_rows = await db.execute(sa_text("""
                SELECT s.slip_date, s.hours, s.rate, s.billed_value,
                       s.narrative,
                       COALESCE(tk.ts_name, tk.ts_initials, 'Unknown') AS tk_name
                FROM ts_slips s
                LEFT JOIN ts_timekeepers tk ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                WHERE trim(s.tenant_id) = trim(:tid)
                  AND s.invoice_num = :inum
                ORDER BY s.slip_date
            """), {"tid": tid, "inum": invoice["invoice_num"]})
            slips = [_ser_row(dict(r)) for r in slip_rows.mappings()]
            return JSONResponse({
                "invoice": _ser_row(dict(invoice)),
                "line_items": slips,
                "source": "timeslips",
            })

        row2 = await db.execute(sa_text("""
            SELECT bi.*, m.matter_name, c.client_name
            FROM billing_invoices bi
            LEFT JOIN billing_matters m ON m.id = bi.matter_id
            LEFT JOIN clients c ON c.id = m.client_id
            WHERE bi.id = :iid AND bi.tenant_id = :tid
        """), {"iid": inv_id, "tid": tid})
        invoice2 = row2.mappings().fetchone()
        if invoice2:
            return JSONResponse({
                "invoice": _ser_row(dict(invoice2)),
                "line_items": [],
                "source": "native",
            })

    return JSONResponse({"error": "Invoice not found"}, status_code=404)
