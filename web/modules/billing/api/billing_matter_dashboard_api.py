"""Billing Matter Dashboard JSON API.

GET /api/v1/billing/matter-dashboard/{matter_id}
  -> all dashboard data for React matter detail page
"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing", tags=["billing-matter-dashboard"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()


@router.get("/matter-dashboard/{matter_id}")
async def matter_dashboard_api(request: Request, matter_id: str):
    tid = _tid(request)

    result = {}

    async with AsyncSessionLocal() as db:
        # -- Matter + client info --
        m_row = await db.execute(sa_text("""
            SELECT m.id::text, m.matter_name, m.matter_number, m.status,
                   m.practice_area, m.billing_type, m.hourly_rate,
                   m.court, m.cause_number, m.judge, m.jurisdiction,
                   m.open_date, m.close_date, m.sol_date, m.notes, m.folder_path,
                   c.id::text AS client_id, c.client_name
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id
                AND trim(c.tenant_id) = trim(m.tenant_id)
            WHERE m.id = CAST(:mid AS uuid) AND trim(m.tenant_id) = trim(:tid)
        """), {"mid": matter_id, "tid": tid})
        matter = m_row.mappings().fetchone()
        if not matter:
            return JSONResponse({"error": "Matter not found"}, status_code=404)

        import datetime
        from decimal import Decimal
        def _ser(v):
            if isinstance(v, (datetime.date, datetime.datetime)): return v.isoformat()
            if isinstance(v, Decimal): return float(v)
            import uuid as _u
            if isinstance(v, _u.UUID): return str(v)
            return v

        result["matter"] = {k: _ser(v) for k, v in dict(matter).items()}

        # Find ts_client_ids via matter_number -> ts_clients nickname2
        matter_number = matter["matter_number"] or ""
        ts_client_ids = []
        if matter_number:
            tc_rows = await db.execute(sa_text("""
                SELECT ts_client_id FROM ts_clients
                WHERE trim(tenant_id) = trim(:tid)
                  AND ts_raw->>'nickname2' = :mn
            """), {"tid": tid, "mn": matter_number})
            ts_client_ids = [str(r.ts_client_id) for r in tc_rows.mappings()]

        # -- KPIs --
        if ts_client_ids:
            kpi = await db.execute(sa_text("""
                SELECT
                    SUM(CASE WHEN billed=false THEN wip_value ELSE 0 END) AS wip_value,
                    SUM(CASE WHEN billed=false THEN hours ELSE 0 END) AS wip_hours,
                    SUM(CASE WHEN billed=true THEN billed_value ELSE 0 END) AS total_billed,
                    SUM(CASE WHEN billed=true THEN hours ELSE 0 END) AS billed_hours,
                    COUNT(*) AS slip_count
                FROM ts_slips
                WHERE trim(tenant_id) = trim(:tid)
                  AND source_client_id = ANY(:ids)
            """), {"tid": tid, "ids": ts_client_ids})
            kr = kpi.mappings().fetchone()
            result["kpis"] = {
                "wip_value": float(kr.wip_value or 0),
                "wip_hours": float(kr.wip_hours or 0),
                "total_billed": float(kr.total_billed or 0),
                "billed_hours": float(kr.billed_hours or 0),
                "slip_count": int(kr.slip_count or 0),
            }

            # AR for this matter
            ar = await db.execute(sa_text("""
                SELECT COUNT(*) AS cnt, COALESCE(SUM(ti.net_due),0) AS total
                FROM ts_invoices ti
                WHERE trim(ti.tenant_id) = trim(:tid)
                  AND ti.paid_in_full = false AND ti.net_due > 0
                  AND EXISTS (
                      SELECT 1 FROM ts_slips s
                      WHERE s.invoice_num = ti.invoice_num
                        AND trim(s.tenant_id) = trim(:tid)
                        AND s.source_client_id = ANY(:ids))
            """), {"tid": tid, "ids": ts_client_ids})
            ar_row = ar.mappings().fetchone()
            result["kpis"]["ar_balance"] = float(ar_row.total or 0)
            result["kpis"]["open_invoice_count"] = int(ar_row.cnt or 0)
        else:
            result["kpis"] = {
                "wip_value": 0, "wip_hours": 0, "total_billed": 0,
                "billed_hours": 0, "slip_count": 0, "ar_balance": 0,
                "open_invoice_count": 0,
            }

        # -- Timekeeper allocation --
        if ts_client_ids:
            tk_rows = await db.execute(sa_text("""
                SELECT
                    COALESCE(tk.ts_name, tk.ts_initials, s.source_tk_id) AS tk_name,
                    SUM(s.hours) AS total_hours,
                    SUM(s.wip_value + s.billed_value) AS total_value,
                    SUM(CASE WHEN s.billed=false THEN s.hours ELSE 0 END) AS wip_hours,
                    SUM(CASE WHEN s.billed=false THEN s.wip_value ELSE 0 END) AS wip_value,
                    SUM(CASE WHEN s.billed=true THEN s.hours ELSE 0 END) AS billed_hours,
                    SUM(CASE WHEN s.billed=true THEN s.billed_value ELSE 0 END) AS billed_value
                FROM ts_slips s
                LEFT JOIN ts_timekeepers tk ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                WHERE trim(s.tenant_id) = trim(:tid) AND s.source_client_id = ANY(:ids)
                GROUP BY tk_name ORDER BY total_hours DESC
            """), {"tid": tid, "ids": ts_client_ids})
            result["tk_allocation"] = [{
                "tk_name": r.tk_name, "total_hours": float(r.total_hours or 0),
                "total_value": float(r.total_value or 0),
                "wip_hours": float(r.wip_hours or 0), "wip_value": float(r.wip_value or 0),
                "billed_hours": float(r.billed_hours or 0), "billed_value": float(r.billed_value or 0),
            } for r in tk_rows.mappings()]
        else:
            result["tk_allocation"] = []

        # -- Open invoices --
        if ts_client_ids:
            inv_rows = await db.execute(sa_text("""
                SELECT ti.invoice_num, ti.net_due, ti.created_at,
                       CURRENT_DATE - ti.created_at::date AS age_days
                FROM ts_invoices ti
                WHERE trim(ti.tenant_id) = trim(:tid)
                  AND ti.paid_in_full = false AND ti.net_due > 0
                  AND EXISTS (
                      SELECT 1 FROM ts_slips s
                      WHERE s.invoice_num = ti.invoice_num
                        AND trim(s.tenant_id) = trim(:tid)
                        AND s.source_client_id = ANY(:ids))
                ORDER BY ti.created_at DESC LIMIT 20
            """), {"tid": tid, "ids": ts_client_ids})
            result["open_invoices"] = [{
                "invoice_num": r.invoice_num, "net_due": float(r.net_due or 0),
                "age_days": int(r.age_days or 0),
                "created_at": str(r.created_at)[:10] if r.created_at else "",
            } for r in inv_rows.mappings()]
        else:
            result["open_invoices"] = []

        # -- Recent slips --
        if ts_client_ids:
            slip_rows = await db.execute(sa_text("""
                SELECT s.source_slip_id, s.slip_date, s.hours,
                       s.wip_value, s.billed_value, s.billed, s.narrative,
                       s.source_tk_id,
                       COALESCE(tk.ts_name, tk.ts_initials, 'Unknown') AS tk_name
                FROM ts_slips s
                LEFT JOIN ts_timekeepers tk ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                WHERE trim(s.tenant_id) = trim(:tid) AND s.source_client_id = ANY(:ids)
                ORDER BY s.slip_date DESC LIMIT 100
            """), {"tid": tid, "ids": ts_client_ids})
            result["recent_slips"] = [{
                "source_slip_id": r.source_slip_id,
                "slip_date": str(r.slip_date)[:10] if r.slip_date else "",
                "hours": float(r.hours or 0),
                "value": float(r.billed_value if r.billed else r.wip_value or 0),
                "billed": bool(r.billed), "tk_name": r.tk_name,
                "narrative": (r.narrative or "")[:200],
                "source_tk_id": r.source_tk_id,
            } for r in slip_rows.mappings()]
        else:
            result["recent_slips"] = []

    return JSONResponse(result)

@router.put("/matter-dashboard/{matter_id}/update")
async def update_matter(request: Request, matter_id: str):
    tid = _tid(request)
    body = await request.json()
    fields = {}
    for k in ['practice_area','matter_type','billing_type','hourly_rate','court','cause_number','judge','jurisdiction','sol_date','notes','status']:
        if k in body:
            fields[k] = body[k] if body[k] else (None if k != 'status' else body[k])
    if not fields:
        return JSONResponse({"error":"No fields"}, status_code=400)
    set_parts = []
    params = {"mid": matter_id, "tid": tid}
    for k, v in fields.items():
        set_parts.append(f"{k} = :{k}")
        params[k] = float(v) if k == 'hourly_rate' and v else v
    sql = f"UPDATE matters SET {', '.join(set_parts)} WHERE id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)"
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text(sql), params)
        await db.commit()
    return JSONResponse({"status":"updated"})


@router.post("/matter-dashboard/{matter_id}/seed-folders")
async def seed_matter_folders_api(request: Request, matter_id: str):
    """Seed the standard folder structure for a single matter."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)
    try:
        from modules.dms.jobs.folder_seeder import seed_matter_folders
        result = seed_matter_folders(tenant_id=tid, matter_id=matter_id)
        return JSONResponse(result)
    except Exception as exc:
        logger.error("seed-folders error matter=%s: %s", matter_id, exc, exc_info=True)
        return JSONResponse({"error": str(exc)}, status_code=500)
