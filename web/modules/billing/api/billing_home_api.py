"""Billing Home JSON API.

GET /api/v1/billing/home  -> all dashboard data for React billing homepage
"""
from __future__ import annotations
import logging
from datetime import date, timedelta
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing", tags=["billing-home-api"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()

def _period_dates(period: str) -> tuple:
    today = date.today()
    if period == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    if period == "quarter":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        return today.replace(month=q_start_month, day=1), today
    if period == "ytd":
        return today.replace(month=1, day=1), today
    return today.replace(day=1), today


@router.get("/home")
async def billing_home_api(request: Request):
    tid = _tid(request)
    period = request.query_params.get("period", "month")
    group_by = request.query_params.get("group_by", "attorney")
    client_id = request.query_params.get("client_id", "")
    date_from, date_to = _period_dates(period)

    scope_clause = ""
    scope_params = {}
    if client_id:
        scope_clause = """AND s.source_client_id = (
            SELECT tc.ts_client_id FROM ts_clients tc
            WHERE tc.praesidium_client_id = :scope_client_id
              AND trim(tc.tenant_id) = trim(:tid) LIMIT 1)"""
        scope_params = {"scope_client_id": str(client_id)}

    group_col = "COALESCE(tk.ts_name, tk.ts_initials, 'Unknown')"
    if group_by == "client":
        group_col = "COALESCE(tc.ts_name, 'Unknown')"

    result = {}

    async with AsyncSessionLocal() as session:
        # -- Matter tree --
        tree_rows = await session.execute(sa_text("""
            SELECT m.id::text AS id, m.matter_name, m.matter_number,
                   m.status, c.id::text AS client_id, c.client_name
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id
                AND trim(c.tenant_id) = trim(m.tenant_id)
            WHERE trim(m.tenant_id) = trim(:tid) AND m.status = 'active'
            ORDER BY c.client_name NULLS LAST, m.matter_name
        """), {"tid": tid})

        client_map = {}
        for r in tree_rows.mappings():
            cid = r["client_id"] or "unknown"
            cname = r["client_name"] or "Unknown Client"
            if cid not in client_map:
                client_map[cid] = {"client_id": cid, "client_name": cname, "matters": []}
            client_map[cid]["matters"].append({
                "id": r["id"], "matter_name": r["matter_name"] or "Untitled",
                "matter_number": r.get("matter_number") or "", "status": r.get("status") or "active",
            })
        clients = sorted(client_map.values(), key=lambda x: x["client_name"])
        result["matter_tree"] = {
            "clients": clients,
            "total_matters": sum(len(c["matters"]) for c in clients),
            "total_clients": len(clients),
        }

        # -- Revenue chart --
        wip_rows = await session.execute(sa_text(f"""
            SELECT {group_col} AS grp, SUM(s.wip_value) AS value
            FROM ts_slips s
            LEFT JOIN ts_timekeepers tk ON s.source_tk_id = tk.ts_tk_id
                AND trim(tk.tenant_id) = trim(:tid)
            LEFT JOIN ts_clients tc ON s.source_client_id = tc.ts_client_id
                AND trim(tc.tenant_id) = trim(:tid)
            WHERE trim(s.tenant_id) = trim(:tid) AND s.billed = false
              AND s.slip_date BETWEEN :dfrom AND :dto {scope_clause}
            GROUP BY grp ORDER BY value DESC NULLS LAST LIMIT 12
        """), {"tid": tid, "dfrom": date_from, "dto": date_to, **scope_params})
        wip_data = {r.grp: float(r.value or 0) for r in wip_rows.mappings()}

        billed_rows = await session.execute(sa_text(f"""
            SELECT {group_col} AS grp, SUM(s.billed_value) AS value
            FROM ts_slips s
            LEFT JOIN ts_timekeepers tk ON s.source_tk_id = tk.ts_tk_id
                AND trim(tk.tenant_id) = trim(:tid)
            LEFT JOIN ts_clients tc ON s.source_client_id = tc.ts_client_id
                AND trim(tc.tenant_id) = trim(:tid)
            WHERE trim(s.tenant_id) = trim(:tid) AND s.billed = true
              AND s.slip_date BETWEEN :dfrom AND :dto {scope_clause}
            GROUP BY grp ORDER BY value DESC NULLS LAST LIMIT 12
        """), {"tid": tid, "dfrom": date_from, "dto": date_to, **scope_params})
        billed_data = {r.grp: float(r.value or 0) for r in billed_rows.mappings()}

        all_groups = sorted(
            set(wip_data) | set(billed_data),
            key=lambda g: billed_data.get(g, 0) + wip_data.get(g, 0), reverse=True
        )[:10]
        chart_data = [{"label": g, "wip": wip_data.get(g, 0), "billed": billed_data.get(g, 0)}
                      for g in all_groups]
        total_wip = sum(d["wip"] for d in chart_data)
        total_billed = sum(d["billed"] for d in chart_data)
        result["revenue"] = {
            "chart_data": chart_data, "total_wip": total_wip, "total_billed": total_billed,
            "realization_rate": round(total_billed / total_wip * 100, 1) if total_wip else 0,
            "period": period, "group_by": group_by,
            "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
        }

        # -- AR aging --
        aging_rows = await session.execute(sa_text("""
            SELECT bucket, COUNT(*) AS cnt, SUM(net_due) AS total FROM (
                SELECT net_due, CASE
                    WHEN CURRENT_DATE - COALESCE(slip_end, created_at)::date <= 30  THEN '0-30'
                    WHEN CURRENT_DATE - COALESCE(slip_end, created_at)::date <= 60  THEN '31-60'
                    WHEN CURRENT_DATE - COALESCE(slip_end, created_at)::date <= 90  THEN '61-90'
                    WHEN CURRENT_DATE - COALESCE(slip_end, created_at)::date <= 120 THEN '91-120'
                    ELSE '120+' END AS bucket
                FROM ts_invoices WHERE trim(tenant_id) = trim(:tid)
                  AND paid_in_full = false AND net_due > 0
            ) sub GROUP BY bucket
        """), {"tid": tid})
        buckets_raw = {r.bucket: {"count": int(r.cnt), "amount": float(r.total or 0)}
                       for r in aging_rows.mappings()}
        bucket_order = ["0-30", "31-60", "61-90", "91-120", "120+"]
        buckets = [{"label": b, "count": buckets_raw.get(b, {}).get("count", 0),
                     "amount": buckets_raw.get(b, {}).get("amount", 0.0)} for b in bucket_order]
        total_ar = sum(b["amount"] for b in buckets)
        max_amt = max((b["amount"] for b in buckets), default=1) or 1
        for b in buckets:
            b["pct"] = round(b["amount"] / max_amt * 100) if max_amt else 0
        result["ar_aging"] = {"buckets": buckets, "total_ar": total_ar}

        # -- Timekeeper KPI --
        today = date.today()
        mth_start = today.replace(day=1)
        tk_rows = await session.execute(sa_text("""
            SELECT COALESCE(tk.ts_name, tk.ts_initials, 'Unknown') AS name,
                   SUM(CASE WHEN s.billed=false THEN s.wip_value ELSE 0 END) AS wip_value,
                   SUM(CASE WHEN s.billed=true THEN s.billed_value ELSE 0 END) AS billed_value,
                   SUM(CASE WHEN s.billed=true THEN s.hours ELSE 0 END) AS billed_hours
            FROM ts_slips s
            JOIN ts_timekeepers tk ON s.source_tk_id = tk.ts_tk_id
                AND trim(tk.tenant_id) = trim(:tid)
            WHERE trim(s.tenant_id) = trim(:tid) AND s.slip_date BETWEEN :mth AND :today
            GROUP BY name ORDER BY wip_value DESC NULLS LAST LIMIT 10
        """), {"tid": tid, "mth": mth_start, "today": today})
        timekeepers = []
        for row in tk_rows.mappings():
            wv, bv, bh = float(row.wip_value or 0), float(row.billed_value or 0), float(row.billed_hours or 0)
            tv = wv + bv
            timekeepers.append({"name": row.name, "wip_value": wv, "billed_value": bv,
                "billed_hours": bh, "util_rate": round(bh/176*100,1), "real_rate": round(bv/tv*100,1) if tv else 0})
        result["timekeepers"] = timekeepers

        # -- Bill state summary --
        bs = (await session.execute(sa_text("""
            SELECT
                COUNT(*) FILTER (WHERE paid_in_full=false AND net_due>0
                    AND CURRENT_DATE-COALESCE(slip_end,created_at)::date<=30) AS cur_cnt,
                COALESCE(SUM(net_due) FILTER (WHERE paid_in_full=false AND net_due>0
                    AND CURRENT_DATE-COALESCE(slip_end,created_at)::date<=30),0) AS cur_amt,
                COUNT(*) FILTER (WHERE paid_in_full=false AND net_due>0
                    AND CURRENT_DATE-COALESCE(slip_end,created_at)::date BETWEEN 31 AND 60) AS mid_cnt,
                COALESCE(SUM(net_due) FILTER (WHERE paid_in_full=false AND net_due>0
                    AND CURRENT_DATE-COALESCE(slip_end,created_at)::date BETWEEN 31 AND 60),0) AS mid_amt,
                COUNT(*) FILTER (WHERE paid_in_full=false AND net_due>0
                    AND CURRENT_DATE-COALESCE(slip_end,created_at)::date>60) AS over_cnt,
                COALESCE(SUM(net_due) FILTER (WHERE paid_in_full=false AND net_due>0
                    AND CURRENT_DATE-COALESCE(slip_end,created_at)::date>60),0) AS over_amt
            FROM ts_invoices WHERE trim(tenant_id) = trim(:tid)
        """), {"tid": tid})).mappings().fetchone()
        result["bill_states"] = [
            {"label":"Current","count":int(bs.cur_cnt or 0),"amount":float(bs.cur_amt or 0),"color":"#1d4ed8","bg":"#dbeafe"},
            {"label":"30-60d","count":int(bs.mid_cnt or 0),"amount":float(bs.mid_amt or 0),"color":"#854d0e","bg":"#fef9c3"},
            {"label":"Overdue","count":int(bs.over_cnt or 0),"amount":float(bs.over_amt or 0),"color":"#dc2626","bg":"#fef2f2"},
        ]

        # -- Recent slips --
        slip_rows = await session.execute(sa_text("""
            SELECT s.id, s.source_slip_id, s.rate, s.slip_date, s.hours, s.wip_value, s.billed_value, s.billed,
                   COALESCE(tk.ts_name, tk.ts_initials, 'Unknown') AS tk_name,
                   COALESCE(tc.ts_name, 'Unknown') AS client_name, s.narrative
            FROM ts_slips s
            LEFT JOIN ts_timekeepers tk ON s.source_tk_id = tk.ts_tk_id
                AND trim(tk.tenant_id) = trim(:tid)
            LEFT JOIN ts_clients tc ON s.source_client_id = tc.ts_client_id
                AND trim(tc.tenant_id) = trim(:tid)
            WHERE trim(s.tenant_id) = trim(:tid)
            ORDER BY s.slip_date DESC LIMIT 20
        """), {"tid": tid})
        result["recent_slips"] = [{
            "id": r.id, "source_slip_id": getattr(r, "source_slip_id", None), "rate": float(r.rate or 0),
            "slip_date": str(r.slip_date)[:10] if r.slip_date else "",
            "hours": float(r.hours or 0),
            "value": float(r.billed_value if r.billed else r.wip_value or 0),
            "billed": bool(r.billed), "tk_name": r.tk_name, "client_name": r.client_name,
            "narrative": (r.narrative or "")[:120],
        } for r in slip_rows.mappings()]

        # -- Recent invoices --
        inv_rows = await session.execute(sa_text("""
            SELECT DISTINCT ON (inv.invoice_num)
                inv.invoice_num,
                inv.net_due,
                inv.charge_fees,
                inv.charge_costs,
                inv.paid_in_full,
                inv.slip_end,
                inv.created_at,
                COALESCE(tc.ts_name, 'Unknown') AS client_name,
                tc.praesidium_client_id,
                CURRENT_DATE - COALESCE(inv.slip_end, inv.created_at)::date AS age_days
            FROM ts_invoices inv
            LEFT JOIN ts_slips s ON s.invoice_num = inv.invoice_num
                AND trim(s.tenant_id) = trim(inv.tenant_id)
            LEFT JOIN ts_clients tc ON s.source_client_id = tc.ts_client_id
                AND trim(tc.tenant_id) = trim(inv.tenant_id)
            WHERE trim(inv.tenant_id) = trim(:tid)
            ORDER BY inv.invoice_num, inv.created_at DESC
        """), {"tid": tid})
        all_invs = [dict(r) for r in inv_rows.mappings()]
        all_invs.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
        result["recent_invoices"] = [{
            "invoice_num": r["invoice_num"],
            "net_due": float(r["net_due"] or 0),
            "client_name": r["client_name"] or "Unknown",
            "client_id": r.get("praesidium_client_id") or "",
            "paid": bool(r["paid_in_full"]),
            "age_days": int(r["age_days"] or 0),
            "date": str(r["slip_end"] or r["created_at"] or "")[:10],
        } for r in all_invs[:20]]

    return JSONResponse(result)
