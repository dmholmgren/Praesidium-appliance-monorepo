"""Billing Client Dashboard JSON API — v3 with invoice UUIDs for PDF viewer.

GET /api/v1/billing/client-dashboard/{client_id}
"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing", tags=["billing-client-dashboard"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()

@router.get("/client-dashboard/{client_id}")
async def client_dashboard_api(request: Request, client_id: str):
    tid = _tid(request)
    result = {}

    async with AsyncSessionLocal() as db:
        cr = await db.execute(sa_text("""
            SELECT client_name, client_type, client_number, primary_contact,
                   email, phone, address1, address2, city, state, zip_code, notes
            FROM clients WHERE id = CAST(:cid AS uuid) AND trim(tenant_id) = trim(:tid)
        """), {"cid": client_id, "tid": tid})
        client_row = cr.mappings().fetchone()
        if client_row: result["client"] = dict(client_row)

        m_rows = await db.execute(sa_text("""
            SELECT id::text, matter_name, matter_number, status, practice_area
            FROM matters WHERE client_id = CAST(:cid AS uuid) AND trim(tenant_id) = trim(:tid)
            ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, matter_name
        """), {"cid": client_id, "tid": tid})
        import datetime, uuid as _u
        from decimal import Decimal
        def _ser(v):
            if isinstance(v, (datetime.date, datetime.datetime)): return v.isoformat()
            if isinstance(v, Decimal): return float(v)
            if isinstance(v, _u.UUID): return str(v)
            return v
        matters = [{k: _ser(v) for k, v in dict(r).items()} for r in m_rows.mappings()]
        result["matters"] = matters

        matter_numbers = [m["matter_number"] for m in matters if m.get("matter_number")]
        empty = dict(wip_value=0, wip_hours=0, ar_balance=0, open_invoice_count=0,
                     total_billed=0, billed_hours=0, slip_count=0,
                     tk_allocation=[], open_invoices=[], matter_billing=[], matter_wip=[],
                     ar_aging={"over_90":0,"over_60":0,"over_30":0,"current":0,"over_90_count":0})
        if not matter_numbers:
            result.update(empty); return JSONResponse(result)

        tc = await db.execute(sa_text("""
            SELECT ts_client_id, ts_raw->>'nickname2' AS nickname2 FROM ts_clients
            WHERE trim(tenant_id) = trim(:tid) AND ts_raw->>'nickname2' = ANY(:nums)
        """), {"tid": tid, "nums": matter_numbers})
        tc_rows = tc.mappings().fetchall()
        ts_ids = [str(r["ts_client_id"]) for r in tc_rows]
        ts_id_to_nick = {str(r["ts_client_id"]): r["nickname2"] for r in tc_rows}
        if not ts_ids:
            result.update(empty); return JSONResponse(result)

        nick_to_matter = {m["matter_number"]: m["matter_name"] for m in matters if m.get("matter_number")}

        kpi = await db.execute(sa_text("""
            SELECT SUM(CASE WHEN billed=false THEN wip_value ELSE 0 END) AS wip_value,
                   SUM(CASE WHEN billed=false THEN hours ELSE 0 END) AS wip_hours,
                   SUM(CASE WHEN billed=true THEN billed_value ELSE 0 END) AS total_billed,
                   SUM(CASE WHEN billed=true THEN hours ELSE 0 END) AS billed_hours,
                   COUNT(*) AS slip_count
            FROM ts_slips WHERE trim(tenant_id) = trim(:tid) AND source_client_id = ANY(:ids)
        """), {"tid": tid, "ids": ts_ids})
        kr = kpi.mappings().fetchone()
        result["wip_value"] = float(kr.wip_value or 0)
        result["wip_hours"] = float(kr.wip_hours or 0)
        result["total_billed"] = float(kr.total_billed or 0)
        result["billed_hours"] = float(kr.billed_hours or 0)
        result["slip_count"] = int(kr.slip_count or 0)

        ar = await db.execute(sa_text("""
            SELECT COUNT(*) AS cnt, COALESCE(SUM(ti.net_due),0) AS total
            FROM ts_invoices ti WHERE trim(ti.tenant_id) = trim(:tid) AND ti.paid_in_full = false AND ti.net_due > 0
              AND EXISTS (SELECT 1 FROM ts_slips s WHERE s.invoice_num = ti.invoice_num
                          AND trim(s.tenant_id) = trim(:tid) AND s.source_client_id = ANY(:ids))
        """), {"tid": tid, "ids": ts_ids})
        ar_row = ar.mappings().fetchone()
        result["ar_balance"] = float(ar_row.total or 0)
        result["open_invoice_count"] = int(ar_row.cnt or 0)

        tk = await db.execute(sa_text("""
            SELECT COALESCE(tk.ts_name, tk.ts_initials, s.source_tk_id) AS tk_name,
                   SUM(s.hours) AS total_hours, SUM(s.wip_value + s.billed_value) AS total_value,
                   SUM(CASE WHEN s.billed=false THEN s.hours ELSE 0 END) AS wip_hours,
                   SUM(CASE WHEN s.billed=false THEN s.wip_value ELSE 0 END) AS wip_value,
                   SUM(CASE WHEN s.billed=true THEN s.hours ELSE 0 END) AS billed_hours,
                   SUM(CASE WHEN s.billed=true THEN s.billed_value ELSE 0 END) AS billed_value
            FROM ts_slips s LEFT JOIN ts_timekeepers tk ON s.source_tk_id = tk.ts_tk_id AND trim(tk.tenant_id) = trim(:tid)
            WHERE trim(s.tenant_id) = trim(:tid) AND s.source_client_id = ANY(:ids)
            GROUP BY tk_name ORDER BY total_hours DESC
        """), {"tid": tid, "ids": ts_ids})
        result["tk_allocation"] = [{
            "tk_name": r.tk_name, "total_hours": float(r.total_hours or 0),
            "total_value": float(r.total_value or 0),
            "wip_hours": float(r.wip_hours or 0), "wip_value": float(r.wip_value or 0),
            "billed_hours": float(r.billed_hours or 0), "billed_value": float(r.billed_value or 0),
        } for r in tk.mappings()]

        mb = await db.execute(sa_text("""
            SELECT s.source_client_id,
                   SUM(CASE WHEN s.billed=true THEN s.billed_value ELSE 0 END) AS billed_value,
                   SUM(CASE WHEN s.billed=false THEN s.wip_value ELSE 0 END) AS wip_value,
                   SUM(s.hours) AS total_hours, SUM(s.wip_value + s.billed_value) AS total_value
            FROM ts_slips s WHERE trim(s.tenant_id) = trim(:tid) AND s.source_client_id = ANY(:ids)
            GROUP BY s.source_client_id ORDER BY total_value DESC
        """), {"tid": tid, "ids": ts_ids})
        matter_billing = []; matter_wip = []
        for r in mb.mappings():
            nick = ts_id_to_nick.get(r.source_client_id, "")
            mname = nick_to_matter.get(nick, nick or "Unknown")
            matter_billing.append({"matter_name": mname, "matter_number": nick,
                "billed_value": float(r.billed_value or 0), "total_value": float(r.total_value or 0)})
            if float(r.wip_value or 0) > 0:
                matter_wip.append({"matter_name": mname, "wip_value": float(r.wip_value or 0)})
        result["matter_billing"] = matter_billing
        result["matter_wip"] = matter_wip

        # Open invoices with UUID for PDF viewer
        inv = await db.execute(sa_text("""
            SELECT ti.invoice_num, ti.net_due, ti.created_at,
                   CURRENT_DATE - ti.created_at::date AS age_days,
                   i.id::text AS invoice_uuid
            FROM ts_invoices ti
            LEFT JOIN invoices i ON i.invoice_number = ti.invoice_num::text AND trim(i.tenant_id) = trim(:tid)
            WHERE trim(ti.tenant_id) = trim(:tid) AND ti.paid_in_full = false AND ti.net_due > 0
              AND EXISTS (SELECT 1 FROM ts_slips s WHERE s.invoice_num = ti.invoice_num
                          AND trim(s.tenant_id) = trim(:tid) AND s.source_client_id = ANY(:ids))
            ORDER BY ti.created_at DESC LIMIT 50
        """), {"tid": tid, "ids": ts_ids})
        result["open_invoices"] = [{
            "invoice_num": r.invoice_num, "net_due": float(r.net_due or 0),
            "age_days": int(r.age_days or 0),
            "created_at": str(r.created_at)[:10] if r.created_at else "",
            "invoice_uuid": r.invoice_uuid,
        } for r in inv.mappings()]

        aging = await db.execute(sa_text("""
            SELECT SUM(CASE WHEN CURRENT_DATE - ti.created_at::date > 90 THEN ti.net_due ELSE 0 END) AS over_90,
                   SUM(CASE WHEN CURRENT_DATE - ti.created_at::date BETWEEN 61 AND 90 THEN ti.net_due ELSE 0 END) AS over_60,
                   SUM(CASE WHEN CURRENT_DATE - ti.created_at::date BETWEEN 31 AND 60 THEN ti.net_due ELSE 0 END) AS over_30,
                   SUM(CASE WHEN CURRENT_DATE - ti.created_at::date <= 30 THEN ti.net_due ELSE 0 END) AS current_bal,
                   COUNT(*) FILTER (WHERE CURRENT_DATE - ti.created_at::date > 90) AS over_90_count
            FROM ts_invoices ti WHERE trim(ti.tenant_id) = trim(:tid) AND ti.paid_in_full = false AND ti.net_due > 0
              AND EXISTS (SELECT 1 FROM ts_slips s WHERE s.invoice_num = ti.invoice_num
                          AND trim(s.tenant_id) = trim(:tid) AND s.source_client_id = ANY(:ids))
        """), {"tid": tid, "ids": ts_ids})
        ar_aging = aging.mappings().fetchone()
        result["ar_aging"] = {"over_90": float(ar_aging.over_90 or 0), "over_60": float(ar_aging.over_60 or 0),
            "over_30": float(ar_aging.over_30 or 0), "current": float(ar_aging.current_bal or 0),
            "over_90_count": int(ar_aging.over_90_count or 0)}

    return JSONResponse(result)


@router.put("/client-dashboard/{client_id}/update")
async def update_client(request: Request, client_id: str):
    tid = _tid(request)
    body = await request.json()
    fields = {}
    for k in ['client_name','client_type','client_number','primary_contact','email','phone','address1','address2','city','state','zip_code','notes']:
        if k in body: fields[k] = body[k] if body[k] else None
    if not fields: return JSONResponse({"error":"No fields"}, status_code=400)
    set_parts = [f"{k} = :{k}" for k in fields]
    params = {"cid": client_id, "tid": tid, **fields}
    sql = f"UPDATE clients SET {', '.join(set_parts)} WHERE id = CAST(:cid AS uuid) AND trim(tenant_id) = trim(:tid)"
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text(sql), params)
        await db.commit()
    return JSONResponse({"status":"updated"})
