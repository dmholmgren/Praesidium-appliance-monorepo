"""
Praesidium — UI Wiring Pass 1 (AI Usage + Nav cleanup)
=======================================================
Idempotent deployment script, run inside praesidium-web.

Tasks:
  1. Extend get_firm_ai_usage(scope) to accept optional client_id filter.
     Adds a client-scoped view without duplicating the widget. Queries
     all join against matters.client_id when client_id is present.
  2. billing_overview.html — split the Bill State row into two columns:
     Bill State (flex:1) + ai_usage_firm (280px fixed).
  3. client_detail.html — change back button href /billing/clients -> /billing.
  4. client_detail.html — expand Row 2 from 2 cols (Matters + TK Alloc) to
     3 cols (Matters + TK Alloc + ai_usage_firm?client_id=...).
  5. base.html — remove the Clients left-nav entry (and its SVG and text).

Every edit is a string replace with a sentinel check so re-running the
script is safe. Each file gets a .bak-uiwiring backup on first run.
"""

from __future__ import annotations
import shutil
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PATHS = {
    "widget_service":    Path("/app/modules/intelligence/widget_service.py"),
    "billing_overview":  Path("/app/modules/billing/templates/billing/billing_overview.html"),
    "client_detail":     Path("/app/modules/billing/templates/billing/client_detail.html"),
    "base_html":         Path("/app/core/templates/base.html"),
}


def backup(p: Path) -> None:
    bak = p.with_suffix(p.suffix + ".bak-uiwiring")
    if not bak.exists():
        shutil.copy2(p, bak)
        print(f"  backup -> {bak}")


def replace_once(p: Path, before: str, after: str, *, marker: str | None = None,
                 required: bool = True) -> bool:
    """
    Apply a single str_replace. If `marker` is provided and already in the
    file, skip (idempotent). Returns True if the file changed.
    """
    src = p.read_text()
    if marker and marker in src:
        print(f"  SKIP  {p.name}: marker already present")
        return False
    if before not in src:
        if required:
            print(f"  ERROR {p.name}: before-block not found")
            raise SystemExit(1)
        print(f"  SKIP  {p.name}: before-block not found (not required)")
        return False
    backup(p)
    p.write_text(src.replace(before, after, 1))
    print(f"  OK    {p.name}")
    return True


# ---------------------------------------------------------------------------
# TASK 1  — Extend get_firm_ai_usage to accept client_id scope
# ---------------------------------------------------------------------------
def task1_widget_service():
    print("\n[1/5] widget_service.py — add client_id scope to get_firm_ai_usage")
    p = PATHS["widget_service"]

    before = '''async def get_firm_ai_usage(scope: dict) -> dict:
    """
    Firm-wide AI spend summary.

    Returns:
        today.total, today.call_count, today.fallbacks, today.overrides
        month.total, month.call_count
        top_matters[]       — top-N by MTD spend
        top_workflows[]     — top-N (module, purpose) pairs by MTD spend
        pending_exceptions  — count of disposition='pending' on ai_cost_exceptions
        unallocated.count   — calls with allocation_status in unallocated/firm_overhead
        unallocated.total_usd
    """
    tenant_id = (scope.get("tenant_id") or "").strip()
    if not tenant_id:
        return {"empty_state": True, "reason": "Tenant context required"}

    start, today = _month_bounds()

    async with AsyncSessionLocal() as session:
        # Today totals
        r_today = await session.execute(
            text("""
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
            """),
            {"tid": tenant_id, "today": today},
        )
        row_today = r_today.mappings().first() or {}

        # Month totals
        r_month = await session.execute(
            text("""
                SELECT COALESCE(SUM(cost_usd), 0) AS total,
                       COUNT(*) FILTER (WHERE status = 'ok') AS calls
                  FROM ai_api_calls
                 WHERE TRIM(tenant_id) = :tid
                   AND created_at::date >= :mstart
                   AND allocation_status != 'reallocated'
            """),
            {"tid": tenant_id, "mstart": start},
        )
        row_month = r_month.mappings().first() or {}

        # Top matters MTD
        r_top = await session.execute(
            text("""
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
                 GROUP BY m.id, m.matter_name, m.matter_number, m.client_id
                 ORDER BY total DESC
                 LIMIT :n
            """),
            {"tid": tenant_id, "mstart": start, "n": TOP_N_DEFAULT},
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
            text("""
                SELECT module, purpose,
                       COUNT(*)          AS calls,
                       SUM(cost_usd)     AS total
                  FROM ai_api_calls
                 WHERE TRIM(tenant_id) = :tid
                   AND created_at::date >= :mstart
                   AND allocation_status != 'reallocated'
                 GROUP BY module, purpose
                 ORDER BY total DESC
                 LIMIT :n
            """),
            {"tid": tenant_id, "mstart": start, "n": TOP_N_DEFAULT},
        )
        top_workflows = [
            {"module":  r["module"],
             "purpose": r["purpose"],
             "calls":   int(r["calls"] or 0),
             "total":   _to_float(r["total"])}
            for r in r_wf.mappings().all()
        ]

        # Pending exceptions
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

        # Unallocated
        r_una = await session.execute(
            text("""
                SELECT COUNT(*)               AS n,
                       COALESCE(SUM(cost_usd),0) AS total
                  FROM ai_api_calls
                 WHERE TRIM(tenant_id) = :tid
                   AND allocation_status IN ('unallocated', 'firm_overhead')
                   AND created_at::date >= :mstart
            """),
            {"tid": tenant_id, "mstart": start},
        )
        row_una = r_una.mappings().first() or {}

    return {
        "empty_state": False,
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
    }'''

    after = '''async def get_firm_ai_usage(scope: dict) -> dict:
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
        matter_filter = "AND matter_id IN (SELECT id FROM matters " \
                        "WHERE client_id = CAST(:cid AS uuid) " \
                        "AND TRIM(tenant_id) = :tid)"
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
    }'''

    replace_once(p, before, after, marker='"scoped_to_client":')


# ---------------------------------------------------------------------------
# TASK 2  — billing_overview: split Bill State row into 2 cols + AI usage
# ---------------------------------------------------------------------------
def task2_billing_overview():
    print("\n[2/5] billing_overview.html — split Bill State row, add AI usage")
    p = PATHS["billing_overview"]

    before = '''      {# Bill state summary strip #}
      <div style="flex-shrink:0; height:88px;
                  border:1px solid var(--border-color,#e2e8f0); border-radius:8px;
                  background:var(--surface,#fff); overflow:hidden;
                  display:flex; flex-direction:column;">

        <div style="padding:5px 12px; border-bottom:1px solid #f1f5f9; background:#f8fafc; flex-shrink:0;">
          <span style="font-size:10px; font-weight:700; color:var(--muted);
                       font-size:11px; font-weight:600;">Bill State</span>
        </div>

        <div hx-get="/widgets/billing_bill_state_summary"
             hx-trigger="load"
             hx-swap="innerHTML"
             id="widget-billing-bill-state"
             style="flex:1; display:flex; align-items:center;">
          <div style="padding:12px; text-align:center; color:var(--muted); font-size:12px; width:100%;">Loading…</div>
        </div>
      </div>

    </div>'''

    after = '''      {# Bill state row — two columns: bill state (flex:1) + firm AI usage (280px) #}
      <div style="flex-shrink:0; height:88px; display:flex; gap:10px;">

        {# Bill state summary strip #}
        <div style="flex:1; min-width:0;
                    border:1px solid var(--border-color,#e2e8f0); border-radius:8px;
                    background:var(--surface,#fff); overflow:hidden;
                    display:flex; flex-direction:column;">

          <div style="padding:5px 12px; border-bottom:1px solid #f1f5f9; background:#f8fafc; flex-shrink:0;">
            <span style="font-size:10px; font-weight:700; color:var(--muted);
                         font-size:11px; font-weight:600;">Bill State</span>
          </div>

          <div hx-get="/widgets/billing_bill_state_summary"
               hx-trigger="load"
               hx-swap="innerHTML"
               id="widget-billing-bill-state"
               style="flex:1; display:flex; align-items:center;">
            <div style="padding:12px; text-align:center; color:var(--muted); font-size:12px; width:100%;">Loading…</div>
          </div>
        </div>

        {# Firm AI usage — strip format, fixed 280px to align with right column #}
        <div style="width:280px; flex-shrink:0;
                    border:1px solid var(--border-color,#e2e8f0); border-radius:8px;
                    background:var(--surface,#fff); overflow:hidden;
                    display:flex; flex-direction:column;">

          <div hx-get="/widgets/ai_usage_firm"
               hx-trigger="load"
               hx-swap="innerHTML"
               id="widget-billing-ai-usage-firm"
               style="flex:1; min-height:0; overflow-y:auto;">
            <div style="padding:12px; text-align:center; color:var(--muted); font-size:12px;">Loading…</div>
          </div>
        </div>
      </div>

    </div>'''

    replace_once(p, before, after,
                 marker='widget-billing-ai-usage-firm')


# ---------------------------------------------------------------------------
# TASK 3 + 4  — client_detail.html: back button + Row 2 expansion
# ---------------------------------------------------------------------------
def task34_client_detail():
    print("\n[3/5] client_detail.html — back button /billing/clients -> /billing")
    p = PATHS["client_detail"]

    replace_once(
        p,
        '<a href="/billing/clients" style="font-size:12px; color:var(--muted); text-decoration:none;">← Clients</a>',
        '<a href="/billing" style="font-size:12px; color:var(--muted); text-decoration:none;">← Billing</a>',
        marker='← Billing</a>',
        required=False,
    )

    print("\n[4/5] client_detail.html — expand Row 2 with AI usage column")

    before = '''{# ── Row 2: Matter Table + Timekeeper Allocation ──────────────────────────────── #}
<div style="display:grid; grid-template-columns:1fr 300px; gap:16px; margin-bottom:16px;">

  {# Matter Table — billing_matter_table widget #}
  <div class="card" style="padding:0; overflow:hidden;">
    <div style="padding:12px 16px; border-bottom:1px solid var(--border);
                display:flex; justify-content:space-between; align-items:center;">
      <span style="font-size:13px; font-weight:600;">
        Matters
        <span id="matter-count" style="font-weight:400; color:var(--muted);"></span>
      </span>
      <div style="display:flex; gap:6px;">
        <button onclick="setMF('active')" id="mf-active"
                style="font-size:11px; padding:2px 10px; border-radius:4px;
                       border:1px solid var(--primary); background:var(--primary);
                       color:#fff; cursor:pointer;">Active</button>
        <button onclick="setMF('all')" id="mf-all"
                style="font-size:11px; padding:2px 10px; border-radius:4px;
                       border:1px solid var(--border); background:#fff;
                       color:var(--muted); cursor:pointer;">All</button>
      </div>
    </div>
    <div id="widget-matter-table"
         hx-get="/widgets/billing_matter_table?client_id={{ client.id }}"
         hx-trigger="load"
         hx-swap="innerHTML"
         hx-on::after-swap="updateMatterCount()">
      <div style="padding:30px; text-align:center; color:var(--muted); font-size:12px;">Loading…</div>
    </div>
  </div>

  {# Timekeeper Allocation — billing_timekeeper_allocation widget #}
  <div class="card" style="padding:0; overflow:hidden;">
    <div style="padding:12px 16px; border-bottom:1px solid var(--border);">
      <div style="font-size:13px; font-weight:600;">Timekeeper Allocation</div>
      <div style="font-size:11px; color:var(--muted); margin-top:2px;">All time on this client</div>
    </div>
    <div id="widget-tk-allocation"
         hx-get="/widgets/billing_timekeeper_allocation?client_id={{ client.id }}"
         hx-trigger="load"
         hx-swap="innerHTML">
      <div style="padding:30px; text-align:center; color:var(--muted); font-size:12px;">Loading…</div>
    </div>
  </div>
</div>'''

    after = '''{# ── Row 2: Matter Table + Timekeeper Allocation + Client AI Usage ────────── #}
<div style="display:grid; grid-template-columns:1fr 260px 260px; gap:16px; margin-bottom:16px;">

  {# Matter Table — billing_matter_table widget #}
  <div class="card" style="padding:0; overflow:hidden;">
    <div style="padding:12px 16px; border-bottom:1px solid var(--border);
                display:flex; justify-content:space-between; align-items:center;">
      <span style="font-size:13px; font-weight:600;">
        Matters
        <span id="matter-count" style="font-weight:400; color:var(--muted);"></span>
      </span>
      <div style="display:flex; gap:6px;">
        <button onclick="setMF('active')" id="mf-active"
                style="font-size:11px; padding:2px 10px; border-radius:4px;
                       border:1px solid var(--primary); background:var(--primary);
                       color:#fff; cursor:pointer;">Active</button>
        <button onclick="setMF('all')" id="mf-all"
                style="font-size:11px; padding:2px 10px; border-radius:4px;
                       border:1px solid var(--border); background:#fff;
                       color:var(--muted); cursor:pointer;">All</button>
      </div>
    </div>
    <div id="widget-matter-table"
         hx-get="/widgets/billing_matter_table?client_id={{ client.id }}"
         hx-trigger="load"
         hx-swap="innerHTML"
         hx-on::after-swap="updateMatterCount()">
      <div style="padding:30px; text-align:center; color:var(--muted); font-size:12px;">Loading…</div>
    </div>
  </div>

  {# Timekeeper Allocation — billing_timekeeper_allocation widget #}
  <div class="card" style="padding:0; overflow:hidden;">
    <div style="padding:12px 16px; border-bottom:1px solid var(--border);">
      <div style="font-size:13px; font-weight:600;">Timekeeper Allocation</div>
      <div style="font-size:11px; color:var(--muted); margin-top:2px;">All time on this client</div>
    </div>
    <div id="widget-tk-allocation"
         hx-get="/widgets/billing_timekeeper_allocation?client_id={{ client.id }}"
         hx-trigger="load"
         hx-swap="innerHTML">
      <div style="padding:30px; text-align:center; color:var(--muted); font-size:12px;">Loading…</div>
    </div>
  </div>

  {# Client-scoped AI Usage — ai_usage_firm widget filtered to this client #}
  <div class="card" style="padding:0; overflow:hidden;">
    <div id="widget-client-ai-usage"
         hx-get="/widgets/ai_usage_firm?client_id={{ client.id }}"
         hx-trigger="load"
         hx-swap="innerHTML">
      <div style="padding:30px; text-align:center; color:var(--muted); font-size:12px;">Loading…</div>
    </div>
  </div>
</div>'''

    replace_once(p, before, after, marker='widget-client-ai-usage')


# ---------------------------------------------------------------------------
# TASK 5  — base.html: remove Clients nav entry
# ---------------------------------------------------------------------------
def task5_base_nav():
    print("\n[5/5] base.html — remove Clients left-nav link")
    p = PATHS["base_html"]

    # The full Clients anchor including SVG and text. Pulled from the
    # live base.html at line ~382.
    before = '''      <a href="/billing/clients" class="nav-link {% if page == 'clients' %}active{% endif %}">
        <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
        Clients
      </a>
'''
    after = ''

    replace_once(p, before, after,
                 marker="{# Clients nav link removed — drill-down via "
                        "billing home or firm_matter_tree #}",
                 required=False)
    # Second pass: if we removed it, drop a comment sentinel so re-runs skip.
    # Done at the insertion anchor — the Timesheets link is stable.
    src = p.read_text()
    sentinel = ("{# Clients nav link removed — drill-down via "
                "billing home or firm_matter_tree #}")
    anchor = ('''      <a href="/billing/timesheet" class="nav-link {% if page == 'timesheets' %}active{% endif %}">''')
    if sentinel not in src and anchor in src:
        # Insert sentinel comment right before the Timesheets link (its sibling)
        src = src.replace(anchor, f"{sentinel}\n{anchor}", 1)
        p.write_text(src)
        print(f"  SENTINEL inserted in {p.name}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def main():
    print("Praesidium — UI Wiring Pass 1")
    print("=" * 60)
    for p in PATHS.values():
        if not p.exists():
            print(f"MISSING: {p}")
            raise SystemExit(1)

    task1_widget_service()
    task2_billing_overview()
    task34_client_detail()
    task5_base_nav()

    # Syntax-check the widget_service change
    import ast
    try:
        ast.parse(PATHS["widget_service"].read_text())
        print("\n  widget_service.py parses clean")
    except SyntaxError as e:
        print(f"\n  SYNTAX ERROR in widget_service.py: {e}")
        raise SystemExit(1)

    print("\nAll tasks applied. Restart praesidium-web:")
    print("  docker restart praesidium-web")


if __name__ == "__main__":
    main()
