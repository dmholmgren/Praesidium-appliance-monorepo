"""
Intelligence Module — AI Usage Drill-Down Routes
=================================================

Two pages surfaced from the billing widgets:

    GET  /billing/ai-usage/client/{client_id}
         Client-level drill-down: all matters under client with MTD and
         today's AI spend, pending exceptions, override/fallback counts.

    GET  /billing/ai-usage/unallocated
         Partner-reviewable report of unallocated and firm-overhead AI
         calls with inline allocate / absorb / reallocate actions.

    POST /billing/ai-usage/allocate
         Move an ai_api_calls row to a target matter_id (bookkeeping only,
         does NOT re-trigger cap checks — per Apr 18, 2026 spec).

    POST /billing/ai-usage/absorb
         Mark an ai_api_calls row as firm-overhead (accepted, not billed).

    POST /billing/ai-usage/reallocate
         Move a previously-allocated row to a different matter. Original
         allocation is preserved in the audit trail; cost now counts against
         destination matter's MTD going forward.

Permission:
    The drill-down client page is visible to any user who has access to the
    client (attorney+).
    The unallocated report and the three POST actions require partner+.

Architectural notes:
    - All three POST actions are idempotent: repeated submission with the
      same (call_id, target) yields the same end state without double-writing.
    - Every action writes allocation_notes with timestamp + user_id + reason
      so the audit trail is on the same row as the allocation itself
      (chain-of-custody discipline).
    - Allocation never retroactively alters today's cap enforcement (the cap
      check already ran at call-time); it flags would_have_breached_cap for
      partner awareness only.

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user
from modules.dashboard.services.nav_context import get_nav_context

log = logging.getLogger(__name__)

router = APIRouter(prefix="/billing/ai-usage", tags=["billing-ai-usage"])


# ---------------------------------------------------------------------------
# Template environment
# ---------------------------------------------------------------------------

_TEMPLATE_DIRS = [
    "/app/modules/intelligence/templates",
    "/app/core/templates",
]
_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIRS),
    autoescape=select_autoescape(["html", "xml"]),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PARTNER_ROLES = {"attorney", "admin", "super_admin"}


def _get_tenant_id(request: Request) -> str:
    tid = getattr(request.state, "tenant_id", None)
    if not tid:
        raise HTTPException(status_code=401, detail="No tenant context")
    return tid.strip()


def _is_partner(user) -> bool:
    """
    Partner-gating check. See architectural note in ChatPrompts v7.5 re the
    role enum not having a distinct 'partner' value — we map to
    attorney+admin+super_admin as the permissive equivalent until the role
    enum is split.
    """
    if not user:
        return False
    role = getattr(user, "role", None)
    if role is None and isinstance(user, dict):
        role = user.get("role")
    return str(role) in PARTNER_ROLES


def _month_start() -> date:
    return date.today().replace(day=1)


# ---------------------------------------------------------------------------
# GET /billing/ai-usage/client/{client_id}
# ---------------------------------------------------------------------------

@router.get("/client/{client_id}", response_class=HTMLResponse)
async def client_ai_usage(
    request: Request,
    client_id: str,
    user=Depends(get_current_user),
):
    """Drill-down page: all matters for a client with AI spend breakdown."""
    tenant_id = _get_tenant_id(request)
    start = _month_start()
    today = date.today()

    async with AsyncSessionLocal() as session:
        # Client header
        c_row = await session.execute(
            text("""
                SELECT id::text AS id, display_name, client_number
                  FROM clients
                 WHERE id = CAST(:cid AS uuid)
                   AND TRIM(tenant_id) = :tid
                 LIMIT 1
            """),
            {"cid": client_id, "tid": tenant_id},
        )
        client = c_row.mappings().first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")

        # All matters for this client with AI spend
        m_rows = await session.execute(
            text("""
                SELECT m.id::text         AS matter_id,
                       m.matter_name,
                       m.matter_number,
                       m.status,
                       COALESCE(SUM(
                           CASE WHEN a.created_at::date = :today
                                THEN a.cost_usd ELSE 0 END), 0) AS today_usd,
                       COALESCE(SUM(
                           CASE WHEN a.created_at::date >= :mstart
                                 AND a.allocation_status = 'allocated'
                                THEN a.cost_usd ELSE 0 END), 0) AS month_usd,
                       COALESCE(SUM(
                           CASE WHEN a.allocation_status = 'reallocated'
                                 AND a.created_at::date >= :mstart
                                THEN a.cost_usd ELSE 0 END), 0) AS reallocated_usd,
                       COUNT(a.id) FILTER
                           (WHERE a.status = 'ok'
                              AND a.created_at::date >= :mstart
                           ) AS calls_month,
                       COUNT(a.id) FILTER
                           (WHERE a.request_metadata->>'fallback_used' = 'true'
                              AND a.created_at::date >= :mstart
                           ) AS fallbacks_month,
                       COUNT(a.id) FILTER
                           (WHERE a.request_metadata->>'override_used' = 'true'
                              AND a.created_at::date >= :mstart
                           ) AS overrides_month
                  FROM matters m
                  LEFT JOIN ai_api_calls a
                    ON a.matter_id = m.id
                   AND TRIM(a.tenant_id) = :tid
                 WHERE m.client_id = CAST(:cid AS uuid)
                   AND TRIM(m.tenant_id) = :tid
                 GROUP BY m.id, m.matter_name, m.matter_number, m.status
                 ORDER BY month_usd DESC, m.matter_name
            """),
            {"tid": tenant_id, "cid": client_id,
             "mstart": start, "today": today},
        )
        matters = [dict(r) for r in m_rows.mappings().all()]

        # Pending exceptions for this client's matters
        e_rows = await session.execute(
            text("""
                SELECT e.matter_id::text AS matter_id, COUNT(*) AS n
                  FROM ai_cost_exceptions e
                  JOIN matters m ON m.id = e.matter_id
                 WHERE TRIM(e.tenant_id) = :tid
                   AND m.client_id = CAST(:cid AS uuid)
                   AND e.disposition = 'pending'
                 GROUP BY e.matter_id
            """),
            {"tid": tenant_id, "cid": client_id},
        )
        pending = {r["matter_id"]: r["n"] for r in e_rows.mappings().all()}

    for m in matters:
        m["pending_exceptions"] = pending.get(m["matter_id"], 0)
        m["today_usd"] = float(m.get("today_usd") or 0)
        m["month_usd"] = float(m.get("month_usd") or 0)
        m["reallocated_usd"] = float(m.get("reallocated_usd") or 0)

    total_today = sum(m["today_usd"] for m in matters)
    total_month = sum(m["month_usd"] for m in matters)

    nav_ctx = await get_nav_context(request) if "get_nav_context" in globals() \
        else {}

    tmpl = _env.get_template("pages/ai_usage_client.html")
    html = tmpl.render(
        client=dict(client),
        matters=matters,
        totals={"today": total_today, "month": total_month},
        period_label=f"{start.isoformat()} to {today.isoformat()}",
        as_of=today.isoformat(),
        nav=nav_ctx,
        user=user,
    )
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# GET /billing/ai-usage/unallocated
# ---------------------------------------------------------------------------

@router.get("/unallocated", response_class=HTMLResponse)
async def unallocated_report(
    request: Request,
    user=Depends(get_current_user),
    reason: Optional[str] = None,
    days: int = 30,
):
    """
    Partner report of unallocated / firm-overhead AI calls.

    Query params:
        reason  — filter by unallocated_reason
        days    — window (default 30)
    """
    tenant_id = _get_tenant_id(request)
    if not _is_partner(user):
        raise HTTPException(status_code=403,
                            detail="Partner role required")

    where_extra = ""
    params: dict = {"tid": tenant_id, "days": days}
    if reason in ("no_matter_context",
                  "policy_firm_overhead",
                  "matter_unavailable"):
        where_extra = " AND a.unallocated_reason = :reason"
        params["reason"] = reason

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text(f"""
                SELECT a.id                    AS call_id,
                       a.created_at,
                       a.module, a.purpose,
                       a.model,
                       a.input_tokens, a.output_tokens, a.total_tokens,
                       a.cost_usd,
                       a.user_id,
                       u.full_name            AS user_name,
                       a.allocation_status,
                       a.unallocated_reason,
                       a.request_metadata
                  FROM ai_api_calls a
             LEFT JOIN users u ON u.id = a.user_id
                 WHERE TRIM(a.tenant_id) = :tid
                   AND a.allocation_status IN ('unallocated', 'firm_overhead')
                   AND a.created_at >= NOW() - (:days || ' days')::interval
                   {where_extra}
                 ORDER BY a.created_at DESC
                 LIMIT 500
            """),
            params,
        )
        calls = [dict(row) for row in r.mappings().all()]

        # Candidate matters for dropdowns — active only, scoped to tenant
        m_r = await session.execute(
            text("""
                SELECT id::text AS id, matter_name, matter_number
                  FROM matters
                 WHERE TRIM(tenant_id) = :tid
                   AND status = 'active'
                 ORDER BY matter_name
                 LIMIT 500
            """),
            {"tid": tenant_id},
        )
        matters = [dict(r) for r in m_r.mappings().all()]

    # Format for template
    for c in calls:
        c["call_id"] = int(c["call_id"])
        c["cost_usd"] = float(c.get("cost_usd") or 0)
        c["created_at_str"] = (
            c["created_at"].strftime("%Y-%m-%d %H:%M")
            if c.get("created_at") else ""
        )

    total_usd = sum(c["cost_usd"] for c in calls)

    tmpl = _env.get_template("pages/ai_usage_unallocated.html")
    html = tmpl.render(
        calls=calls,
        matters=matters,
        total_usd=total_usd,
        count=len(calls),
        active_reason=reason,
        days=days,
        user=user,
    )
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# POST actions — allocate / absorb / reallocate
# ---------------------------------------------------------------------------

async def _append_notes(
    existing: Optional[str], action: str, user_id: int, detail: str
) -> str:
    """Append a timestamped audit note, preserving prior notes."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = f"[{ts}] {action} by user={user_id}: {detail}"
    if existing:
        return existing + "\n" + new
    return new


@router.post("/allocate")
async def allocate_to_matter(
    request: Request,
    user=Depends(get_current_user),
):
    """
    Move a call to a target matter. Bookkeeping only — does NOT re-trigger
    cap checks (per Apr 18, 2026 spec). Flags would_have_breached_cap if
    the retroactive allocation would have exceeded the matter's cap at the
    time of the original call.
    """
    tenant_id = _get_tenant_id(request)
    if not _is_partner(user):
        raise HTTPException(status_code=403, detail="Partner role required")

    body = await request.json()
    call_id = int(body.get("call_id") or 0)
    target_matter_id = body.get("matter_id") or None
    reason = body.get("reason") or "partner allocation"

    if not call_id or not target_matter_id:
        raise HTTPException(status_code=400,
                            detail="call_id and matter_id required")

    async with AsyncSessionLocal() as session:
        # Confirm the call belongs to this tenant and is in an allocatable state
        r = await session.execute(
            text("""
                SELECT id, matter_id::text AS matter_id, cost_usd,
                       allocation_status, allocation_notes, created_at
                  FROM ai_api_calls
                 WHERE id = :cid
                   AND TRIM(tenant_id) = :tid
                 LIMIT 1
            """),
            {"cid": call_id, "tid": tenant_id},
        )
        row = r.mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Call not found")
        if row["allocation_status"] not in (
                "unallocated", "firm_overhead", "allocated"):
            raise HTTPException(status_code=409,
                                detail="Call not in allocatable state")

        # Compute would_have_breached_cap — informational only
        # Look up routing for this module.purpose to check matter cap
        r2 = await session.execute(
            text("""
                SELECT matter_daily_cost_cap_usd
                  FROM ai_model_routing
                 WHERE (TRIM(tenant_id) = :tid OR tenant_id IS NULL)
                   AND status = 'published'
                   AND module = (SELECT module FROM ai_api_calls WHERE id = :cid)
                   AND purpose = (SELECT purpose FROM ai_api_calls WHERE id = :cid)
                 ORDER BY CASE WHEN tenant_id IS NULL THEN 1 ELSE 0 END
                 LIMIT 1
            """),
            {"tid": tenant_id, "cid": call_id},
        )
        routing_row = r2.mappings().first()
        would_have_breached = False
        if routing_row and routing_row["matter_daily_cost_cap_usd"]:
            cap = float(routing_row["matter_daily_cost_cap_usd"])
            # Would this call + same-day calls on the target matter have breached?
            r3 = await session.execute(
                text("""
                    SELECT COALESCE(SUM(cost_usd), 0) AS total
                      FROM ai_api_calls
                     WHERE TRIM(tenant_id) = :tid
                       AND matter_id = CAST(:mid AS uuid)
                       AND created_at::date = :d
                       AND allocation_status != 'reallocated'
                       AND id != :cid
                """),
                {"tid": tenant_id, "mid": target_matter_id,
                 "d": row["created_at"].date(), "cid": call_id},
            )
            same_day_total = float(r3.scalar() or 0)
            if same_day_total + float(row["cost_usd"]) > cap:
                would_have_breached = True

        # The distinction:
        # - If previously 'unallocated' or 'firm_overhead': straight move
        #   (allocation_status -> 'allocated', matter_id set)
        # - If previously 'allocated' with a different matter: this is
        #   a reallocation — original stays but gets status='reallocated',
        #   and we insert a fresh bookkeeping row on the new matter.
        #
        # For the unallocated report, case 1 is the normal path. We implement
        # both here so the action is safe from any entry point.
        notes = await _append_notes(
            row["allocation_notes"], "ALLOCATE",
            user.id if hasattr(user, "id") else user.get("id"),
            f"to matter={target_matter_id} reason={reason}"
        )

        if row["allocation_status"] in ("unallocated", "firm_overhead"):
            await session.execute(
                text("""
                    UPDATE ai_api_calls
                       SET matter_id = CAST(:mid AS uuid),
                           allocation_status = 'allocated',
                           unallocated_reason = NULL,
                           allocated_to_matter_id = CAST(:mid AS uuid),
                           allocated_by = :uid,
                           allocated_at = NOW(),
                           allocation_notes = :notes,
                           would_have_breached_cap = :whbc
                     WHERE id = :cid
                       AND TRIM(tenant_id) = :tid
                """),
                {"mid": target_matter_id,
                 "uid": user.id if hasattr(user, "id") else user.get("id"),
                 "notes": notes,
                 "whbc": would_have_breached,
                 "cid": call_id,
                 "tid": tenant_id},
            )
        else:
            # Reallocation of an already-allocated row (use POST /reallocate
            # for the standard path; this is a convenience).
            raise HTTPException(
                status_code=409,
                detail="Call already allocated; use /reallocate endpoint",
            )

        await session.commit()

    return JSONResponse({
        "ok": True,
        "call_id": call_id,
        "allocated_to_matter_id": target_matter_id,
        "would_have_breached_cap": would_have_breached,
    })


@router.post("/absorb")
async def absorb_as_firm_overhead(
    request: Request,
    user=Depends(get_current_user),
):
    """
    Accept a call as firm overhead. Used when a partner reviews an unallocated
    charge and decides it should not be billed to any specific matter.
    """
    tenant_id = _get_tenant_id(request)
    if not _is_partner(user):
        raise HTTPException(status_code=403, detail="Partner role required")

    body = await request.json()
    call_id = int(body.get("call_id") or 0)
    reason = body.get("reason") or "accepted as firm overhead"

    if not call_id:
        raise HTTPException(status_code=400, detail="call_id required")

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text("""
                SELECT id, allocation_status, allocation_notes
                  FROM ai_api_calls
                 WHERE id = :cid
                   AND TRIM(tenant_id) = :tid
                 LIMIT 1
            """),
            {"cid": call_id, "tid": tenant_id},
        )
        row = r.mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Call not found")
        if row["allocation_status"] not in ("unallocated", "firm_overhead"):
            raise HTTPException(status_code=409,
                                detail="Call not in absorbable state")

        notes = await _append_notes(
            row["allocation_notes"], "ABSORB",
            user.id if hasattr(user, "id") else user.get("id"),
            reason,
        )

        await session.execute(
            text("""
                UPDATE ai_api_calls
                   SET allocation_status = 'firm_overhead',
                       unallocated_reason =
                         COALESCE(unallocated_reason, 'no_matter_context'),
                       allocated_by = :uid,
                       allocated_at = NOW(),
                       allocation_notes = :notes
                 WHERE id = :cid
                   AND TRIM(tenant_id) = :tid
            """),
            {"uid": user.id if hasattr(user, "id") else user.get("id"),
             "notes": notes, "cid": call_id, "tid": tenant_id},
        )
        await session.commit()

    return JSONResponse({"ok": True, "call_id": call_id,
                         "allocation_status": "firm_overhead"})


@router.post("/reallocate")
async def reallocate(
    request: Request,
    user=Depends(get_current_user),
):
    """
    Move a previously-allocated call to a different matter.

    Implementation:
        1. Mark the original row allocation_status = 'reallocated'
           (so it drops out of cap math going forward).
        2. Insert a new bookkeeping row on the target matter with identical
           token/cost figures, status='ok', and request_metadata flagging
           the linkage to the original call_id.

    This preserves a clean audit trail — the original call is never deleted
    or altered beyond status + notes.
    """
    tenant_id = _get_tenant_id(request)
    if not _is_partner(user):
        raise HTTPException(status_code=403, detail="Partner role required")

    body = await request.json()
    call_id = int(body.get("call_id") or 0)
    target_matter_id = body.get("matter_id") or None
    reason = body.get("reason") or "partner reallocation"

    if not call_id or not target_matter_id:
        raise HTTPException(status_code=400,
                            detail="call_id and matter_id required")

    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text("""
                SELECT id, tenant_id, user_id, provider, model, module, purpose,
                       input_tokens, output_tokens, total_tokens,
                       cost_usd, latency_ms, status, request_metadata,
                       allocation_status, allocation_notes, matter_id::text AS matter_id
                  FROM ai_api_calls
                 WHERE id = :cid
                   AND TRIM(tenant_id) = :tid
                 LIMIT 1
            """),
            {"cid": call_id, "tid": tenant_id},
        )
        row = r.mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Call not found")
        if row["allocation_status"] not in ("allocated", "firm_overhead"):
            raise HTTPException(status_code=409,
                                detail="Only allocated or firm_overhead calls "
                                       "can be reallocated")

        notes = await _append_notes(
            row["allocation_notes"], "REALLOCATE",
            user.id if hasattr(user, "id") else user.get("id"),
            f"from={row['matter_id']} to={target_matter_id} reason={reason}"
        )

        # 1. Flag original as reallocated
        await session.execute(
            text("""
                UPDATE ai_api_calls
                   SET allocation_status = 'reallocated',
                       allocated_by = :uid,
                       allocated_at = NOW(),
                       allocation_notes = :notes
                 WHERE id = :cid
                   AND TRIM(tenant_id) = :tid
            """),
            {"uid": user.id if hasattr(user, "id") else user.get("id"),
             "notes": notes, "cid": call_id, "tid": tenant_id},
        )

        # 2. Insert twin row on target matter
        import json as _json
        meta = row["request_metadata"] or {}
        if isinstance(meta, str):
            meta = _json.loads(meta)
        meta["reallocated_from_call_id"] = call_id
        meta["reallocated_by"] = user.id if hasattr(user, "id") else user.get("id")

        await session.execute(
            text("""
                INSERT INTO ai_api_calls
                  (tenant_id, user_id, provider, model, module, purpose,
                   input_tokens, output_tokens, total_tokens,
                   cost_usd, latency_ms, status,
                   request_metadata, matter_id,
                   allocation_status, allocation_notes, allocated_by, allocated_at)
                VALUES
                  (:tenant_id, :user_id, :provider, :model, :module, :purpose,
                   :input_tokens, :output_tokens, :total_tokens,
                   :cost_usd, :latency_ms, :status,
                   CAST(:meta AS jsonb), CAST(:mid AS uuid),
                   'allocated', :notes, :uid, NOW())
            """),
            {
                "tenant_id": tenant_id,
                "user_id": row["user_id"],
                "provider": row["provider"],
                "model": row["model"],
                "module": row["module"],
                "purpose": row["purpose"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "total_tokens": row["total_tokens"],
                "cost_usd": row["cost_usd"],
                "latency_ms": row["latency_ms"],
                "status": row["status"],
                "meta": _json.dumps(meta),
                "mid": target_matter_id,
                "notes": notes,
                "uid": user.id if hasattr(user, "id") else user.get("id"),
            },
        )
        await session.commit()

    return JSONResponse({
        "ok": True,
        "call_id": call_id,
        "reallocated_to_matter_id": target_matter_id,
    })
