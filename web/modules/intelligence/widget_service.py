"""
Intelligence Module — Widget Service Functions
===============================================

Three data_panel widgets surfaced on the billing landing page:

    ai_usage_my_matters       — user-scope; per-matter AI spend for matters
                                the current user has touched this month.
                                Row click drills to /billing/ai-usage/client/{id}
    ai_usage_firm             — firm-scope, partner-gated; today / month / top-N
                                matters / pending exceptions / top-N workflows
    ai_unallocated_review     — firm-scope, partner-gated; counts unallocated
                                and firm-overhead rows awaiting partner action

Each function takes a `scope` dict (standard widget signature) and returns
a dict the template consumes. No side effects — pure reads.

All queries respect tenant isolation via TRIM(tenant_id) = :tid per the
adapter pattern.

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text

from core.db.base import AsyncSessionLocal


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOP_N_DEFAULT = 5
RECENT_DAYS = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_float(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _month_bounds(today: Optional[date] = None) -> tuple[date, date]:
    today = today or date.today()
    start = today.replace(day=1)
    return start, today


# ---------------------------------------------------------------------------
# Widget: ai_usage_my_matters  (user scope)
# ---------------------------------------------------------------------------

async def get_my_matters_ai_usage(scope: dict) -> dict:
    """
    Per-matter AI spend for the current user's active matters, MTD and today.

    Scope requires:
        tenant_id   (required)
        user_id     (required; if absent, returns empty_state dict)
    """
    tenant_id = (scope.get("tenant_id") or "").strip()
    user_id = scope.get("user_id")

    if not tenant_id or not user_id:
        return {
            "empty_state": True,
            "reason": "User context required",
            "rows": [],
            "totals": {"today": 0.0, "month": 0.0},
        }

    start, today = _month_bounds()

    async with AsyncSessionLocal() as session:
        # Identify matters the user has touched this month — any AI call,
        # plus any matter they have a time_entry on (so a matter without
        # AI spend still shows up in "my matters")
        result = await session.execute(
            text("""
                WITH my_matters AS (
                    SELECT DISTINCT matter_id
                      FROM ai_api_calls
                     WHERE TRIM(tenant_id) = :tid
                       AND user_id = :uid
                       AND matter_id IS NOT NULL
                       AND created_at::date >= :mstart
                    UNION
                    SELECT DISTINCT matter_id
                      FROM time_entries
                     WHERE TRIM(tenant_id) = :tid
                       AND user_id = :uid
                       AND date >= :mstart
                )
                SELECT m.id::text          AS matter_id,
                       m.matter_name,
                       m.matter_number,
                       m.client_id::text   AS client_id,
                       COALESCE(SUM(
                           CASE WHEN a.created_at::date = :today
                                THEN a.cost_usd ELSE 0 END), 0) AS spend_today,
                       COALESCE(SUM(
                           CASE WHEN a.allocation_status != 'reallocated'
                                THEN a.cost_usd ELSE 0 END), 0) AS spend_month,
                       COALESCE(SUM(
                           CASE WHEN a.allocation_status = 'reallocated'
                                THEN a.cost_usd ELSE 0 END), 0) AS reallocated_month,
                       COUNT(a.id) FILTER (WHERE a.status = 'ok') AS call_count,
                       COUNT(a.id) FILTER
                           (WHERE a.status = 'ok'
                              AND a.request_metadata->>'fallback_used' = 'true'
                           ) AS fallback_count,
                       COUNT(a.id) FILTER
                           (WHERE a.status = 'ok'
                              AND a.request_metadata->>'override_used' = 'true'
                           ) AS override_count
                  FROM my_matters mm
                  JOIN matters m
                    ON m.id = mm.matter_id
                   AND TRIM(m.tenant_id) = :tid
                  LEFT JOIN ai_api_calls a
                    ON a.matter_id = m.id
                   AND TRIM(a.tenant_id) = :tid
                   AND a.created_at::date >= :mstart
                 GROUP BY m.id, m.matter_name, m.matter_number, m.client_id
                 ORDER BY spend_month DESC NULLS LAST, m.matter_name
                 LIMIT 50
            """),
            {"tid": tenant_id, "uid": user_id,
             "mstart": start, "today": today},
        )
        rows = result.mappings().all()

        # Pending exceptions on those matters
        exc_result = await session.execute(
            text("""
                SELECT matter_id::text AS matter_id, COUNT(*) AS n
                  FROM ai_cost_exceptions
                 WHERE TRIM(tenant_id) = :tid
                   AND disposition = 'pending'
                   AND matter_id IS NOT NULL
                 GROUP BY matter_id
            """),
            {"tid": tenant_id},
        )
        pending_by_matter = {r["matter_id"]: r["n"]
                             for r in exc_result.mappings().all()}

    rows_out: list[dict] = []
    total_today = 0.0
    total_month = 0.0
    for r in rows:
        today_v = _to_float(r["spend_today"])
        month_v = _to_float(r["spend_month"])
        total_today += today_v
        total_month += month_v
        rows_out.append({
            "matter_id":        r["matter_id"],
            "matter_name":      r["matter_name"],
            "matter_number":    r["matter_number"],
            "client_id":        r["client_id"],
            "spend_today":      today_v,
            "spend_month":      month_v,
            "reallocated_month": _to_float(r["reallocated_month"]),
            "call_count":       int(r["call_count"] or 0),
            "fallback_count":   int(r["fallback_count"] or 0),
            "override_count":   int(r["override_count"] or 0),
            "pending_exceptions": pending_by_matter.get(r["matter_id"], 0),
        })

    return {
        "empty_state": len(rows_out) == 0,
        "rows": rows_out,
        "totals": {"today": total_today, "month": total_month},
        "as_of": today.isoformat(),
        "period_label": f"{start.isoformat()} to {today.isoformat()}",
    }


# ---------------------------------------------------------------------------
# Widget: ai_usage_firm  (firm scope, partner-gated)
# ---------------------------------------------------------------------------

async def get_firm_ai_usage(scope: dict) -> dict:
    """
    Firm-wide AI spend summary.

    Scope:
        tenant_id   (required)
        client_id   (optional) — when present, all queries filter to matters
                    under this client. The widget title shifts from
                    "Firm AI Usage" to "Client AI Usage" client-side.

    Returns:
        scoped_to_client    — True when client_id filter is active
        today.total, today.calls, today.fallbacks, today.overrides
        month.total, month.calls
        top_matters[]       — top-N by MTD spend
        top_workflows[]     — top-N (module, purpose) pairs by MTD spend
        pending_exceptions  — count of disposition='pending' on ai_cost_exceptions
        unallocated.count   — calls with allocation_status in unallocated/firm_overhead
        unallocated.total_usd

    Note: when scoped to a client, unallocated/firm_overhead counts are
    necessarily zero for rows without matter_id — "client scope" means
    "calls tied to a matter under this client," which is the correct
    semantic for a client detail page.
    """
    tenant_id = (scope.get("tenant_id") or "").strip()
    if not tenant_id:
        return {"empty_state": True, "reason": "Tenant context required"}

    client_id = (scope.get("client_id") or "").strip() or None
    scoped = client_id is not None

    start, today = _month_bounds()

    # Scope subquery — resolved once, reused in every CTE below. When not
    # scoped this is a no-op CROSS JOIN; when scoped it filters ai_api_calls
    # down to the set of matter_ids belonging to this client.
    scope_params: dict = {"tid": tenant_id, "today": today, "mstart": start,
                          "n": TOP_N_DEFAULT}
    if scoped:
        scope_params["cid"] = client_id
        matter_filter = "AND matter_id IN (SELECT id FROM matters "                         "WHERE client_id = CAST(:cid AS uuid) "                         "AND TRIM(tenant_id) = :tid)"
    else:
        matter_filter = ""

    async with AsyncSessionLocal() as session:
        # Today totals
        r_today = await session.execute(
            text(f"""
                SELECT COALESCE(SUM(cost_usd), 0) AS total,
                       COUNT(*) FILTER (WHERE status = 'ok') AS calls,
                       COUNT(*) FILTER
                           (WHERE request_metadata->>'fallback_used' = 'true'
                           ) AS fallbacks,
                       COUNT(*) FILTER
                           (WHERE request_metadata->>'override_used' = 'true'
                           ) AS overrides
                  FROM ai_api_calls
                 WHERE TRIM(tenant_id) = :tid
                   AND created_at::date = :today
                   AND allocation_status != 'reallocated'
                   {matter_filter}
            """),
            scope_params,
        )
        row_today = r_today.mappings().first() or {}

        # Month totals
        r_month = await session.execute(
            text(f"""
                SELECT COALESCE(SUM(cost_usd), 0) AS total,
                       COUNT(*) FILTER (WHERE status = 'ok') AS calls
                  FROM ai_api_calls
                 WHERE TRIM(tenant_id) = :tid
                   AND created_at::date >= :mstart
                   AND allocation_status != 'reallocated'
                   {matter_filter}
            """),
            scope_params,
        )
        row_month = r_month.mappings().first() or {}

        # Top matters MTD (always inner-join matters, so client filter
        # drops through naturally)
        client_where = "AND m.client_id = CAST(:cid AS uuid)" if scoped else ""
        r_top = await session.execute(
            text(f"""
                SELECT m.id::text       AS matter_id,
                       m.matter_name,
                       m.matter_number,
                       m.client_id::text AS client_id,
                       SUM(a.cost_usd)  AS total
                  FROM ai_api_calls a
                  JOIN matters m ON m.id = a.matter_id
                                AND TRIM(m.tenant_id) = :tid
                 WHERE TRIM(a.tenant_id) = :tid
                   AND a.created_at::date >= :mstart
                   AND a.allocation_status = 'allocated'
                   {client_where}
                 GROUP BY m.id, m.matter_name, m.matter_number, m.client_id
                 ORDER BY total DESC
                 LIMIT :n
            """),
            scope_params,
        )
        top_matters = [
            {"matter_id":      r["matter_id"],
             "matter_name":    r["matter_name"],
             "matter_number":  r["matter_number"],
             "client_id":      r["client_id"],
             "spend_month":    _to_float(r["total"])}
            for r in r_top.mappings().all()
        ]

        # Top workflows
        r_wf = await session.execute(
            text(f"""
                SELECT module, purpose,
                       COUNT(*)          AS calls,
                       SUM(cost_usd)     AS total
                  FROM ai_api_calls
                 WHERE TRIM(tenant_id) = :tid
                   AND created_at::date >= :mstart
                   AND allocation_status != 'reallocated'
                   {matter_filter}
                 GROUP BY module, purpose
                 ORDER BY total DESC
                 LIMIT :n
            """),
            scope_params,
        )
        top_workflows = [
            {"module":  r["module"],
             "purpose": r["purpose"],
             "calls":   int(r["calls"] or 0),
             "total":   _to_float(r["total"])}
            for r in r_wf.mappings().all()
        ]

        # Pending exceptions — when scoped, join through matters
        if scoped:
            r_exc = await session.execute(
                text("""
                    SELECT COUNT(*) AS n
                      FROM ai_cost_exceptions e
                      JOIN matters m ON m.id = e.matter_id
                     WHERE TRIM(e.tenant_id) = :tid
                       AND e.disposition = 'pending'
                       AND m.client_id = CAST(:cid AS uuid)
                """),
                {"tid": tenant_id, "cid": client_id},
            )
        else:
            r_exc = await session.execute(
                text("""
                    SELECT COUNT(*) AS n
                      FROM ai_cost_exceptions
                     WHERE TRIM(tenant_id) = :tid
                       AND disposition = 'pending'
                """),
                {"tid": tenant_id},
            )
        pending_exceptions = int((r_exc.scalar() or 0))

        # Unallocated — when scoped, only matter-attached rows can participate
        r_una = await session.execute(
            text(f"""
                SELECT COUNT(*)               AS n,
                       COALESCE(SUM(cost_usd),0) AS total
                  FROM ai_api_calls
                 WHERE TRIM(tenant_id) = :tid
                   AND allocation_status IN ('unallocated', 'firm_overhead')
                   AND created_at::date >= :mstart
                   {matter_filter}
            """),
            scope_params,
        )
        row_una = r_una.mappings().first() or {}

    return {
        "empty_state": False,
        "scoped_to_client": scoped,
        "today": {
            "total":     _to_float(row_today.get("total")),
            "calls":     int(row_today.get("calls") or 0),
            "fallbacks": int(row_today.get("fallbacks") or 0),
            "overrides": int(row_today.get("overrides") or 0),
        },
        "month": {
            "total":     _to_float(row_month.get("total")),
            "calls":     int(row_month.get("calls") or 0),
        },
        "top_matters":        top_matters,
        "top_workflows":      top_workflows,
        "pending_exceptions": pending_exceptions,
        "unallocated": {
            "count":     int(row_una.get("n") or 0),
            "total_usd": _to_float(row_una.get("total")),
        },
        "as_of": today.isoformat(),
        "period_label": f"{start.isoformat()} to {today.isoformat()}",
    }


# ---------------------------------------------------------------------------
# Widget: ai_unallocated_review  (partner-gated summary card)
# ---------------------------------------------------------------------------

async def get_unallocated_review(scope: dict) -> dict:
    """
    Summary of AI calls awaiting partner allocation review.
    Three bucket counts by unallocated_reason:
        no_matter_context    — genuinely cross-matter, review for manual allocation
        policy_firm_overhead — firm-absorbed by policy (timesheet reconcile, etc.)
        matter_unavailable   — edge case (deleted/archived matter)
    """
    tenant_id = (scope.get("tenant_id") or "").strip()
    if not tenant_id:
        return {"empty_state": True, "reason": "Tenant context required"}

    start, today = _month_bounds()

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text("""
                SELECT unallocated_reason,
                       COUNT(*)              AS n,
                       COALESCE(SUM(cost_usd),0) AS total
                  FROM ai_api_calls
                 WHERE TRIM(tenant_id) = :tid
                   AND allocation_status IN ('unallocated', 'firm_overhead')
                   AND created_at::date >= :mstart
                 GROUP BY unallocated_reason
            """),
            {"tid": tenant_id, "mstart": start},
        )
        buckets = {
            "no_matter_context":    {"n": 0, "total": 0.0},
            "policy_firm_overhead": {"n": 0, "total": 0.0},
            "matter_unavailable":   {"n": 0, "total": 0.0},
        }
        for row in r.mappings().all():
            key = row["unallocated_reason"] or "no_matter_context"
            if key in buckets:
                buckets[key] = {
                    "n":     int(row["n"] or 0),
                    "total": _to_float(row["total"]),
                }

    total_n = sum(b["n"] for b in buckets.values())
    total_usd = sum(b["total"] for b in buckets.values())

    return {
        "empty_state": total_n == 0,
        "buckets": buckets,
        "total_count": total_n,
        "total_usd": total_usd,
        "as_of": today.isoformat(),
        "period_label": f"{start.isoformat()} to {today.isoformat()}",
    }
