# ── Add this to modules/billing/api/views.py ────────────────────────────────
# Insert after the existing imports, before billing_root()

@views.get("/api/v1/billing/client-dashboard/{client_id}")
async def client_dashboard_api(request: Request, client_id: str):
    """
    Returns all data needed for the client billing dashboard in one call:
    - KPIs: WIP, AR, total billed, total collected
    - Per-matter WIP breakdown
    - Timekeeper allocation (all time + WIP only)
    - Open invoices with aging
    - Recent slips (last 50, from ts_slips via matter_number → ts_raw->>'nickname2')
    """
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal

    tenant_id = getattr(request.state, "tenant_id", "").strip()

    async with AsyncSessionLocal() as db:

        # ── Get client + matter numbers ───────────────────────────────────────
        matters_res = await db.execute(
            text("""
                SELECT id, matter_name, matter_number, status
                FROM matters
                WHERE client_id = :cid AND tenant_id = :tid
                ORDER BY matter_name
            """),
            {"cid": client_id, "tid": tenant_id}
        )
        matters = [dict(r._mapping) for r in matters_res.fetchall()]
        matter_numbers = [m["matter_number"] for m in matters if m.get("matter_number")]
        matter_by_num  = {m["matter_number"]: m for m in matters}

        if not matter_numbers:
            return {
                "wip_value": 0, "wip_hours": 0, "ar_balance": 0,
                "open_invoice_count": 0, "total_billed": 0, "total_slip_count": 0,
                "total_collected": 0, "payment_count": 0,
                "matter_wip": [], "tk_allocation": [],
                "open_invoices": [], "recent_slips": [],
            }

        # ── Get ts_client_ids for this client's matters via nickname2 ─────────
        ts_clients_res = await db.execute(
            text("""
                SELECT ts_client_id, ts_raw->>'nickname2' as nickname2
                FROM ts_clients
                WHERE tenant_id = :tid
                  AND ts_raw->>'nickname2' = ANY(:nums)
            """),
            {"tid": tenant_id, "nums": matter_numbers}
        )
        ts_rows = ts_clients_res.fetchall()
        ts_id_by_nn2 = {r.nickname2: str(r.ts_client_id) for r in ts_rows}
        ts_client_ids = list(ts_id_by_nn2.values())

        if not ts_client_ids:
            # No Timeslips match — return empty billing data
            return {
                "wip_value": 0, "wip_hours": 0, "ar_balance": 0,
                "open_invoice_count": 0, "total_billed": 0, "total_slip_count": 0,
                "total_collected": 0, "payment_count": 0,
                "matter_wip": [], "tk_allocation": [],
                "open_invoices": [], "recent_slips": [],
            }

        # ── WIP (unbilled slips) ──────────────────────────────────────────────
        wip_res = await db.execute(
            text("""
                SELECT
                    source_client_id,
                    SUM(hours)       as wip_hours,
                    SUM(wip_value)   as wip_value
                FROM ts_slips
                WHERE tenant_id = :tid
                  AND source_client_id = ANY(:ids)
                  AND billed = false
                GROUP BY source_client_id
            """),
            {"tid": tenant_id, "ids": ts_client_ids}
        )
        wip_rows = wip_res.fetchall()
        total_wip_hours = sum(float(r.wip_hours or 0) for r in wip_rows)
        total_wip_value = sum(float(r.wip_value or 0) for r in wip_rows)

        # Per-matter WIP
        matter_wip = []
        ts_id_to_matter = {}
        for nn2, ts_id in ts_id_by_nn2.items():
            m = matter_by_num.get(nn2)
            if m:
                ts_id_to_matter[ts_id] = m["id"]
        wip_by_cid = {str(r.source_client_id): r for r in wip_rows}
        for ts_id, matter_id in ts_id_to_matter.items():
            w = wip_by_cid.get(ts_id)
            matter_wip.append({
                "matter_id":  matter_id,
                "wip_hours":  float(w.wip_hours) if w else 0,
                "wip_value":  float(w.wip_value) if w else 0,
            })

        # ── Total billed (all time) ───────────────────────────────────────────
        billed_res = await db.execute(
            text("""
                SELECT
                    SUM(billed_value)  as total_billed,
                    COUNT(*)           as slip_count
                FROM ts_slips
                WHERE tenant_id = :tid
                  AND source_client_id = ANY(:ids)
                  AND billed = true
            """),
            {"tid": tenant_id, "ids": ts_client_ids}
        )
        billed_row    = billed_res.fetchone()
        total_billed  = float(billed_row.total_billed or 0)
        total_slips   = int(billed_row.slip_count or 0)

        # ── AR (open Timeslips invoices via slip join) ───────────────────────────
        # ts_invoices has no source_client_id — join through ts_slips invoice_num
        ar_res = await db.execute(
            text("""
                SELECT
                    ti.source_invoice_id,
                    ti.invoice_num,
                    ti.net_due       as balance_due,
                    ti.slip_start    as invoice_date
                FROM ts_invoices ti
                WHERE ti.tenant_id = :tid
                  AND ti.paid_in_full = false
                  AND EXISTS (
                      SELECT 1 FROM ts_slips s
                      WHERE s.tenant_id = ti.tenant_id
                        AND s.invoice_num = ti.invoice_num
                        AND s.source_client_id = ANY(:ids)
                  )
                ORDER BY ti.invoice_num DESC
                LIMIT 50
            """),
            {"tid": tenant_id, "ids": ts_client_ids}
        )
        ar_rows        = ar_res.fetchall()
        ar_balance     = sum(float(r.balance_due or 0) for r in ar_rows)
        open_inv_count = len(ar_rows)

        open_invoices = [
            {
                "invoice_num":  r.invoice_num,
                "balance_due":  float(r.balance_due or 0),
                "matter_name":  "—",
                "invoice_date": str(r.invoice_date) if r.invoice_date else None,
            }
            for r in ar_rows
        ]

        # ── Collected (Timeslips payments via invoice join) ──────────────────────
        pay_res = await db.execute(
            text("""
                SELECT
                    COUNT(*)    as pay_count,
                    SUM(amount) as total_collected
                FROM ts_payments tp
                WHERE tp.tenant_id = :tid
                  AND EXISTS (
                      SELECT 1 FROM ts_invoices ti
                      WHERE ti.tenant_id = tp.tenant_id
                        AND ti.invoice_num = tp.invoice_num
                        AND EXISTS (
                            SELECT 1 FROM ts_slips s
                            WHERE s.tenant_id = ti.tenant_id
                              AND s.invoice_num = ti.invoice_num
                              AND s.source_client_id = ANY(:ids)
                        )
                  )
            """),
            {"tid": tenant_id, "ids": ts_client_ids}
        )
        pay_row         = pay_res.fetchone()
        total_collected = abs(float(pay_row.total_collected or 0))
        payment_count   = int(pay_row.pay_count or 0)

        # ── Timekeeper allocation ─────────────────────────────────────────────
        tk_res = await db.execute(
            text("""
                SELECT
                    s.source_tk_id                                          as tk_id,
                    COALESCE(t.ts_name, s.source_tk_id)                    as tk_name,
                    SUM(s.hours)                                            as total_hours,
                    SUM(s.billed_value + s.wip_value)                      as total_value,
                    SUM(CASE WHEN s.billed=false THEN s.hours   ELSE 0 END) as wip_hours,
                    SUM(CASE WHEN s.billed=false THEN s.wip_value ELSE 0 END) as wip_value,
                    SUM(CASE WHEN s.billed=true  THEN s.hours   ELSE 0 END) as billed_hours
                FROM ts_slips s
                LEFT JOIN ts_timekeepers t
                    ON t.ts_tk_id = s.source_tk_id AND t.tenant_id = s.tenant_id
                WHERE s.tenant_id = :tid
                  AND s.source_client_id = ANY(:ids)
                GROUP BY s.source_tk_id, t.ts_name
                ORDER BY total_hours DESC
            """),
            {"tid": tenant_id, "ids": ts_client_ids}
        )
        tk_allocation = [
            {
                "tk_id":        r.tk_id,
                "tk_name":      r.tk_name,
                "total_hours":  float(r.total_hours or 0),
                "total_value":  float(r.total_value or 0),
                "wip_hours":    float(r.wip_hours or 0),
                "wip_value":    float(r.wip_value or 0),
                "billed_hours": float(r.billed_hours or 0),
            }
            for r in tk_res.fetchall()
        ]

        # ── Recent slips (last 50) ────────────────────────────────────────────
        slips_res = await db.execute(
            text("""
                SELECT
                    s.source_slip_id,
                    s.slip_date,
                    s.source_client_id,
                    s.source_tk_id,
                    COALESCE(t.ts_name, s.source_tk_id) as tk_name,
                    s.hours,
                    s.wip_value,
                    s.billed_value,
                    s.billed,
                    s.narrative,
                    tc.ts_raw->>'nickname2'              as nickname2
                FROM ts_slips s
                LEFT JOIN ts_timekeepers t
                    ON t.ts_tk_id = s.source_tk_id AND t.tenant_id = s.tenant_id
                LEFT JOIN ts_clients tc
                    ON tc.ts_client_id::text = s.source_client_id
                    AND tc.tenant_id = s.tenant_id
                WHERE s.tenant_id = :tid
                  AND s.source_client_id = ANY(:ids)
                ORDER BY s.slip_date DESC
                LIMIT 50
            """),
            {"tid": tenant_id, "ids": ts_client_ids}
        )
        nn2_to_matter = {nn2: matter_by_num.get(nn2, {}) for nn2 in ts_id_by_nn2}
        recent_slips = []
        for r in slips_res.fetchall():
            m = matter_by_num.get(r.nickname2, {})
            recent_slips.append({
                "slip_date":     str(r.slip_date) if r.slip_date else "",
                "matter_id":     m.get("id"),
                "matter_name":   m.get("matter_name", "—"),
                "source_tk_id":  r.source_tk_id,
                "tk_name":       r.tk_name,
                "hours":         float(r.hours or 0),
                "value":         float(r.billed_value if r.billed else r.wip_value or 0),
                "billed":        bool(r.billed),
                "narrative":     r.narrative or "",
            })

    from fastapi.responses import JSONResponse
    return JSONResponse({
        "wip_value":         total_wip_value,
        "wip_hours":         total_wip_hours,
        "ar_balance":        ar_balance,
        "open_invoice_count": open_inv_count,
        "total_billed":      total_billed,
        "total_slip_count":  total_slips,
        "total_collected":   total_collected,
        "payment_count":     payment_count,
        "matter_wip":        matter_wip,
        "tk_allocation":     tk_allocation,
        "open_invoices":     open_invoices,
        "recent_slips":      recent_slips,
    })
