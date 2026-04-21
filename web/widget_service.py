"""
Billing Widget Service — data source functions for billing_home widget slots.

Confirmed schema (live DB, Apr 14 2026):
  ts_slips:       source_client_id, source_tk_id, wip_value (precomputed),
                  billed_value (precomputed), billed, slip_date, hours
  ts_timekeepers: ts_tk_id, ts_name, ts_initials
  ts_clients:     ts_client_id, ts_name, praesidium_client_id
  ts_matters:     ts_matter_id, ts_client_id, praesidium_matter_id
  ts_invoices:    invoice_num, invoice_status, net_due, paid_in_full, created_at
                  (NO: status, balance, due_date, client_id, matter_id)
  ts_payments:    date_entered, amount, source_client_id, source_invoice_id
  matters:        id, client_id, matter_name, matter_number, status, practice_area
  clients:        id, client_name, is_active
"""

import logging
from datetime import date, timedelta

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)


def _period_dates(period: str) -> tuple:
    today = date.today()
    if period == "last_month":
        first_this = today.replace(day=1)
        last_prev  = first_this - timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    if period == "quarter":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        return today.replace(month=q_start_month, day=1), today
    if period == "ytd":
        return today.replace(month=1, day=1), today
    return today.replace(day=1), today


def _get_client_scope(scope: dict) -> tuple[str, dict]:
    """Optional client filter — joins via ts_clients.praesidium_client_id."""
    req = scope.get("request")
    client_id = scope.get("client_id") or (
        req.query_params.get("client_id") if req else None
    )
    if client_id:
        return (
            """AND s.source_client_id = (
                SELECT tc.ts_client_id FROM ts_clients tc
                WHERE tc.praesidium_client_id::text = :scope_client_id
                  AND trim(tc.tenant_id) = trim(:tid)
                LIMIT 1
            )""",
            {"scope_client_id": str(client_id)}
        )
    return "", {}


# ---------------------------------------------------------------------------
# 1. billing_revenue_chart
# ---------------------------------------------------------------------------

async def get_billing_revenue_chart(scope: dict) -> dict:
    tenant_id = scope.get("tenant_id", "").strip()
    req       = scope.get("request")
    period    = req.query_params.get("period",   "month")    if req else "month"
    group_by  = req.query_params.get("group_by", "attorney") if req else "attorney"

    date_from, date_to = _period_dates(period)
    scope_clause, scope_params = _get_client_scope(scope)

    group_col = "COALESCE(tk.ts_name, tk.ts_initials, 'Unknown')"
    if group_by == "client":
        group_col = "COALESCE(tc.ts_name, 'Unknown')"

    try:
        async with AsyncSessionLocal() as session:

            wip_rows = await session.execute(sa_text(f"""
                SELECT {group_col} AS grp, SUM(s.wip_value) AS value
                FROM ts_slips s
                LEFT JOIN ts_timekeepers tk
                    ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                LEFT JOIN ts_clients tc
                    ON s.source_client_id = tc.ts_client_id
                    AND trim(tc.tenant_id) = trim(:tid)
                WHERE trim(s.tenant_id) = trim(:tid)
                  AND s.billed = false
                  AND s.slip_date BETWEEN :dfrom AND :dto
                  {scope_clause}
                GROUP BY grp ORDER BY value DESC NULLS LAST LIMIT 12
            """), {"tid": tenant_id, "dfrom": date_from, "dto": date_to,
                   **scope_params})
            wip_data = {r.grp: float(r.value or 0) for r in wip_rows.mappings()}

            billed_rows = await session.execute(sa_text(f"""
                SELECT {group_col} AS grp, SUM(s.billed_value) AS value
                FROM ts_slips s
                LEFT JOIN ts_timekeepers tk
                    ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                LEFT JOIN ts_clients tc
                    ON s.source_client_id = tc.ts_client_id
                    AND trim(tc.tenant_id) = trim(:tid)
                WHERE trim(s.tenant_id) = trim(:tid)
                  AND s.billed = true
                  AND s.slip_date BETWEEN :dfrom AND :dto
                  {scope_clause}
                GROUP BY grp ORDER BY value DESC NULLS LAST LIMIT 12
            """), {"tid": tenant_id, "dfrom": date_from, "dto": date_to,
                   **scope_params})
            billed_data = {r.grp: float(r.value or 0) for r in billed_rows.mappings()}

            collected_rows = await session.execute(sa_text("""
                SELECT COALESCE(tk.ts_name, tk.ts_initials, 'Unknown') AS grp,
                       SUM(p.amount) AS value
                FROM ts_payments p
                LEFT JOIN ts_slips s
                    ON s.source_invoice_id = p.source_invoice_id
                    AND trim(s.tenant_id) = trim(:tid)
                LEFT JOIN ts_timekeepers tk
                    ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                WHERE trim(p.tenant_id) = trim(:tid)
                  AND p.date_entered BETWEEN :dfrom AND :dto
                GROUP BY grp ORDER BY value DESC NULLS LAST LIMIT 12
            """), {"tid": tenant_id, "dfrom": date_from, "dto": date_to})
            collected_data = {r.grp: float(r.value or 0)
                              for r in collected_rows.mappings()}

        all_groups = sorted(
            set(wip_data) | set(billed_data) | set(collected_data),
            key=lambda g: billed_data.get(g, 0) + wip_data.get(g, 0),
            reverse=True
        )[:10]

        chart_data = [{
            "label":     g,
            "wip":       wip_data.get(g, 0),
            "billed":    billed_data.get(g, 0),
            "collected": collected_data.get(g, 0),
        } for g in all_groups]

        total_wip       = sum(d["wip"]       for d in chart_data)
        total_billed    = sum(d["billed"]     for d in chart_data)
        total_collected = sum(d["collected"]  for d in chart_data)

        return {
            "chart_data":       chart_data,
            "total_wip":        total_wip,
            "total_billed":     total_billed,
            "total_collected":  total_collected,
            "realization_rate": round(total_billed / total_wip * 100, 1) if total_wip else 0,
            "collection_rate":  round(total_collected / total_billed * 100, 1) if total_billed else 0,
            "period":           period,
            "group_by":         group_by,
            "date_from":        date_from.isoformat(),
            "date_to":          date_to.isoformat(),
            "error":            None,
        }

    except Exception as exc:
        logger.error("billing_revenue_chart error: %s", exc)
        return {"chart_data": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# 2. billing_ar_aging_snapshot
# ---------------------------------------------------------------------------

async def get_billing_ar_aging_snapshot(scope: dict) -> dict:
    """
    AR aging using ts_invoices. Ages from created_at (no due_date column).
    """
    tenant_id = scope.get("tenant_id", "").strip()

    try:
        async with AsyncSessionLocal() as session:

            aging_rows = await session.execute(sa_text("""
                SELECT
                    CASE
                        WHEN CURRENT_DATE - created_at::date <= 30  THEN '0-30'
                        WHEN CURRENT_DATE - created_at::date <= 60  THEN '31-60'
                        WHEN CURRENT_DATE - created_at::date <= 90  THEN '61-90'
                        WHEN CURRENT_DATE - created_at::date <= 120 THEN '91-120'
                        ELSE '120+'
                    END              AS bucket,
                    COUNT(*)         AS invoice_count,
                    SUM(net_due)     AS total_balance
                FROM ts_invoices
                WHERE trim(tenant_id) = trim(:tid)
                  AND paid_in_full = false
                  AND net_due > 0
                GROUP BY bucket
                ORDER BY
                    CASE bucket
                        WHEN '0-30'   THEN 1
                        WHEN '31-60'  THEN 2
                        WHEN '61-90'  THEN 3
                        WHEN '91-120' THEN 4
                        ELSE 5
                    END
            """), {"tid": tenant_id})

            buckets_raw = {
                r.bucket: {"count": int(r.invoice_count or 0),
                            "amount": float(r.total_balance or 0)}
                for r in aging_rows.mappings()
            }

            top_rows = await session.execute(sa_text("""
                SELECT invoice_num,
                       net_due                        AS balance,
                       CURRENT_DATE - created_at::date AS days_outstanding
                FROM ts_invoices
                WHERE trim(tenant_id) = trim(:tid)
                  AND paid_in_full = false
                  AND net_due > 0
                ORDER BY net_due DESC
                LIMIT 5
            """), {"tid": tenant_id})

            top_invoices = [{
                "invoice_number":   r.invoice_num,
                "client_name":      "—",
                "balance":          float(r.balance or 0),
                "days_outstanding": int(r.days_outstanding or 0),
            } for r in top_rows.mappings()]

        bucket_order = ["0-30", "31-60", "61-90", "91-120", "120+"]
        buckets = [{
            "label":  b,
            "count":  buckets_raw.get(b, {}).get("count",  0),
            "amount": buckets_raw.get(b, {}).get("amount", 0.0),
        } for b in bucket_order]

        total_ar   = sum(b["amount"] for b in buckets)
        max_amount = max((b["amount"] for b in buckets), default=1) or 1
        for b in buckets:
            b["pct"] = round(b["amount"] / max_amount * 100) if max_amount else 0

        return {"buckets": buckets, "total_ar": total_ar,
                "top_invoices": top_invoices, "error": None}

    except Exception as exc:
        logger.error("billing_ar_aging_snapshot error: %s", exc)
        return {"buckets": [], "total_ar": 0, "top_invoices": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# 3. billing_timekeeper_kpi
# ---------------------------------------------------------------------------

async def get_billing_timekeeper_kpi(scope: dict) -> dict:
    """Per-timekeeper KPI using precomputed wip_value / billed_value on ts_slips."""
    tenant_id = scope.get("tenant_id", "").strip()
    today     = date.today()
    mth_start = today.replace(day=1)

    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text("""
                SELECT
                    COALESCE(tk.ts_name, tk.ts_initials, 'Unknown') AS name,
                    SUM(CASE WHEN s.billed = false THEN s.wip_value    ELSE 0 END) AS wip_value,
                    SUM(CASE WHEN s.billed = true  THEN s.billed_value ELSE 0 END) AS billed_value,
                    SUM(CASE WHEN s.billed = true  THEN s.hours        ELSE 0 END) AS billed_hours
                FROM ts_slips s
                JOIN ts_timekeepers tk
                    ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                WHERE trim(s.tenant_id) = trim(:tid)
                  AND s.slip_date BETWEEN :mth_start AND :today
                GROUP BY name
                ORDER BY wip_value DESC NULLS LAST
                LIMIT 10
            """), {"tid": tenant_id, "mth_start": mth_start, "today": today})

            available_hours = 176.0
            timekeepers = []
            for row in rows.mappings():
                wip_val    = float(row.wip_value    or 0)
                billed_val = float(row.billed_value or 0)
                billed_hrs = float(row.billed_hours or 0)
                total_val  = wip_val + billed_val
                util_rate  = round(billed_hrs / available_hours * 100, 1)
                real_rate  = round(billed_val / total_val * 100, 1) if total_val else 0
                timekeepers.append({
                    "name":         row.name,
                    "wip_value":    wip_val,
                    "billed_value": billed_val,
                    "billed_hours": billed_hrs,
                    "util_rate":    util_rate,
                    "real_rate":    real_rate,
                    "util_pct":     min(100, util_rate),
                })

        return {"timekeepers": timekeepers, "error": None}

    except Exception as exc:
        logger.error("billing_timekeeper_kpi error: %s", exc)
        return {"timekeepers": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# 4. billing_bill_state_summary
# ---------------------------------------------------------------------------

async def get_billing_bill_state_summary(scope: dict) -> dict:
    """
    Outstanding invoice totals bucketed by age.
    ts_invoices has no status/due_date — uses created_at for aging.
    """
    tenant_id = scope.get("tenant_id", "").strip()

    try:
        async with AsyncSessionLocal() as session:
            r = (await session.execute(sa_text("""
                SELECT
                    COUNT(*) FILTER (
                        WHERE paid_in_full = false AND net_due > 0
                        AND CURRENT_DATE - created_at::date <= 30
                    )                  AS current_count,
                    COALESCE(SUM(net_due) FILTER (
                        WHERE paid_in_full = false AND net_due > 0
                        AND CURRENT_DATE - created_at::date <= 30
                    ), 0)              AS current_amount,
                    COUNT(*) FILTER (
                        WHERE paid_in_full = false AND net_due > 0
                        AND CURRENT_DATE - created_at::date BETWEEN 31 AND 60
                    )                  AS mid_count,
                    COALESCE(SUM(net_due) FILTER (
                        WHERE paid_in_full = false AND net_due > 0
                        AND CURRENT_DATE - created_at::date BETWEEN 31 AND 60
                    ), 0)              AS mid_amount,
                    COUNT(*) FILTER (
                        WHERE paid_in_full = false AND net_due > 0
                        AND CURRENT_DATE - created_at::date > 60
                    )                  AS overdue_count,
                    COALESCE(SUM(net_due) FILTER (
                        WHERE paid_in_full = false AND net_due > 0
                        AND CURRENT_DATE - created_at::date > 60
                    ), 0)              AS overdue_amount
                FROM ts_invoices
                WHERE trim(tenant_id) = trim(:tid)
            """), {"tid": tenant_id})).mappings().fetchone()

            if not r:
                return {"states": [], "error": None}

        states = [
            {
                "label":  "Current",
                "count":  int(r.current_count  or 0),
                "amount": float(r.current_amount or 0),
                "color":  "#1d4ed8",
                "bg":     "#dbeafe",
                "href":   "/billing/invoices?aging=current",
            },
            {
                "label":  "30-60d",
                "count":  int(r.mid_count  or 0),
                "amount": float(r.mid_amount or 0),
                "color":  "#854d0e",
                "bg":     "#fef9c3",
                "href":   "/billing/invoices?aging=due",
            },
            {
                "label":  "Overdue",
                "count":  int(r.overdue_count  or 0),
                "amount": float(r.overdue_amount or 0),
                "color":  "#dc2626",
                "bg":     "#fef2f2",
                "href":   "/billing/invoices?aging=overdue",
            },
        ]

        return {"states": states, "error": None}

    except Exception as exc:
        logger.error("billing_bill_state_summary error: %s", exc)
        return {"states": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# 5. firm_matter_tree — billing context (WIP-enriched)
# ---------------------------------------------------------------------------

async def get_firm_matter_tree_billing(scope: dict) -> dict:
    """
    Client/matter tree with per-matter WIP from ts_slips.
    Joins: matters → clients (Praesidium) → ts_clients → ts_slips.
    """
    tenant_id = scope.get("tenant_id", "").strip()
    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text("""
                SELECT
                    m.id::text        AS id,
                    m.matter_name,
                    m.matter_number,
                    m.status,
                    c.id::text        AS client_id,
                    c.client_name,
                    COALESCE(SUM(s.wip_value), 0) AS wip_value
                FROM matters m
                LEFT JOIN clients c
                    ON m.client_id = c.id
                    AND trim(c.tenant_id) = trim(m.tenant_id)
                LEFT JOIN ts_clients tc
                    ON tc.praesidium_client_id = c.id
                    AND trim(tc.tenant_id) = trim(m.tenant_id)
                LEFT JOIN ts_slips s
                    ON s.source_client_id = tc.ts_client_id
                    AND trim(s.tenant_id) = trim(m.tenant_id)
                    AND s.billed = false
                WHERE trim(m.tenant_id) = trim(:tid)
                  AND m.status = 'active'
                GROUP BY m.id, m.matter_name, m.matter_number,
                         m.status, c.id, c.client_name
                ORDER BY c.client_name NULLS LAST, m.matter_name
            """), {"tid": tenant_id})

            client_map: dict = {}
            for row in rows.mappings():
                cid   = row["client_id"] or "unknown"
                cname = row["client_name"] or "Unknown Client"
                if cid not in client_map:
                    client_map[cid] = {"client_id": cid,
                                       "client_name": cname, "matters": []}
                client_map[cid]["matters"].append({
                    "id":            row["id"],
                    "matter_name":   row["matter_name"] or "Untitled",
                    "matter_number": row.get("matter_number") or "",
                    "status":        row.get("status") or "active",
                    "client_id":     cid,
                    "wip_value":     float(row["wip_value"] or 0),
                    "doc_count":     0,
                })

        clients = sorted(client_map.values(), key=lambda x: x["client_name"])
        total_matters = sum(len(c["matters"]) for c in clients)
        return {"clients": clients, "total_matters": total_matters,
                "total_clients": len(clients), "error": None}

    except Exception as exc:
        logger.error("get_firm_matter_tree_billing error: %s", exc)
        return {"clients": [], "total_matters": 0,
                "total_clients": 0, "error": str(exc)}
