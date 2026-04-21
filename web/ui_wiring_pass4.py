"""
Praesidium — UI Wiring Pass 4 (layout polish)
==============================================
Fixes from visual QA:

  - Client detail KPI cards (ai_usage_client_mtd, ai_usage_client_today)
    match native billing_client_kpi card pattern exactly: label / $number /
    count-as-footnote. Remove the redundant "Month to date" / "Today"
    subtitle since the title already says (MTD) / (Today).

  - Firm AI Usage widget (ai_usage_firm) matches Bill State rhythm: label
    on its own line above, dollar + calls inline on the row below. Applies
    to the MTD and Today blocks.

  - Same inline treatment for my_matters and unallocated for consistency
    within the AI widget family.

Idempotent. .bak-uiwiring4 backups per file.
"""

from __future__ import annotations
import shutil
from pathlib import Path

ROOT = Path("/app")

FILES = {
    "firm":         ROOT / "modules/intelligence/templates/ai_usage_firm.html",
    "my_matters":   ROOT / "modules/intelligence/templates/ai_usage_my_matters.html",
    "unallocated":  ROOT / "modules/intelligence/templates/ai_unallocated_review.html",
    "client_mtd":   ROOT / "modules/intelligence/templates/ai_usage_client_mtd.html",
    "client_today": ROOT / "modules/intelligence/templates/ai_usage_client_today.html",
}


def backup(p: Path) -> None:
    bak = p.with_suffix(p.suffix + ".bak-uiwiring4")
    if p.exists() and not bak.exists():
        shutil.copy2(p, bak)


def write_template(p: Path, content: str) -> None:
    backup(p)
    p.write_text(content)
    print(f"  OK  wrote {p.name}")


# ---------------------------------------------------------------------------
# Client KPI cards — match native billing_client_kpi pattern EXACTLY
# Label / $big-number / small-footnote-count — three stacked lines.
# ---------------------------------------------------------------------------
CLIENT_MTD_TEMPLATE = """{# widgets/ai_usage_client_mtd.html
   Client-scoped AI MTD widget.
   Matches native billing_client_kpi card: label / big-$ / footnote.
#}
<div class="card" style="padding:14px;">
  <div style="font-size:10px; color:var(--muted); text-transform:uppercase;
              letter-spacing:.05em; margin-bottom:4px;">
    AI Usage (MTD)
  </div>
  <div style="font-size:22px; font-weight:600; color:var(--text);
              font-variant-numeric:tabular-nums;">
    {% if empty_state %}$0.00{% else %}${{ "{:,.2f}".format(month.total) }}{% endif %}
  </div>
  <div style="font-size:11px; color:var(--muted); margin-top:2px;">
    {% if empty_state %}0 calls{% else %}{{ month.calls }} call{{ 's' if month.calls != 1 else '' }}{% endif %}
  </div>
</div>
"""

CLIENT_TODAY_TEMPLATE = """{# widgets/ai_usage_client_today.html
   Client-scoped AI Today widget.
   Matches native billing_client_kpi card: label / big-$ / footnote.
#}
<div class="card" style="padding:14px;">
  <div style="font-size:10px; color:var(--muted); text-transform:uppercase;
              letter-spacing:.05em; margin-bottom:4px;">
    AI Usage (Today)
  </div>
  <div style="font-size:22px; font-weight:600; color:var(--text);
              font-variant-numeric:tabular-nums;">
    {% if empty_state %}$0.0000{% else %}${{ "{:,.4f}".format(today.total) }}{% endif %}
  </div>
  <div style="font-size:11px; color:var(--muted); margin-top:2px;">
    {% if empty_state %}0 calls{% else %}{{ today.calls }} call{{ 's' if today.calls != 1 else '' }}{% endif %}
  </div>
</div>
"""


# ---------------------------------------------------------------------------
# Firm widget — match Bill State rhythm.
# Label on own line, then dollar + calls inline on next line.
# ---------------------------------------------------------------------------
FIRM_TEMPLATE = """{# widgets/ai_usage_firm.html
   Firm AI Usage — shaded header + Bill State-style body rhythm.
   Label-on-top, dollar+calls-inline-below.
#}
<div style="display:flex; flex-direction:column; height:100%; min-height:0;"
     id="widget-ai-usage-firm-body"
     hx-get="/widgets/ai_usage_firm{% if scoped_to_client and (top_matters and top_matters[0].client_id) %}?client_id={{ top_matters[0].client_id }}{% endif %}"
     hx-trigger="every 60s"
     hx-swap="outerHTML">

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

  {# Totals — Bill State rhythm: label above, dollar + calls inline below #}
  <div style="padding:8px 12px; flex-shrink:0;">
    <div style="margin-bottom:6px;">
      <div style="font-size:9px; font-weight:700; color:var(--muted);
                  text-transform:uppercase; letter-spacing:0.05em;
                  margin-bottom:2px;">
        Month to date
      </div>
      <div style="display:flex; align-items:baseline; gap:8px;">
        <span style="font-size:16px; font-weight:700; color:var(--text);
                     font-variant-numeric:tabular-nums; line-height:1;">
          ${{ "{:,.2f}".format(month.total) }}
        </span>
        <span style="font-size:10px; color:var(--muted);">
          · {{ month.calls }} call{{ 's' if month.calls != 1 else '' }}
        </span>
      </div>
    </div>
    <div>
      <div style="font-size:9px; font-weight:700; color:var(--muted);
                  text-transform:uppercase; letter-spacing:0.05em;
                  margin-bottom:2px;
                  display:flex; align-items:center; justify-content:space-between;">
        <span>Today</span>
        <span>
          {% if today.fallbacks > 0 %}
          <span style="font-size:10px; color:#1d4ed8; font-weight:600;"
                title="Fallbacks today">↓{{ today.fallbacks }}</span>
          {% endif %}
          {% if today.overrides > 0 %}
          <span style="font-size:10px; color:#dc2626; font-weight:600; margin-left:6px;"
                title="Overrides today">!{{ today.overrides }}</span>
          {% endif %}
        </span>
      </div>
      <div style="display:flex; align-items:baseline; gap:8px;">
        <span style="font-size:16px; font-weight:700; color:var(--text);
                     font-variant-numeric:tabular-nums; line-height:1;">
          ${{ "{:,.4f}".format(today.total) }}
        </span>
        <span style="font-size:10px; color:var(--muted);">
          · {{ today.calls }} call{{ 's' if today.calls != 1 else '' }}
        </span>
      </div>
    </div>
  </div>

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
       style="color:#dc2626; text-decoration:none; font-weight:600;">
      {{ pending_exceptions }} pending
    </a>
    {% endif %}
  </div>
  {% endif %}

  {% endif %}
</div>
"""


# ---------------------------------------------------------------------------
# My AI Usage — consistent with firm widget body pattern
# ---------------------------------------------------------------------------
MY_MATTERS_TEMPLATE = """{# widgets/ai_usage_my_matters.html #}
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

  <div style="padding:8px 12px; flex-shrink:0;">
    <div style="margin-bottom:6px;">
      <div style="font-size:9px; font-weight:700; color:var(--muted);
                  text-transform:uppercase; letter-spacing:0.05em;
                  margin-bottom:2px;">
        Month to date
      </div>
      <div style="font-size:16px; font-weight:700; color:var(--text);
                  font-variant-numeric:tabular-nums; line-height:1;">
        ${{ "{:,.2f}".format(totals.month) }}
      </div>
    </div>
    <div>
      <div style="font-size:9px; font-weight:700; color:var(--muted);
                  text-transform:uppercase; letter-spacing:0.05em;
                  margin-bottom:2px;">
        Today
      </div>
      <div style="font-size:16px; font-weight:700; color:var(--text);
                  font-variant-numeric:tabular-nums; line-height:1;">
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
          <span style="color:#b45309;">⚠ {{ row.pending_exceptions }}</span>
          {% elif row.override_count > 0 %}
          <span style="color:#dc2626;">!{{ row.override_count }}</span>
          {% elif row.fallback_count > 0 %}
          <span style="color:#1d4ed8;">↓{{ row.fallback_count }}</span>
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
# Unallocated — consistent body pattern
# ---------------------------------------------------------------------------
UNALLOCATED_TEMPLATE = """{# widgets/ai_unallocated_review.html #}
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

  <div style="padding:8px 12px; flex-shrink:0;">
    <div style="font-size:9px; font-weight:700; color:var(--muted);
                text-transform:uppercase; letter-spacing:0.05em;
                margin-bottom:2px;">
      Month to date
    </div>
    <div style="display:flex; align-items:baseline; gap:8px;">
      <span style="font-size:16px; font-weight:700; color:var(--text);
                   font-variant-numeric:tabular-nums; line-height:1;">
        ${{ "{:,.2f}".format(total_usd) }}
      </span>
      <span style="font-size:10px; color:var(--muted);">
        · {{ total_count }} call{{ 's' if total_count != 1 else '' }}
      </span>
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


def main():
    print("Praesidium — UI Wiring Pass 4 (layout polish)")
    print("=" * 60)
    write_template(FILES["client_mtd"],   CLIENT_MTD_TEMPLATE)
    write_template(FILES["client_today"], CLIENT_TODAY_TEMPLATE)
    write_template(FILES["firm"],         FIRM_TEMPLATE)
    write_template(FILES["my_matters"],   MY_MATTERS_TEMPLATE)
    write_template(FILES["unallocated"],  UNALLOCATED_TEMPLATE)
    print("\nDone. Reload in browser — no restart needed.")


if __name__ == "__main__":
    main()
