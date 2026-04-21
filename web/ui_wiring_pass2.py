"""
Praesidium — UI Wiring Pass 2
==============================
Idempotent deployment script, run inside praesidium-web.

Tasks:
  1. Rewrite ai_usage_firm.html — restore shaded title bar header,
     simplified body (title up top, MTD / today totals horizontal in body).
  2. Rewrite ai_usage_my_matters.html — same style, same header pattern.
  3. Rewrite ai_unallocated_review.html — same style, same header pattern.
  4. Create two new compact AI widgets for client detail:
       ai_usage_client_mtd  — client MTD total + call count, horizontal
       ai_usage_client_today — client today total + call count, horizontal
     Both use existing get_firm_ai_usage service (scoped with client_id).
  5. Register the two new widgets in widget_registry.
  6. Edit client_detail.html:
       - Expand KPI row from 4 to 6 columns with gap + slightly taller boxes
       - Add ai_usage_client_mtd + ai_usage_client_today as cols 5 & 6
       - Revert Row 2 back to 2 columns (Matters + Timekeeper Allocation)
         since AI usage is now in the KPI row instead

Dates subordinate: every widget carries "Month to date" or "Today" as a
sub-label under its title, not as a separate section.

Idempotent — re-run is safe. .bak-uiwiring2 backups on first run per file.
"""

from __future__ import annotations
import shutil
import asyncio
from pathlib import Path


HOST = False  # run inside container; paths are /app/...
ROOT = Path("/app")

FILES = {
    "firm":             ROOT / "modules/intelligence/templates/ai_usage_firm.html",
    "my_matters":       ROOT / "modules/intelligence/templates/ai_usage_my_matters.html",
    "unallocated":      ROOT / "modules/intelligence/templates/ai_unallocated_review.html",
    "client_mtd":       ROOT / "modules/intelligence/templates/ai_usage_client_mtd.html",
    "client_today":     ROOT / "modules/intelligence/templates/ai_usage_client_today.html",
    "client_detail":    ROOT / "modules/billing/templates/billing/client_detail.html",
    "widget_service":   ROOT / "modules/intelligence/widget_service.py",
}


def backup(p: Path) -> None:
    bak = p.with_suffix(p.suffix + ".bak-uiwiring2")
    if p.exists() and not bak.exists():
        shutil.copy2(p, bak)
        print(f"  backup -> {bak.name}")


def write_template(p: Path, content: str) -> None:
    backup(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    print(f"  OK    wrote {p.name}")


def replace_once(p: Path, before: str, after: str, *,
                 marker: str | None = None, required: bool = True) -> bool:
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
    print(f"  OK    patched {p.name}")
    return True


# ---------------------------------------------------------------------------
# Widget template: ai_usage_firm — shaded title bar + horizontal body numbers
# ---------------------------------------------------------------------------

FIRM_TEMPLATE = """{# widgets/ai_usage_firm.html
   Firm AI Usage — shaded header matching native billing widgets.
   Auto-refresh every 60s. No manual resync.
   Data source: modules.intelligence.widget_service.get_firm_ai_usage
#}
<div style="display:flex; flex-direction:column; height:100%; min-height:0;"
     id="widget-ai-usage-firm-body"
     hx-get="/widgets/ai_usage_firm{% if scoped_to_client and (top_matters and top_matters[0].client_id) %}?client_id={{ top_matters[0].client_id }}{% endif %}"
     hx-trigger="every 60s"
     hx-swap="outerHTML">

  {# Shaded title bar #}
  <div style="padding:6px 12px; border-bottom:1px solid #f1f5f9;
              background:#f8fafc; flex-shrink:0;
              display:flex; align-items:center; justify-content:space-between;">
    <span style="font-size:11px; font-weight:600; color:var(--muted);">
      {% if scoped_to_client %}Client AI Usage{% else %}Firm AI Usage{% endif %}
    </span>
  </div>

  {% if empty_state %}
  <div style="flex:1; display:flex; align-items:center; justify-content:center;
              padding:16px; text-align:center;">
    <div style="font-size:10px; color:var(--muted);">
      {{ reason or 'No AI usage yet.' }}
    </div>
  </div>

  {% else %}

  {# Body — horizontal numbers row #}
  <div style="padding:8px 12px; flex-shrink:0;
              display:flex; align-items:baseline; gap:16px;">
    <div>
      <div style="font-size:9px; font-weight:700; color:var(--muted);
                  text-transform:uppercase; letter-spacing:0.05em;">
        Month to date
      </div>
      <div style="font-size:15px; font-weight:700; color:var(--text);
                  font-variant-numeric:tabular-nums; line-height:1.2;">
        ${{ "{:,.2f}".format(month.total) }}
      </div>
      <div style="font-size:9px; color:var(--muted);">
        {{ month.calls }} call{{ 's' if month.calls != 1 else '' }}
      </div>
    </div>
    <div>
      <div style="font-size:9px; font-weight:700; color:var(--muted);
                  text-transform:uppercase; letter-spacing:0.05em;">
        Today
      </div>
      <div style="font-size:15px; font-weight:700; color:var(--text);
                  font-variant-numeric:tabular-nums; line-height:1.2;">
        ${{ "{:,.4f}".format(today.total) }}
      </div>
      <div style="font-size:9px; color:var(--muted);">
        {{ today.calls }} call{{ 's' if today.calls != 1 else '' }}
      </div>
    </div>
    {% if today.fallbacks > 0 or today.overrides > 0 %}
    <div style="margin-left:auto; display:flex; gap:8px; font-size:10px;">
      {% if today.fallbacks > 0 %}
      <span style="color:#1d4ed8; font-weight:600;" title="Fallbacks today">
        ↓{{ today.fallbacks }}
      </span>
      {% endif %}
      {% if today.overrides > 0 %}
      <span style="color:#dc2626; font-weight:600;" title="Overrides today">
        !{{ today.overrides }}
      </span>
      {% endif %}
    </div>
    {% endif %}
  </div>

  {# Top matters — hidden when client-scoped (redundant on client page) #}
  {% if top_matters and not scoped_to_client %}
  <div style="flex:1; min-height:0; overflow-y:auto; border-top:1px solid #f1f5f9;">
    <div style="padding:5px 12px 3px; font-size:9px; font-weight:700;
                color:var(--muted); text-transform:uppercase;
                letter-spacing:0.05em;">
      Top Matters
    </div>
    {% for m in top_matters[:5] %}
    <a href="/billing/ai-usage/client/{{ m.client_id }}"
       style="display:flex; align-items:center; justify-content:space-between;
              padding:4px 12px; text-decoration:none; border-bottom:1px solid #f8fafc;"
       onmouseover="this.style.background='#f8fafc'"
       onmouseout="this.style.background=''">
      <div style="flex:1; min-width:0; padding-right:8px;">
        <div style="font-size:11px; color:var(--text);
                    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
          {{ m.matter_name }}
        </div>
      </div>
      <div style="font-size:11px; font-weight:600; color:var(--text);
                  font-variant-numeric:tabular-nums; flex-shrink:0;">
        ${{ "{:,.2f}".format(m.spend_month) }}
      </div>
    </a>
    {% endfor %}
  </div>
  {% endif %}

  {# Top workflows #}
  {% if top_workflows %}
  <div style="{% if not top_matters or scoped_to_client %}flex:1; min-height:0; overflow-y:auto;{% else %}flex-shrink:0;{% endif %}
              border-top:1px solid #f1f5f9;">
    <div style="padding:5px 12px 3px; font-size:9px; font-weight:700;
                color:var(--muted); text-transform:uppercase;
                letter-spacing:0.05em;">
      Top Workflows
    </div>
    {% for w in top_workflows[:5] %}
    <div style="display:flex; align-items:center; justify-content:space-between;
                padding:4px 12px; border-bottom:1px solid #f8fafc;">
      <div style="flex:1; min-width:0; padding-right:8px;">
        <div style="font-size:11px; color:var(--text);
                    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
          {{ w.module }}.{{ w.purpose }}
        </div>
      </div>
      <div style="font-size:11px; font-weight:600; color:var(--text);
                  font-variant-numeric:tabular-nums; flex-shrink:0;">
        ${{ "{:,.2f}".format(w.total) }}
        <span style="color:var(--muted); font-weight:400; font-size:9px;">
          ({{ w.calls }})
        </span>
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  {# Footer — unallocated + exceptions #}
  {% if unallocated.count > 0 or pending_exceptions > 0 %}
  <div style="padding:5px 12px; flex-shrink:0; border-top:1px solid #f1f5f9;
              display:flex; align-items:center; justify-content:space-between;
              font-size:10px;">
    <div>
      {% if unallocated.count > 0 %}
      <a href="/billing/ai-usage/unallocated"
         style="color:#b45309; text-decoration:none; font-weight:600;"
         onmouseover="this.style.textDecoration='underline'"
         onmouseout="this.style.textDecoration='none'">
        {{ unallocated.count }} unallocated · ${{ "{:,.2f}".format(unallocated.total_usd) }}
      </a>
      {% endif %}
    </div>
    {% if pending_exceptions > 0 %}
    <a href="/billing/ai-usage/unallocated"
       style="color:#dc2626; text-decoration:none; font-weight:600;"
       title="Pending exceptions">
      {{ pending_exceptions }} pending
    </a>
    {% endif %}
  </div>
  {% endif %}

  {% endif %}
</div>
"""


# ---------------------------------------------------------------------------
# Widget template: ai_usage_my_matters — shaded title bar
# ---------------------------------------------------------------------------

MY_MATTERS_TEMPLATE = """{# widgets/ai_usage_my_matters.html
   My AI Usage — shaded header matching native widgets.
   Data source: modules.intelligence.widget_service.get_my_matters_ai_usage
#}
<div style="display:flex; flex-direction:column; height:100%; min-height:0;"
     id="widget-ai-usage-my-matters-body"
     hx-get="/widgets/ai_usage_my_matters"
     hx-trigger="every 60s"
     hx-swap="outerHTML">

  <div style="padding:6px 12px; border-bottom:1px solid #f1f5f9;
              background:#f8fafc; flex-shrink:0;">
    <span style="font-size:11px; font-weight:600; color:var(--muted);">
      My AI Usage
    </span>
  </div>

  {% if empty_state %}
  <div style="flex:1; display:flex; align-items:center; justify-content:center;
              padding:16px; text-align:center;">
    <div style="font-size:10px; color:var(--muted);">
      {{ reason or 'No AI usage yet this month.' }}
    </div>
  </div>

  {% else %}

  {# Totals row — horizontal #}
  <div style="padding:8px 12px; flex-shrink:0;
              display:flex; align-items:baseline; gap:16px;">
    <div>
      <div style="font-size:9px; font-weight:700; color:var(--muted);
                  text-transform:uppercase; letter-spacing:0.05em;">
        Month to date
      </div>
      <div style="font-size:15px; font-weight:700; color:var(--text);
                  font-variant-numeric:tabular-nums; line-height:1.2;">
        ${{ "{:,.2f}".format(totals.month) }}
      </div>
    </div>
    <div>
      <div style="font-size:9px; font-weight:700; color:var(--muted);
                  text-transform:uppercase; letter-spacing:0.05em;">
        Today
      </div>
      <div style="font-size:15px; font-weight:700; color:var(--text);
                  font-variant-numeric:tabular-nums; line-height:1.2;">
        ${{ "{:,.4f}".format(totals.today) }}
      </div>
    </div>
  </div>

  <div style="flex:1; min-height:0; overflow-y:auto; border-top:1px solid #f1f5f9;">
    <div style="padding:5px 12px 3px; font-size:9px; font-weight:700;
                color:var(--muted); text-transform:uppercase;
                letter-spacing:0.05em;">
      By Matter
    </div>
    {% for row in rows %}
    <a href="/billing/ai-usage/client/{{ row.client_id }}"
       style="display:flex; align-items:center; justify-content:space-between;
              padding:5px 12px; text-decoration:none; border-bottom:1px solid #f8fafc;"
       onmouseover="this.style.background='#f8fafc'"
       onmouseout="this.style.background=''">
      <div style="flex:1; min-width:0; padding-right:8px;">
        <div style="font-size:11px; color:var(--text);
                    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
          {{ row.matter_name }}
        </div>
        <div style="font-size:9px; color:var(--muted);">
          {{ row.matter_number }}
          {% if row.call_count %} · {{ row.call_count }} call{{ 's' if row.call_count != 1 else '' }}{% endif %}
        </div>
      </div>
      <div style="text-align:right; flex-shrink:0;">
        <div style="font-size:11px; font-weight:600; color:var(--text);
                    font-variant-numeric:tabular-nums;">
          ${{ "{:,.2f}".format(row.spend_month) }}
        </div>
        <div style="font-size:9px; color:var(--muted);">
          {% if row.pending_exceptions > 0 %}
          <span style="color:#b45309;" title="Pending exceptions">
            ⚠ {{ row.pending_exceptions }}
          </span>
          {% elif row.override_count > 0 %}
          <span style="color:#dc2626;" title="Overrides">
            !{{ row.override_count }}
          </span>
          {% elif row.fallback_count > 0 %}
          <span style="color:#1d4ed8;" title="Fallbacks">
            ↓{{ row.fallback_count }}
          </span>
          {% endif %}
        </div>
      </div>
    </a>
    {% endfor %}
  </div>

  {% endif %}
</div>
"""


# ---------------------------------------------------------------------------
# Widget template: ai_unallocated_review — shaded title bar
# ---------------------------------------------------------------------------

UNALLOCATED_TEMPLATE = """{# widgets/ai_unallocated_review.html
   Unallocated AI Charges — shaded header matching native widgets.
   Data source: modules.intelligence.widget_service.get_unallocated_review
#}
<div style="display:flex; flex-direction:column; height:100%; min-height:0;"
     id="widget-ai-unallocated-review-body"
     hx-get="/widgets/ai_unallocated_review"
     hx-trigger="every 60s"
     hx-swap="outerHTML">

  <div style="padding:6px 12px; border-bottom:1px solid #f1f5f9;
              background:#f8fafc; flex-shrink:0;
              display:flex; align-items:center; justify-content:space-between;">
    <span style="font-size:11px; font-weight:600; color:var(--muted);">
      Unallocated AI
    </span>
    {% if not empty_state %}
    <a href="/billing/ai-usage/unallocated"
       style="font-size:9px; color:var(--primary,#1B2A4A); text-decoration:none; font-weight:600;">
      Review →
    </a>
    {% endif %}
  </div>

  {% if empty_state %}
  <div style="flex:1; display:flex; align-items:center; justify-content:center;
              padding:16px; text-align:center; flex-direction:column;">
    <div style="font-size:22px; margin-bottom:4px;">✓</div>
    <div style="font-size:10px; color:var(--muted);">
      {{ reason or 'Nothing awaiting review.' }}
    </div>
  </div>

  {% else %}

  <div style="padding:8px 12px; flex-shrink:0;
              display:flex; align-items:baseline; gap:16px;">
    <div>
      <div style="font-size:9px; font-weight:700; color:var(--muted);
                  text-transform:uppercase; letter-spacing:0.05em;">
        Month to date
      </div>
      <div style="font-size:15px; font-weight:700; color:var(--text);
                  font-variant-numeric:tabular-nums; line-height:1.2;">
        ${{ "{:,.2f}".format(total_usd) }}
      </div>
      <div style="font-size:9px; color:var(--muted);">
        {{ total_count }} call{{ 's' if total_count != 1 else '' }}
      </div>
    </div>
  </div>

  <div style="flex:1; min-height:0; overflow-y:auto; border-top:1px solid #f1f5f9;">
    {% if buckets.no_matter_context.n > 0 %}
    <div style="display:flex; align-items:center; justify-content:space-between;
                padding:5px 12px; border-bottom:1px solid #f8fafc;">
      <div style="flex:1; min-width:0; padding-right:8px;">
        <div style="font-size:11px; color:var(--text);">No matter context</div>
        <div style="font-size:9px; color:var(--muted);">Cross-matter calls</div>
      </div>
      <div style="text-align:right; flex-shrink:0;">
        <div style="font-size:11px; font-weight:600; color:var(--text);
                    font-variant-numeric:tabular-nums;">
          ${{ "{:,.2f}".format(buckets.no_matter_context.total) }}
        </div>
        <div style="font-size:9px; color:var(--muted);">
          {{ buckets.no_matter_context.n }} call{{ 's' if buckets.no_matter_context.n != 1 else '' }}
        </div>
      </div>
    </div>
    {% endif %}
    {% if buckets.policy_firm_overhead.n > 0 %}
    <div style="display:flex; align-items:center; justify-content:space-between;
                padding:5px 12px; border-bottom:1px solid #f8fafc;">
      <div style="flex:1; min-width:0; padding-right:8px;">
        <div style="font-size:11px; color:var(--text);">Firm overhead</div>
        <div style="font-size:9px; color:var(--muted);">Non-billable by policy</div>
      </div>
      <div style="text-align:right; flex-shrink:0;">
        <div style="font-size:11px; font-weight:600; color:var(--text);
                    font-variant-numeric:tabular-nums;">
          ${{ "{:,.2f}".format(buckets.policy_firm_overhead.total) }}
        </div>
        <div style="font-size:9px; color:var(--muted);">
          {{ buckets.policy_firm_overhead.n }} call{{ 's' if buckets.policy_firm_overhead.n != 1 else '' }}
        </div>
      </div>
    </div>
    {% endif %}
    {% if buckets.matter_unavailable.n > 0 %}
    <div style="display:flex; align-items:center; justify-content:space-between;
                padding:5px 12px; border-bottom:1px solid #f8fafc;
                background:#fffbeb;">
      <div style="flex:1; min-width:0; padding-right:8px;">
        <div style="font-size:11px; color:var(--text);">Matter unavailable</div>
        <div style="font-size:9px; color:#b45309;">Needs attention</div>
      </div>
      <div style="text-align:right; flex-shrink:0;">
        <div style="font-size:11px; font-weight:600; color:var(--text);
                    font-variant-numeric:tabular-nums;">
          ${{ "{:,.2f}".format(buckets.matter_unavailable.total) }}
        </div>
        <div style="font-size:9px; color:var(--muted);">
          {{ buckets.matter_unavailable.n }} call{{ 's' if buckets.matter_unavailable.n != 1 else '' }}
        </div>
      </div>
    </div>
    {% endif %}
  </div>

  {% endif %}
</div>
"""


# ---------------------------------------------------------------------------
# New widgets: ai_usage_client_mtd + ai_usage_client_today
# These fit the KPI card format exactly — same padding + height as the
# existing billing_client_kpi cards. Each reads from get_firm_ai_usage
# with client_id scope, but renders just one of (month | today) horizontally.
# ---------------------------------------------------------------------------

CLIENT_MTD_TEMPLATE = """{# widgets/ai_usage_client_mtd.html
   Compact client-scoped AI MTD widget — fits KPI card slot.
   Data source: modules.intelligence.widget_service.get_firm_ai_usage
                (called with client_id scope)
#}
<div class="card" style="padding:14px; min-height:80px;
                         border-radius:8px; background:var(--surface,#fff);
                         border:1px solid var(--border,#e2e8f0);
                         display:flex; flex-direction:column;
                         justify-content:space-between;">
  <div>
    <div style="font-size:10px; font-weight:600; color:var(--muted);
                text-transform:uppercase; letter-spacing:0.05em;">
      AI Usage (MTD)
    </div>
    <div style="font-size:9px; color:var(--muted); margin-top:1px;">
      Month to date
    </div>
  </div>
  <div style="display:flex; align-items:baseline; gap:10px;">
    <div style="font-size:17px; font-weight:700; color:var(--text);
                font-variant-numeric:tabular-nums; line-height:1;">
      {% if empty_state %}$0.00{% else %}${{ "{:,.2f}".format(month.total) }}{% endif %}
    </div>
    <div style="font-size:10px; color:var(--muted);">
      {% if empty_state %}0 calls{% else %}{{ month.calls }} call{{ 's' if month.calls != 1 else '' }}{% endif %}
    </div>
  </div>
</div>
"""

CLIENT_TODAY_TEMPLATE = """{# widgets/ai_usage_client_today.html
   Compact client-scoped AI Today widget — fits KPI card slot.
#}
<div class="card" style="padding:14px; min-height:80px;
                         border-radius:8px; background:var(--surface,#fff);
                         border:1px solid var(--border,#e2e8f0);
                         display:flex; flex-direction:column;
                         justify-content:space-between;">
  <div>
    <div style="font-size:10px; font-weight:600; color:var(--muted);
                text-transform:uppercase; letter-spacing:0.05em;">
      AI Usage (Today)
    </div>
    <div style="font-size:9px; color:var(--muted); margin-top:1px;">
      Today
    </div>
  </div>
  <div style="display:flex; align-items:baseline; gap:10px;">
    <div style="font-size:17px; font-weight:700; color:var(--text);
                font-variant-numeric:tabular-nums; line-height:1;">
      {% if empty_state %}$0.0000{% else %}${{ "{:,.4f}".format(today.total) }}{% endif %}
    </div>
    <div style="font-size:10px; color:var(--muted);">
      {% if empty_state %}0 calls{% else %}{{ today.calls }} call{{ 's' if today.calls != 1 else '' }}{% endif %}
    </div>
  </div>
</div>
"""


# ---------------------------------------------------------------------------
# TASK 1-3  Widget template rewrites
# ---------------------------------------------------------------------------
def task_rewrite_widgets():
    print("\n[1/6] Rewrite ai_usage_firm.html")
    write_template(FILES["firm"], FIRM_TEMPLATE)

    print("\n[2/6] Rewrite ai_usage_my_matters.html")
    write_template(FILES["my_matters"], MY_MATTERS_TEMPLATE)

    print("\n[3/6] Rewrite ai_unallocated_review.html")
    write_template(FILES["unallocated"], UNALLOCATED_TEMPLATE)


# ---------------------------------------------------------------------------
# TASK 4  New compact widgets for client detail KPI row
# ---------------------------------------------------------------------------
def task_new_client_widgets():
    print("\n[4/6] Create ai_usage_client_mtd + ai_usage_client_today templates")
    write_template(FILES["client_mtd"], CLIENT_MTD_TEMPLATE)
    write_template(FILES["client_today"], CLIENT_TODAY_TEMPLATE)


# ---------------------------------------------------------------------------
# TASK 5  Register new widgets in widget_registry
# ---------------------------------------------------------------------------
def task_register_widgets():
    print("\n[5/6] Register ai_usage_client_mtd / _today in widget_registry")
    # Deferred import so the script can be syntax-checked without app context
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal

    async def _register():
        async with AsyncSessionLocal() as session:
            # Both new widgets reuse the existing firm_ai_usage service fn
            # with client_id scope. render_template differs.
            await session.execute(text("""
                INSERT INTO widget_registry
                    (tenant_id, widget_slug, widget_name, category, widget_type,
                     data_source, render_template, target_route,
                     source_platform, default_size, resizable,
                     permission_level, feature_flag, config_schema, is_platform_standard)
                VALUES
                    (NULL, 'ai_usage_client_mtd', 'AI Usage (MTD)', 'billing', 'data_panel',
                     'modules.intelligence.widget_service.get_firm_ai_usage',
                     'ai_usage_client_mtd.html',
                     NULL,
                     'praesidium', 'small', FALSE,
                     'attorney', NULL,
                     '{}'::jsonb, TRUE)
                ON CONFLICT (widget_slug) WHERE tenant_id IS NULL DO NOTHING
            """))
            await session.execute(text("""
                INSERT INTO widget_registry
                    (tenant_id, widget_slug, widget_name, category, widget_type,
                     data_source, render_template, target_route,
                     source_platform, default_size, resizable,
                     permission_level, feature_flag, config_schema, is_platform_standard)
                VALUES
                    (NULL, 'ai_usage_client_today', 'AI Usage (Today)', 'billing', 'data_panel',
                     'modules.intelligence.widget_service.get_firm_ai_usage',
                     'ai_usage_client_today.html',
                     NULL,
                     'praesidium', 'small', FALSE,
                     'attorney', NULL,
                     '{}'::jsonb, TRUE)
                ON CONFLICT (widget_slug) WHERE tenant_id IS NULL DO NOTHING
            """))
            await session.commit()
        print("  OK    widget_registry rows upserted")

    try:
        asyncio.run(_register())
    except Exception as exc:
        # Fallback: INSERT without ON CONFLICT if the partial-index
        # constraint name isn't matchable. Try a defensive UPSERT pattern.
        print(f"  WARN  ON CONFLICT failed ({exc}); trying SELECT-then-INSERT")
        async def _register_safe():
            async with AsyncSessionLocal() as session:
                for slug, name, tmpl in [
                    ('ai_usage_client_mtd',   'AI Usage (MTD)',   'ai_usage_client_mtd.html'),
                    ('ai_usage_client_today', 'AI Usage (Today)', 'ai_usage_client_today.html'),
                ]:
                    r = await session.execute(
                        text("SELECT 1 FROM widget_registry WHERE widget_slug = :s AND tenant_id IS NULL"),
                        {"s": slug})
                    if r.first():
                        print(f"  SKIP  {slug}: already registered")
                        continue
                    await session.execute(text("""
                        INSERT INTO widget_registry
                            (tenant_id, widget_slug, widget_name, category, widget_type,
                             data_source, render_template, target_route,
                             source_platform, default_size, resizable,
                             permission_level, feature_flag, config_schema, is_platform_standard)
                        VALUES
                            (NULL, :slug, :name, 'billing', 'data_panel',
                             'modules.intelligence.widget_service.get_firm_ai_usage',
                             :tmpl, NULL,
                             'praesidium', 'small', FALSE,
                             'attorney', NULL,
                             '{}'::jsonb, TRUE)
                    """), {"slug": slug, "name": name, "tmpl": tmpl})
                    print(f"  OK    registered {slug}")
                await session.commit()
        asyncio.run(_register_safe())


# ---------------------------------------------------------------------------
# TASK 6  client_detail.html — 6-column KPI row + revert Row 2
# ---------------------------------------------------------------------------
def task_client_detail():
    print("\n[6/6] client_detail.html — 6-col KPI row + revert Row 2 to 2 cols")
    p = FILES["client_detail"]

    # 6a — Expand KPI row. The billing_client_kpi widget renders 4 cards;
    # we now need it to render those same 4 cards but narrower, and we
    # append two more cards from separate widgets after it.
    #
    # The skeleton loader has `grid-template-columns:repeat(4,1fr)`.
    # The widget itself renders its own grid. We'll wrap both the widget
    # call and the two new AI widget calls in an outer grid of 6 columns.
    #
    # Before: a single <div id="widget-client-kpi"> with the billing_client_kpi
    # widget and a 4-card skeleton.
    # After: an outer grid of 6 columns, containing the original widget call
    # (which still renders 4 internal cards — the widget template needs to
    # be told not to wrap in its own grid OR we restructure to use a flex row).
    #
    # Cleaner approach: keep billing_client_kpi untouched, but wrap its
    # container in a flex row with the two new widgets. The skeleton gets
    # a different min-height and the widget's internal grid of 4 gets
    # placed flex:1 + the two AI widgets each take a fixed column width.

    kpi_before = '''{# ── KPI Row — billing_client_kpi widget ─────────────────────────────────────── #}
<div id="widget-client-kpi" style="margin-bottom:20px;"
     hx-get="/widgets/billing_client_kpi?client_id={{ client.id }}"
     hx-trigger="load"
     hx-swap="innerHTML">
  <div style="display:grid; grid-template-columns:repeat(4,1fr); gap:12px;">
    {% for _ in range(4) %}
    <div class="card" style="padding:14px; min-height:72px; background:#F9FAFB;">
      <div style="height:10px; width:60%; background:#E5E7EB; border-radius:3px; margin-bottom:8px;"></div>
      <div style="height:22px; width:50%; background:#E5E7EB; border-radius:3px;"></div>
    </div>
    {% endfor %}
  </div>
</div>'''

    kpi_after = '''{# ── KPI Row — 6 columns: 4 billing KPIs + AI Usage MTD + AI Usage Today ────── #}
<div style="display:grid; grid-template-columns:repeat(6,1fr); gap:12px; margin-bottom:20px;
            align-items:stretch;">

  {# Columns 1–4: billing_client_kpi widget renders 4 cards. Internal grid
     is overridden by container — widget template uses a subgrid or 4-in-a-row. #}
  <div id="widget-client-kpi" style="grid-column:1 / span 4;"
       hx-get="/widgets/billing_client_kpi?client_id={{ client.id }}"
       hx-trigger="load"
       hx-swap="innerHTML">
    <div style="display:grid; grid-template-columns:repeat(4,1fr); gap:12px; height:100%;">
      {% for _ in range(4) %}
      <div class="card" style="padding:14px; min-height:80px; background:#F9FAFB;">
        <div style="height:10px; width:60%; background:#E5E7EB; border-radius:3px; margin-bottom:8px;"></div>
        <div style="height:22px; width:50%; background:#E5E7EB; border-radius:3px;"></div>
      </div>
      {% endfor %}
    </div>
  </div>

  {# Column 5: AI Usage (MTD), client-scoped #}
  <div id="widget-ai-usage-client-mtd"
       hx-get="/widgets/ai_usage_client_mtd?client_id={{ client.id }}"
       hx-trigger="load"
       hx-swap="innerHTML">
    <div class="card" style="padding:14px; min-height:80px; background:#F9FAFB;">
      <div style="height:10px; width:70%; background:#E5E7EB; border-radius:3px; margin-bottom:8px;"></div>
      <div style="height:22px; width:50%; background:#E5E7EB; border-radius:3px;"></div>
    </div>
  </div>

  {# Column 6: AI Usage (Today), client-scoped #}
  <div id="widget-ai-usage-client-today"
       hx-get="/widgets/ai_usage_client_today?client_id={{ client.id }}"
       hx-trigger="load"
       hx-swap="innerHTML">
    <div class="card" style="padding:14px; min-height:80px; background:#F9FAFB;">
      <div style="height:10px; width:70%; background:#E5E7EB; border-radius:3px; margin-bottom:8px;"></div>
      <div style="height:22px; width:50%; background:#E5E7EB; border-radius:3px;"></div>
    </div>
  </div>

</div>'''

    replace_once(p, kpi_before, kpi_after,
                 marker='widget-ai-usage-client-mtd',
                 required=True)

    # 6b — Revert Row 2 to 2 columns (remove the AI usage column we put in
    # the previous pass; AI is now in the KPI row above).
    row2_before = '''{# ── Row 2: Matter Table + Timekeeper Allocation + Client AI Usage ────────── #}
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

    row2_after = '''{# ── Row 2: Matter Table + Timekeeper Allocation ────────────────────────────── #}
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

    replace_once(p, row2_before, row2_after,
                 marker='Row 2: Matter Table + Timekeeper Allocation ───',
                 required=False)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def main():
    print("Praesidium — UI Wiring Pass 2")
    print("=" * 60)

    task_rewrite_widgets()
    task_new_client_widgets()
    task_register_widgets()
    task_client_detail()

    print("\nAll tasks applied. No restart needed — Jinja reads on demand,")
    print("widget_registry updates are live on next widget request.")


if __name__ == "__main__":
    main()
