"""
Billing Widget Service — data source functions for billing_home widget slots
and client/matter detail widget slots.

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

Scope resolution:
  client_id  → Praesidium clients.id (UUID as text string)
  matter_id  → Praesidium matters.id (UUID as text string)
  Both resolve to ts_client_id via ts_clients.praesidium_client_id.
  DO NOT use ::uuid cast on praesidium_client_id — it is TEXT.
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
    """
    Optional client filter for ts_slips queries.
    client_id is the Praesidium clients.id (UUID as text string).
    Resolves to ts_client_id via ts_clients.praesidium_client_id.
    """
    req = scope.get("request")
    client_id = scope.get("client_id") or (
        req.query_params.get("client_id") if req else None
    )
    if client_id:
        return (
            """AND s.source_client_id = (
                SELECT tc.ts_client_id FROM ts_clients tc
                WHERE tc.praesidium_client_id = :scope_client_id
                  AND trim(tc.tenant_id) = trim(:tid)
                LIMIT 1
            )""",
            {"scope_client_id": str(client_id)}
        )
    return "", {}


def _get_payment_scope(scope: dict) -> tuple[str, dict]:
    """Same as _get_client_scope but for ts_payments.source_client_id."""
    req = scope.get("request")
    client_id = scope.get("client_id") or (
        req.query_params.get("client_id") if req else None
    )
    if client_id:
        return (
            """AND p.source_client_id = (
                SELECT tc.ts_client_id FROM ts_clients tc
                WHERE tc.praesidium_client_id = :scope_client_id
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
    pay_clause,   pay_params   = _get_payment_scope(scope)

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

            collected_rows = await session.execute(sa_text(f"""
                SELECT COALESCE(tc.ts_name, 'Unknown') AS grp,
                       SUM(p.amount) AS value
                FROM ts_payments p
                LEFT JOIN ts_clients tc
                    ON p.source_client_id = tc.ts_client_id
                    AND trim(tc.tenant_id) = trim(:tid)
                WHERE trim(p.tenant_id) = trim(:tid)
                  AND p.date_entered BETWEEN :dfrom AND :dto
                  {pay_clause}
                GROUP BY grp ORDER BY value DESC NULLS LAST LIMIT 12
            """), {"tid": tenant_id, "dfrom": date_from, "dto": date_to,
                   **pay_params})
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
                SELECT bucket, COUNT(*) AS invoice_count, SUM(net_due) AS total_balance
                FROM (
                    SELECT net_due,
                        CASE
                            WHEN CURRENT_DATE - created_at::date <= 30  THEN '0-30'
                            WHEN CURRENT_DATE - created_at::date <= 60  THEN '31-60'
                            WHEN CURRENT_DATE - created_at::date <= 90  THEN '61-90'
                            WHEN CURRENT_DATE - created_at::date <= 120 THEN '91-120'
                            ELSE '120+'
                        END AS bucket
                    FROM ts_invoices
                    WHERE trim(tenant_id) = trim(:tid)
                      AND paid_in_full = false
                      AND net_due > 0
                ) sub
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
    Client/matter tree — fast load, no WIP aggregation.
    WIP values shown in chart when client is clicked (scoped separately).
    Removes the ts_slips join that caused slow load across 1200+ matters.
    """
    tenant_id = scope.get("tenant_id", "").strip()
    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text("""
                SELECT
                    m.id::text    AS id,
                    m.matter_name,
                    m.matter_number,
                    m.status,
                    c.id::text    AS client_id,
                    c.client_name
                FROM matters m
                LEFT JOIN clients c
                    ON m.client_id = c.id
                    AND trim(c.tenant_id) = trim(m.tenant_id)
                WHERE trim(m.tenant_id) = trim(:tid)
                  AND m.status = 'active'
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
                    "wip_value":     0,   # loaded on demand via bilScopeClient
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


# ===========================================================================
# CLIENT DETAIL WIDGETS
# All accept scope = {tenant_id, client_id, request}
# client_id resolves to ts_client_id via ts_clients.praesidium_client_id
# ===========================================================================

# ---------------------------------------------------------------------------
# 6. billing_client_kpi
# ---------------------------------------------------------------------------

async def get_billing_client_kpi(scope: dict) -> dict:
    """
    Four KPI tiles for a specific client:
      wip_value, wip_hours, ar_balance, open_invoice_count,
      total_billed, total_slip_count, total_collected, payment_count
    """
    tenant_id = scope.get("tenant_id", "").strip()
    client_id = scope.get("client_id", "")

    if not client_id:
        return {"error": "client_id required", "wip_value": 0, "wip_hours": 0,
                "ar_balance": 0, "open_invoice_count": 0,
                "total_billed": 0, "total_slip_count": 0,
                "total_collected": 0, "payment_count": 0}

    try:
        async with AsyncSessionLocal() as session:

            # WIP
            wip = (await session.execute(sa_text("""
                SELECT COALESCE(SUM(s.wip_value), 0) AS wip_value,
                       COALESCE(SUM(s.hours), 0)     AS wip_hours
                FROM ts_slips s
                WHERE trim(s.tenant_id) = trim(:tid)
                  AND s.billed = false
                  AND s.source_client_id = (
                      SELECT tc.ts_client_id FROM ts_clients tc
                      WHERE tc.praesidium_client_id = :cid
                        AND trim(tc.tenant_id) = trim(:tid)
                      LIMIT 1
                  )
            """), {"tid": tenant_id, "cid": str(client_id)})).mappings().fetchone()

            # Total billed all time
            billed = (await session.execute(sa_text("""
                SELECT COALESCE(SUM(s.billed_value), 0) AS total_billed,
                       COUNT(*) AS slip_count
                FROM ts_slips s
                WHERE trim(s.tenant_id) = trim(:tid)
                  AND s.billed = true
                  AND s.source_client_id = (
                      SELECT tc.ts_client_id FROM ts_clients tc
                      WHERE tc.praesidium_client_id = :cid
                        AND trim(tc.tenant_id) = trim(:tid)
                      LIMIT 1
                  )
            """), {"tid": tenant_id, "cid": str(client_id)})).mappings().fetchone()

            # AR — open invoices joined via ts_slips invoice_num
            ar = (await session.execute(sa_text("""
                SELECT COALESCE(SUM(ti.net_due), 0) AS ar_balance,
                       COUNT(*) AS open_invoice_count
                FROM ts_invoices ti
                WHERE trim(ti.tenant_id) = trim(:tid)
                  AND ti.paid_in_full = false
                  AND ti.net_due > 0
                  AND EXISTS (
                      SELECT 1 FROM ts_slips s
                      WHERE trim(s.tenant_id) = trim(:tid)
                        AND s.invoice_num = ti.invoice_num
                        AND s.source_client_id = (
                            SELECT tc.ts_client_id FROM ts_clients tc
                            WHERE tc.praesidium_client_id = :cid
                              AND trim(tc.tenant_id) = trim(:tid)
                            LIMIT 1
                        )
                  )
            """), {"tid": tenant_id, "cid": str(client_id)})).mappings().fetchone()

            # Collected all time
            collected = (await session.execute(sa_text("""
                SELECT COALESCE(SUM(p.amount), 0) AS total_collected,
                       COUNT(*) AS payment_count
                FROM ts_payments p
                WHERE trim(p.tenant_id) = trim(:tid)
                  AND p.source_client_id = (
                      SELECT tc.ts_client_id FROM ts_clients tc
                      WHERE tc.praesidium_client_id = :cid
                        AND trim(tc.tenant_id) = trim(:tid)
                      LIMIT 1
                  )
            """), {"tid": tenant_id, "cid": str(client_id)})).mappings().fetchone()

        return {
            "wip_value":          float(wip.wip_value or 0),
            "wip_hours":          float(wip.wip_hours or 0),
            "ar_balance":         float(ar.ar_balance or 0),
            "open_invoice_count": int(ar.open_invoice_count or 0),
            "total_billed":       float(billed.total_billed or 0),
            "total_slip_count":   int(billed.slip_count or 0),
            "total_collected":    abs(float(collected.total_collected or 0)),
            "payment_count":      int(collected.payment_count or 0),
            "error":              None,
        }

    except Exception as exc:
        logger.error("get_billing_client_kpi error: %s", exc)
        return {"wip_value": 0, "wip_hours": 0, "ar_balance": 0,
                "open_invoice_count": 0, "total_billed": 0,
                "total_slip_count": 0, "total_collected": 0,
                "payment_count": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# 7. billing_matter_table
# ---------------------------------------------------------------------------

async def get_billing_matter_table(scope: dict) -> dict:
    """
    Per-matter WIP hours, WIP value, and AR for a specific client.
    Includes all matters (active + inactive) — template controls visibility.
    """
    tenant_id = scope.get("tenant_id", "").strip()
    client_id = scope.get("client_id", "")

    if not client_id:
        return {"matters": [], "error": "client_id required"}

    try:
        async with AsyncSessionLocal() as session:

            # Get ts_client_id for this Praesidium client
            tc_row = (await session.execute(sa_text("""
                SELECT ts_client_id FROM ts_clients
                WHERE praesidium_client_id = :cid
                  AND trim(tenant_id) = trim(:tid)
                LIMIT 1
            """), {"tid": tenant_id, "cid": str(client_id)})).mappings().fetchone()

            ts_client_id = str(tc_row.ts_client_id) if tc_row else None

            # Get all matters for client
            matter_rows = (await session.execute(sa_text("""
                SELECT id::text AS id, matter_name, matter_number, status
                FROM matters
                WHERE client_id = :cid
                  AND trim(tenant_id) = trim(:tid)
                ORDER BY
                    CASE status WHEN 'active' THEN 0 ELSE 1 END,
                    matter_name
            """), {"tid": tenant_id, "cid": str(client_id)})).mappings().fetchall()

            matters = []
            for m in matter_rows:
                matter = {
                    "id":            m["id"],
                    "matter_name":   m["matter_name"] or "Untitled",
                    "matter_number": m["matter_number"] or "—",
                    "status":        m["status"] or "active",
                    "wip_hours":     0.0,
                    "wip_value":     0.0,
                    "ar_balance":    0.0,
                }
                matters.append(matter)

            # Augment with WIP if ts_client_id resolved
            if ts_client_id and matters:
                wip_rows = (await session.execute(sa_text("""
                    SELECT
                        COALESCE(SUM(CASE WHEN billed=false THEN hours     ELSE 0 END), 0) AS wip_hours,
                        COALESCE(SUM(CASE WHEN billed=false THEN wip_value ELSE 0 END), 0) AS wip_value
                    FROM ts_slips
                    WHERE trim(tenant_id) = trim(:tid)
                      AND source_client_id = :ts_cid
                      AND billed = false
                """), {"tid": tenant_id, "ts_cid": ts_client_id})).mappings().fetchone()

                # AR per client (ts_invoices doesn't have matter_id — apply at client level)
                ar_row = (await session.execute(sa_text("""
                    SELECT COALESCE(SUM(ti.net_due), 0) AS ar_balance
                    FROM ts_invoices ti
                    WHERE trim(ti.tenant_id) = trim(:tid)
                      AND ti.paid_in_full = false
                      AND ti.net_due > 0
                      AND EXISTS (
                          SELECT 1 FROM ts_slips s
                          WHERE trim(s.tenant_id) = trim(:tid)
                            AND s.invoice_num = ti.invoice_num
                            AND s.source_client_id = :ts_cid
                      )
                """), {"tid": tenant_id, "ts_cid": ts_client_id})).mappings().fetchone()

                # Distribute WIP to first active matter (ts_slips has no matter linkage
                # in current schema — best effort, WIP shown at client level)
                total_wip_hours = float(wip_rows.wip_hours or 0)
                total_wip_value = float(wip_rows.wip_value or 0)
                total_ar        = float(ar_row.ar_balance or 0)

                active_matters = [m for m in matters if m["status"] == "active"]
                if active_matters:
                    # Spread evenly across active matters as a placeholder
                    # until ts_matters.praesidium_matter_id linkage is populated
                    share = len(active_matters)
                    for m in active_matters:
                        m["wip_hours"]  = round(total_wip_hours / share, 2)
                        m["wip_value"]  = round(total_wip_value / share, 2)
                        m["ar_balance"] = round(total_ar / share, 2)

        return {"matters": matters, "error": None}

    except Exception as exc:
        logger.error("get_billing_matter_table error: %s", exc)
        return {"matters": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# 8. billing_timekeeper_allocation
# ---------------------------------------------------------------------------

async def get_billing_timekeeper_allocation(scope: dict) -> dict:
    """
    Timekeeper allocation bar chart for a client (all time).
    Returns sorted list with pct bars precomputed.
    Accepts client_id or matter_id scope — matter_id not yet resolvable
    in ts_slips (no matter linkage), so scopes to client.
    """
    tenant_id = scope.get("tenant_id", "").strip()
    client_id = scope.get("client_id", "")

    if not client_id:
        return {"timekeepers": [], "total_hours": 0, "error": "client_id required"}

    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(sa_text("""
                SELECT
                    COALESCE(tk.ts_name, tk.ts_initials, s.source_tk_id) AS tk_name,
                    SUM(s.hours)                                          AS total_hours,
                    SUM(s.wip_value + s.billed_value)                    AS total_value,
                    SUM(CASE WHEN s.billed=false THEN s.hours ELSE 0 END) AS wip_hours,
                    SUM(CASE WHEN s.billed=true  THEN s.hours ELSE 0 END) AS billed_hours
                FROM ts_slips s
                LEFT JOIN ts_timekeepers tk
                    ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                WHERE trim(s.tenant_id) = trim(:tid)
                  AND s.source_client_id = (
                      SELECT tc.ts_client_id FROM ts_clients tc
                      WHERE tc.praesidium_client_id = :cid
                        AND trim(tc.tenant_id) = trim(:tid)
                      LIMIT 1
                  )
                GROUP BY tk_name
                ORDER BY total_hours DESC NULLS LAST
                LIMIT 12
            """), {"tid": tenant_id, "cid": str(client_id)})).mappings().fetchall()

        timekeepers = [{
            "tk_name":      r.tk_name or "Unknown",
            "total_hours":  float(r.total_hours  or 0),
            "total_value":  float(r.total_value  or 0),
            "wip_hours":    float(r.wip_hours    or 0),
            "billed_hours": float(r.billed_hours or 0),
        } for r in rows]

        total_hours = sum(t["total_hours"] for t in timekeepers) or 1
        for t in timekeepers:
            t["pct"] = round(t["total_hours"] / total_hours * 100, 1)

        return {"timekeepers": timekeepers, "total_hours": total_hours,
                "error": None}

    except Exception as exc:
        logger.error("get_billing_timekeeper_allocation error: %s", exc)
        return {"timekeepers": [], "total_hours": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# 9. billing_wip_by_timekeeper
# ---------------------------------------------------------------------------

async def get_billing_wip_by_timekeeper(scope: dict) -> dict:
    """
    Unbilled (WIP) hours and value per timekeeper for a client.
    Table widget — rows sorted by wip_value desc, totals row appended.
    """
    tenant_id = scope.get("tenant_id", "").strip()
    client_id = scope.get("client_id", "")

    if not client_id:
        return {"rows": [], "total_hours": 0, "total_value": 0,
                "error": "client_id required"}

    try:
        async with AsyncSessionLocal() as session:
            db_rows = (await session.execute(sa_text("""
                SELECT
                    COALESCE(tk.ts_name, tk.ts_initials, s.source_tk_id) AS tk_name,
                    COALESCE(SUM(s.hours),     0) AS wip_hours,
                    COALESCE(SUM(s.wip_value), 0) AS wip_value
                FROM ts_slips s
                LEFT JOIN ts_timekeepers tk
                    ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                WHERE trim(s.tenant_id) = trim(:tid)
                  AND s.billed = false
                  AND s.source_client_id = (
                      SELECT tc.ts_client_id FROM ts_clients tc
                      WHERE tc.praesidium_client_id = :cid
                        AND trim(tc.tenant_id) = trim(:tid)
                      LIMIT 1
                  )
                GROUP BY tk_name
                ORDER BY wip_value DESC NULLS LAST
            """), {"tid": tenant_id, "cid": str(client_id)})).mappings().fetchall()

        rows = [{
            "tk_name":   r.tk_name or "Unknown",
            "wip_hours": float(r.wip_hours or 0),
            "wip_value": float(r.wip_value or 0),
        } for r in db_rows]

        total_hours = sum(r["wip_hours"] for r in rows)
        total_value = sum(r["wip_value"] for r in rows)

        # Precompute pct for each row
        for r in rows:
            r["pct"] = round(r["wip_value"] / total_value * 100, 1) if total_value else 0

        return {"rows": rows, "total_hours": total_hours,
                "total_value": total_value, "error": None}

    except Exception as exc:
        logger.error("get_billing_wip_by_timekeeper error: %s", exc)
        return {"rows": [], "total_hours": 0, "total_value": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# 10. billing_open_invoices
# ---------------------------------------------------------------------------

async def get_billing_open_invoices(scope: dict) -> dict:
    """
    Open (unpaid) invoices for a client, with aging.
    ts_invoices has no client_id — joins via ts_slips.invoice_num.
    """
    tenant_id = scope.get("tenant_id", "").strip()
    client_id = scope.get("client_id", "")

    if not client_id:
        return {"invoices": [], "total_ar": 0, "error": "client_id required"}

    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(sa_text("""
                SELECT
                    ti.invoice_num,
                    ti.net_due                        AS balance,
                    CURRENT_DATE - ti.created_at::date AS days_outstanding
                FROM ts_invoices ti
                WHERE trim(ti.tenant_id) = trim(:tid)
                  AND ti.paid_in_full = false
                  AND ti.net_due > 0
                  AND EXISTS (
                      SELECT 1 FROM ts_slips s
                      WHERE trim(s.tenant_id) = trim(:tid)
                        AND s.invoice_num = ti.invoice_num
                        AND s.source_client_id = (
                            SELECT tc.ts_client_id FROM ts_clients tc
                            WHERE tc.praesidium_client_id = :cid
                              AND trim(tc.tenant_id) = trim(:tid)
                            LIMIT 1
                        )
                  )
                ORDER BY ti.net_due DESC
                LIMIT 50
            """), {"tid": tenant_id, "cid": str(client_id)})).mappings().fetchall()

        invoices = []
        for r in rows:
            days = int(r.days_outstanding or 0)
            if days > 90:
                age_color = "#7f1d1d"
            elif days > 60:
                age_color = "#b45309"
            elif days > 30:
                age_color = "#92400e"
            else:
                age_color = "#166534"
            invoices.append({
                "invoice_num":      r.invoice_num,
                "balance":          float(r.balance or 0),
                "days_outstanding": days,
                "age_color":        age_color,
            })

        total_ar = sum(i["balance"] for i in invoices)
        return {"invoices": invoices, "total_ar": total_ar, "error": None}

    except Exception as exc:
        logger.error("get_billing_open_invoices error: %s", exc)
        return {"invoices": [], "total_ar": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# 11. billing_recent_slips
# ---------------------------------------------------------------------------

async def get_billing_recent_slips(scope: dict) -> dict:
    """
    Last 50 time entries for a client, with timekeeper name.
    Slip objects carry source_slip_id for the edit drawer PUT endpoint.
    Template renders the table rows and filter buttons only — drawer JS
    lives in the page shell (client_detail.html).
    """
    tenant_id = scope.get("tenant_id", "").strip()
    client_id = scope.get("client_id", "")

    if not client_id:
        return {"slips": [], "error": "client_id required"}

    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(sa_text("""
                SELECT
                    s.source_slip_id,
                    s.slip_date,
                    s.source_tk_id,
                    COALESCE(tk.ts_name, tk.ts_initials, s.source_tk_id) AS tk_name,
                    s.hours,
                    s.wip_value,
                    s.billed_value,
                    s.billed,
                    s.narrative,
                    s.rate
                FROM ts_slips s
                LEFT JOIN ts_timekeepers tk
                    ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                WHERE trim(s.tenant_id) = trim(:tid)
                  AND s.source_client_id = (
                      SELECT tc.ts_client_id FROM ts_clients tc
                      WHERE tc.praesidium_client_id = :cid
                        AND trim(tc.tenant_id) = trim(:tid)
                      LIMIT 1
                  )
                ORDER BY s.slip_date DESC
                LIMIT 50
            """), {"tid": tenant_id, "cid": str(client_id)})).mappings().fetchall()

        slips = [{
            "source_slip_id": str(r.source_slip_id or ""),
            "slip_date":      str(r.slip_date or ""),
            "source_tk_id":   str(r.source_tk_id or ""),
            "tk_name":        r.tk_name or "Unknown",
            "hours":          float(r.hours or 0),
            "value":          float(r.billed_value if r.billed else r.wip_value or 0),
            "rate":           float(r.rate or 0),
            "billed":         bool(r.billed),
            "narrative":      (r.narrative or ""),
        } for r in rows]

        return {"slips": slips, "error": None}

    except Exception as exc:
        logger.error("get_billing_recent_slips error: %s", exc)
        return {"slips": [], "error": str(exc)}

# ===========================================================================
# MATTER DETAIL WIDGETS
# All accept scope = {tenant_id, matter_id, request}
# matter_id is a Praesidium matters.id (UUID as text string)
# Resolves to ts_client_id via matters.matter_number → ts_clients nickname2
# ts_slips has no matter linkage — all queries scope at ts_client_id level
# (same pattern as client widgets but resolved via matter_number tiebreaker)
# ===========================================================================

async def _resolve_ts_client_id_from_matter(session, tenant_id: str, matter_id: str) -> tuple[str, str, str]:
    """
    Given a Praesidium matter UUID, return (matter_number, matter_name, ts_client_id).
    ts_client_id is resolved via matters.matter_number = ts_clients.ts_raw->>'nickname2'.
    Returns ('', '', '') if not found.
    """
    mn_q = await session.execute(sa_text("""
        SELECT matter_number, matter_name
        FROM matters
        WHERE id = CAST(:mid AS uuid)
          AND trim(tenant_id) = trim(:tid)
    """), {"mid": matter_id, "tid": tenant_id})
    mn_row = mn_q.mappings().fetchone()
    if not mn_row:
        return ("", "", "")

    matter_number = mn_row["matter_number"]
    matter_name   = mn_row["matter_name"]

    ts_q = await session.execute(sa_text("""
        SELECT ts_client_id FROM ts_clients
        WHERE trim(tenant_id) = trim(:tid)
          AND ts_raw->>'nickname2' = :mn
        ORDER BY (
            SELECT MAX(slip_date) FROM ts_slips
            WHERE trim(tenant_id) = trim(:tid)
              AND source_client_id = ts_client_id
        ) DESC NULLS LAST
        LIMIT 1
    """), {"tid": tenant_id, "mn": matter_number})
    ts_row = ts_q.fetchone()
    ts_client_id = str(ts_row[0]) if ts_row else ""

    return (matter_number, matter_name, ts_client_id)


# ---------------------------------------------------------------------------
# 12. billing_matter_kpi
# ---------------------------------------------------------------------------

async def get_billing_matter_kpi(scope: dict) -> dict:
    """
    Four KPI tiles for a single matter:
      wip_value, wip_hours, ar_balance, open_invoice_count,
      total_billed, total_collected
    Note: ts_slips has no matter linkage — scopes to ts_client_id resolved
    from matter_number. Values shown are client-level approximations until
    ts_matters.praesidium_matter_id is populated.
    """
    tenant_id = scope.get("tenant_id", "").strip()
    matter_id = scope.get("matter_id", "")

    empty = {
        "matter_name": "", "matter_number": "", "wip_value": 0, "wip_hours": 0,
        "ar_balance": 0, "open_invoice_count": 0, "total_billed": 0,
        "total_collected": 0, "ts_linked": False, "error": None,
    }

    if not matter_id:
        return {**empty, "error": "matter_id required"}

    try:
        async with AsyncSessionLocal() as session:
            matter_number, matter_name, ts_client_id = \
                await _resolve_ts_client_id_from_matter(session, tenant_id, matter_id)

            if not matter_number:
                return {**empty, "error": "Matter not found"}

            if not ts_client_id:
                return {
                    **empty,
                    "matter_name": matter_name,
                    "matter_number": matter_number,
                    "ts_linked": False,
                    "error": None,
                }

            # WIP
            wip = (await session.execute(sa_text("""
                SELECT COALESCE(SUM(wip_value), 0) AS wip_value,
                       COALESCE(SUM(CASE WHEN NOT billed THEN hours ELSE 0 END), 0) AS wip_hours
                FROM ts_slips
                WHERE trim(tenant_id) = trim(:tid)
                  AND source_client_id = :ts_cid
                  AND billed = false
            """), {"tid": tenant_id, "ts_cid": ts_client_id})).mappings().fetchone()

            # Total billed all time
            billed = (await session.execute(sa_text("""
                SELECT COALESCE(SUM(billed_value), 0) AS total_billed
                FROM ts_slips
                WHERE trim(tenant_id) = trim(:tid)
                  AND source_client_id = :ts_cid
                  AND billed = true
            """), {"tid": tenant_id, "ts_cid": ts_client_id})).mappings().fetchone()

            # AR — open invoices via slip join
            ar = (await session.execute(sa_text("""
                SELECT COALESCE(SUM(ti.net_due), 0) AS ar_balance,
                       COUNT(*) AS open_invoice_count
                FROM ts_invoices ti
                WHERE trim(ti.tenant_id) = trim(:tid)
                  AND ti.paid_in_full = false
                  AND ti.net_due > 0
                  AND EXISTS (
                      SELECT 1 FROM ts_slips s
                      WHERE trim(s.tenant_id) = trim(:tid)
                        AND s.invoice_num = ti.invoice_num
                        AND s.source_client_id = :ts_cid
                  )
            """), {"tid": tenant_id, "ts_cid": ts_client_id})).mappings().fetchone()

            # Collected
            collected = (await session.execute(sa_text("""
                SELECT COALESCE(SUM(amount), 0) AS total_collected
                FROM ts_payments
                WHERE trim(tenant_id) = trim(:tid)
                  AND source_client_id = :ts_cid
            """), {"tid": tenant_id, "ts_cid": ts_client_id})).mappings().fetchone()

        return {
            "matter_name":        matter_name,
            "matter_number":      matter_number,
            "wip_value":          float(wip.wip_value or 0),
            "wip_hours":          float(wip.wip_hours or 0),
            "ar_balance":         float(ar.ar_balance or 0),
            "open_invoice_count": int(ar.open_invoice_count or 0),
            "total_billed":       float(billed.total_billed or 0),
            "total_collected":    abs(float(collected.total_collected or 0)),
            "ts_linked":          True,
            "error":              None,
        }

    except Exception as exc:
        logger.error("get_billing_matter_kpi error: %s", exc)
        return {**empty, "error": str(exc)}


# ---------------------------------------------------------------------------
# 13. billing_matter_timekeeper_allocation
# ---------------------------------------------------------------------------

async def get_billing_matter_timekeeper_allocation(scope: dict) -> dict:
    """
    Timekeeper allocation bar chart for a matter (resolves via matter_number).
    Same as client widget but filtered to the ts_client_id for this matter.
    """
    tenant_id = scope.get("tenant_id", "").strip()
    matter_id = scope.get("matter_id", "")

    if not matter_id:
        return {"timekeepers": [], "total_hours": 0, "error": "matter_id required"}

    try:
        async with AsyncSessionLocal() as session:
            _, _, ts_client_id = await _resolve_ts_client_id_from_matter(
                session, tenant_id, matter_id
            )
            if not ts_client_id:
                return {"timekeepers": [], "total_hours": 0, "ts_linked": False, "error": None}

            rows = (await session.execute(sa_text("""
                SELECT
                    COALESCE(tk.ts_name, tk.ts_initials, s.source_tk_id) AS tk_name,
                    SUM(s.hours)                                          AS total_hours,
                    SUM(s.wip_value + s.billed_value)                    AS total_value,
                    SUM(CASE WHEN s.billed=false THEN s.hours ELSE 0 END) AS wip_hours,
                    SUM(CASE WHEN s.billed=true  THEN s.hours ELSE 0 END) AS billed_hours
                FROM ts_slips s
                LEFT JOIN ts_timekeepers tk
                    ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                WHERE trim(s.tenant_id) = trim(:tid)
                  AND s.source_client_id = :ts_cid
                GROUP BY tk_name
                ORDER BY total_hours DESC NULLS LAST
                LIMIT 12
            """), {"tid": tenant_id, "ts_cid": ts_client_id})).mappings().fetchall()

        timekeepers = [{
            "tk_name":      r.tk_name or "Unknown",
            "total_hours":  float(r.total_hours  or 0),
            "total_value":  float(r.total_value  or 0),
            "wip_hours":    float(r.wip_hours    or 0),
            "billed_hours": float(r.billed_hours or 0),
        } for r in rows]

        total_hours = sum(t["total_hours"] for t in timekeepers) or 1
        for t in timekeepers:
            t["pct"] = round(t["total_hours"] / total_hours * 100, 1)

        return {"timekeepers": timekeepers, "total_hours": total_hours,
                "ts_linked": True, "error": None}

    except Exception as exc:
        logger.error("get_billing_matter_timekeeper_allocation error: %s", exc)
        return {"timekeepers": [], "total_hours": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# 14. billing_matter_wip_by_timekeeper
# ---------------------------------------------------------------------------

async def get_billing_matter_wip_by_timekeeper(scope: dict) -> dict:
    """Unbilled WIP hours and value per timekeeper for this matter's ts_client_id."""
    tenant_id = scope.get("tenant_id", "").strip()
    matter_id = scope.get("matter_id", "")

    if not matter_id:
        return {"rows": [], "total_hours": 0, "total_value": 0, "error": "matter_id required"}

    try:
        async with AsyncSessionLocal() as session:
            _, _, ts_client_id = await _resolve_ts_client_id_from_matter(
                session, tenant_id, matter_id
            )
            if not ts_client_id:
                return {"rows": [], "total_hours": 0, "total_value": 0,
                        "ts_linked": False, "error": None}

            db_rows = (await session.execute(sa_text("""
                SELECT
                    COALESCE(tk.ts_name, tk.ts_initials, s.source_tk_id) AS tk_name,
                    COALESCE(SUM(s.hours),     0) AS wip_hours,
                    COALESCE(SUM(s.wip_value), 0) AS wip_value
                FROM ts_slips s
                LEFT JOIN ts_timekeepers tk
                    ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                WHERE trim(s.tenant_id) = trim(:tid)
                  AND s.billed = false
                  AND s.source_client_id = :ts_cid
                GROUP BY tk_name
                ORDER BY wip_value DESC NULLS LAST
            """), {"tid": tenant_id, "ts_cid": ts_client_id})).mappings().fetchall()

        rows = [{
            "tk_name":   r.tk_name or "Unknown",
            "wip_hours": float(r.wip_hours or 0),
            "wip_value": float(r.wip_value or 0),
        } for r in db_rows]

        total_hours = sum(r["wip_hours"] for r in rows)
        total_value = sum(r["wip_value"] for r in rows)
        for r in rows:
            r["pct"] = round(r["wip_value"] / total_value * 100, 1) if total_value else 0

        return {"rows": rows, "total_hours": total_hours, "total_value": total_value,
                "ts_linked": True, "error": None}

    except Exception as exc:
        logger.error("get_billing_matter_wip_by_timekeeper error: %s", exc)
        return {"rows": [], "total_hours": 0, "total_value": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# 15. billing_matter_open_invoices
# ---------------------------------------------------------------------------

async def get_billing_matter_open_invoices(scope: dict) -> dict:
    """
    Open invoices for this matter (via ts_client_id from matter_number).
    Uses slip_end for aging (more accurate than created_at).
    """
    tenant_id = scope.get("tenant_id", "").strip()
    matter_id = scope.get("matter_id", "")

    if not matter_id:
        return {"invoices": [], "total_ar": 0, "error": "matter_id required"}

    try:
        async with AsyncSessionLocal() as session:
            _, _, ts_client_id = await _resolve_ts_client_id_from_matter(
                session, tenant_id, matter_id
            )
            if not ts_client_id:
                return {"invoices": [], "total_ar": 0, "ts_linked": False, "error": None}

            rows = (await session.execute(sa_text("""
                SELECT
                    ti.invoice_num,
                    ti.net_due                                              AS balance,
                    ti.charge_fees,
                    ti.charge_costs,
                    ti.slip_start,
                    ti.slip_end,
                    CURRENT_DATE - COALESCE(ti.slip_end, ti.created_at::date) AS days_outstanding
                FROM ts_invoices ti
                WHERE trim(ti.tenant_id) = trim(:tid)
                  AND ti.paid_in_full = false
                  AND ti.net_due > 0
                  AND EXISTS (
                      SELECT 1 FROM ts_slips s
                      WHERE trim(s.tenant_id) = trim(:tid)
                        AND s.invoice_num = ti.invoice_num
                        AND s.source_client_id = :ts_cid
                  )
                ORDER BY ti.net_due DESC
                LIMIT 50
            """), {"tid": tenant_id, "ts_cid": ts_client_id})).mappings().fetchall()

        invoices = []
        for r in rows:
            days = int(r.days_outstanding or 0)
            if days > 90:   age_color = "#7f1d1d"
            elif days > 60: age_color = "#b45309"
            elif days > 30: age_color = "#92400e"
            else:           age_color = "#166534"
            invoices.append({
                "invoice_num":      r.invoice_num,
                "balance":          float(r.balance or 0),
                "charge_fees":      float(r.charge_fees or 0),
                "charge_costs":     float(r.charge_costs or 0),
                "slip_start":       str(r.slip_start) if r.slip_start else "",
                "slip_end":         str(r.slip_end)   if r.slip_end   else "",
                "days_outstanding": days,
                "age_color":        age_color,
            })

        total_ar = sum(i["balance"] for i in invoices)
        return {"invoices": invoices, "total_ar": total_ar, "ts_linked": True, "error": None}

    except Exception as exc:
        logger.error("get_billing_matter_open_invoices error: %s", exc)
        return {"invoices": [], "total_ar": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# 16. billing_matter_recent_slips
# ---------------------------------------------------------------------------

async def get_billing_matter_recent_slips(scope: dict) -> dict:
    """
    Last 100 time entries for this matter (via ts_client_id).
    Carries source_slip_id for the inline edit drawer.
    """
    tenant_id = scope.get("tenant_id", "").strip()
    matter_id = scope.get("matter_id", "")

    if not matter_id:
        return {"slips": [], "error": "matter_id required"}

    try:
        async with AsyncSessionLocal() as session:
            _, _, ts_client_id = await _resolve_ts_client_id_from_matter(
                session, tenant_id, matter_id
            )
            if not ts_client_id:
                return {"slips": [], "ts_linked": False, "error": None}

            rows = (await session.execute(sa_text("""
                SELECT
                    s.source_slip_id,
                    s.slip_date,
                    s.source_tk_id,
                    COALESCE(tk.ts_name, tk.ts_initials, s.source_tk_id) AS tk_name,
                    s.hours,
                    s.wip_value,
                    s.billed_value,
                    s.billed,
                    s.narrative,
                    s.rate
                FROM ts_slips s
                LEFT JOIN ts_timekeepers tk
                    ON s.source_tk_id = tk.ts_tk_id
                    AND trim(tk.tenant_id) = trim(:tid)
                WHERE trim(s.tenant_id) = trim(:tid)
                  AND s.source_client_id = :ts_cid
                ORDER BY s.slip_date DESC
                LIMIT 100
            """), {"tid": tenant_id, "ts_cid": ts_client_id})).mappings().fetchall()

        slips = [{
            "source_slip_id": str(r.source_slip_id or ""),
            "slip_date":      str(r.slip_date or ""),
            "source_tk_id":   str(r.source_tk_id or ""),
            "tk_name":        r.tk_name or "Unknown",
            "hours":          float(r.hours or 0),
            "value":          float(r.billed_value if r.billed else r.wip_value or 0),
            "rate":           float(r.rate or 0),
            "billed":         bool(r.billed),
            "narrative":      (r.narrative or ""),
        } for r in rows]

        return {"slips": slips, "ts_linked": True, "error": None}

    except Exception as exc:
        logger.error("get_billing_matter_recent_slips error: %s", exc)
        return {"slips": [], "error": str(exc)}
