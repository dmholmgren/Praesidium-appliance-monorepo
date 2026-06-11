"""Billing Dashboard — WIP by Matter + Receivables by Matter endpoints.

GET /api/v1/billing/wip-by-matter        -> top matters by unbilled WIP (from time_entries)
GET /api/v1/billing/receivables-by-matter -> top matters by outstanding AR
GET /api/v1/billing/ai-spend             -> AI API usage cost summary
GET /api/v1/billing/matter-client/{mid}  -> lookup client_id for a matter
"""
from __future__ import annotations
import logging
from datetime import date
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing", tags=["billing-dashboard-widgets"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()


@router.get("/matter-client/{matter_id}")
async def matter_client_lookup(matter_id: str, request: Request):
    """Quick lookup: matter_id -> client_id for navigation."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        row = await session.execute(sa_text("""
            SELECT m.client_id::text AS client_id
            FROM matters m
            WHERE m.id = :mid::uuid AND trim(m.tenant_id) = trim(:tid)
            LIMIT 1
        """), {"mid": matter_id, "tid": tid})
        r = row.mappings().fetchone()
        if r:
            return JSONResponse({"client_id": r["client_id"]})
    return JSONResponse({"client_id": None}, status_code=404)


@router.get("/wip-by-matter")
async def wip_by_matter(request: Request):
    """Top 20 matters by unbilled WIP from time_entries (draft status)."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        rows = await session.execute(sa_text("""
            SELECT
                m.id::text AS matter_id,
                m.matter_name,
                m.matter_number,
                c.id::text AS client_id,
                c.client_name,
                SUM(te.amount) AS wip_amount,
                SUM(te.hours) AS hours
            FROM time_entries te
            JOIN matters m ON te.matter_id = m.id
                AND trim(m.tenant_id) = trim(te.tenant_id)
            JOIN clients c ON m.client_id = c.id
                AND trim(c.tenant_id) = trim(m.tenant_id)
            WHERE trim(te.tenant_id) = trim(:tid)
              AND te.status = 'draft'
              AND te.amount > 0
            GROUP BY m.id, m.matter_name, m.matter_number, c.id, c.client_name
            ORDER BY wip_amount DESC
            LIMIT 20
        """), {"tid": tid})

        matters = []
        for r in rows.mappings():
            matters.append({
                "matter_id": r["matter_id"],
                "matter_name": r["matter_name"] or "Untitled",
                "matter_number": r.get("matter_number") or "",
                "client_id": r["client_id"],
                "client_name": r["client_name"] or "Unknown",
                "wip_amount": float(r["wip_amount"] or 0),
                "hours": round(float(r["hours"] or 0), 1),
            })

    return JSONResponse({"matters": matters})


@router.get("/receivables-by-matter")
async def receivables_by_matter(request: Request):
    """Top 20 clients by outstanding receivables.

    ts_invoices has no client column, so we join through ts_slips
    (which has source_client_id and invoice_num) to get client linkage.
    """
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        rows = await session.execute(sa_text("""
            WITH invoice_clients AS (
                SELECT DISTINCT ON (inv.invoice_num)
                    inv.invoice_num,
                    inv.net_due,
                    CURRENT_DATE - COALESCE(inv.slip_end, inv.created_at)::date AS age_days,
                    s.source_client_id
                FROM ts_invoices inv
                JOIN ts_slips s ON s.invoice_num = inv.invoice_num
                    AND trim(s.tenant_id) = trim(inv.tenant_id)
                WHERE trim(inv.tenant_id) = trim(:tid)
                  AND inv.paid_in_full = false
                  AND inv.net_due > 0
                ORDER BY inv.invoice_num, s.id
            ),
            client_ar AS (
                SELECT
                    ic.source_client_id,
                    COUNT(*) AS invoice_count,
                    SUM(ic.net_due) AS total_ar,
                    MAX(ic.age_days) AS max_age_days
                FROM invoice_clients ic
                GROUP BY ic.source_client_id
            ),
            client_resolved AS (
                SELECT
                    ca.source_client_id,
                    ca.invoice_count,
                    ca.total_ar,
                    ca.max_age_days,
                    tc.ts_name,
                    tc.praesidium_client_id,
                    c.id::text AS client_id,
                    c.client_name
                FROM client_ar ca
                JOIN ts_clients tc ON ca.source_client_id = tc.ts_client_id
                    AND trim(tc.tenant_id) = trim(:tid)
                LEFT JOIN clients c ON tc.praesidium_client_id IS NOT NULL
                    AND tc.praesidium_client_id::uuid = c.id
                    AND trim(c.tenant_id) = trim(:tid)
            )
            SELECT
                COALESCE(cr.client_id, cr.source_client_id) AS matter_id,
                COALESCE(cr.client_name, cr.ts_name, 'Unknown') AS matter_name,
                COALESCE(cr.client_id, '') AS client_id,
                COALESCE(cr.client_name, cr.ts_name, 'Unknown') AS client_name,
                cr.invoice_count,
                cr.total_ar,
                cr.max_age_days
            FROM client_resolved cr
            ORDER BY cr.total_ar DESC
            LIMIT 20
        """), {"tid": tid})

        matters = []
        for r in rows.mappings():
            matters.append({
                "matter_id": r["matter_id"],
                "matter_name": r["matter_name"] or "Unknown",
                "client_id": r["client_id"] or "",
                "client_name": r["client_name"] or "Unknown",
                "invoice_count": int(r["invoice_count"] or 0),
                "total_ar": float(r["total_ar"] or 0),
                "max_age_days": int(r["max_age_days"] or 0),
            })

    return JSONResponse({"matters": matters})


@router.get("/ai-spend")
async def ai_spend(request: Request):
    """AI spend: Claude API calls + Voyage embedding costs across all tables."""
    tid = _tid(request)
    today = date.today()
    mth_start = today.replace(day=1)

    async with AsyncSessionLocal() as session:
        try:
            row = await session.execute(sa_text("""
                WITH all_costs AS (
                    -- Claude API calls (ai_api_calls)
                    SELECT
                        'claude' AS source,
                        created_at,
                        COALESCE(cost_usd,
                            COALESCE(input_tokens, 0) * 0.000003 +
                            COALESCE(output_tokens, 0) * 0.000015
                        ) AS cost
                    FROM ai_api_calls
                    WHERE trim(tenant_id) = trim(:tid)
                       OR tenant_id = '' OR tenant_id IS NULL

                    UNION ALL

                    -- DMS embeddings (voyage-law-2)
                    SELECT
                        'voyage' AS source,
                        e.embedded_at AS created_at,
                        COALESCE(e.cost_usd,
                            COALESCE(ch.token_count, 0) * 0.00000012
                        ) AS cost
                    FROM dms_chunk_embeddings e
                    JOIN dms_chunks ch ON e.chunk_id = ch.id
                    WHERE trim(e.tenant_id) = trim(:tid)
                       OR e.tenant_id = '' OR e.tenant_id IS NULL

                    UNION ALL

                    -- Billing embeddings (voyage-law-2)
                    SELECT
                        'voyage' AS source,
                        e.embedded_at AS created_at,
                        COALESCE(e.cost_usd,
                            COALESCE(ch.token_count, 0) * 0.00000012
                        ) AS cost
                    FROM billing_chunk_embeddings e
                    JOIN billing_chunks ch ON e.chunk_id = ch.id
                    WHERE trim(e.tenant_id) = trim(:tid)
                       OR e.tenant_id = '' OR e.tenant_id IS NULL
                )
                SELECT
                    COUNT(*) AS total_calls,
                    COALESCE(SUM(cost), 0) AS total_cost,
                    COUNT(*) FILTER (WHERE created_at >= :mth) AS this_month_calls,
                    COALESCE(SUM(cost) FILTER (WHERE created_at >= :mth), 0) AS this_month_cost,
                    COUNT(*) FILTER (WHERE source = 'claude') AS claude_calls,
                    COALESCE(SUM(cost) FILTER (WHERE source = 'claude'), 0) AS claude_cost,
                    COUNT(*) FILTER (WHERE source = 'voyage') AS embed_calls,
                    COALESCE(SUM(cost) FILTER (WHERE source = 'voyage'), 0) AS embed_cost
                FROM all_costs
            """), {"tid": tid, "mth": mth_start})
            r = row.mappings().fetchone()
            if r:
                return JSONResponse({
                    "total_calls": int(r["total_calls"] or 0),
                    "total_cost": round(float(r["total_cost"] or 0), 2),
                    "this_month_calls": int(r["this_month_calls"] or 0),
                    "this_month_cost": round(float(r["this_month_cost"] or 0), 2),
                    "claude_calls": int(r["claude_calls"] or 0),
                    "claude_cost": round(float(r["claude_cost"] or 0), 2),
                    "embed_calls": int(r["embed_calls"] or 0),
                    "embed_cost": round(float(r["embed_cost"] or 0), 2),
                })
        except Exception as exc:
            logger.error("ai_spend error: %s", exc)

    return JSONResponse({
        "total_calls": 0, "total_cost": 0,
        "this_month_calls": 0, "this_month_cost": 0,
        "claude_calls": 0, "claude_cost": 0,
        "embed_calls": 0, "embed_cost": 0,
    })
