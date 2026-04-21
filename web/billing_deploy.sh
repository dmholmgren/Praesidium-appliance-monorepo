#!/bin/bash
# billing_deploy.sh
# Run from /opt/praesidium after SCP'ing all files there
# Usage: bash billing_deploy.sh

set -e

echo "=== Billing Build Deploy ==="

# ── Templates: billing module ─────────────────────────────────────────────
docker cp modules/billing/templates/billing/billing_home.html \
    praesidium-web:/app/modules/billing/templates/billing/billing_home.html

# ── Widget templates: create dir then copy ────────────────────────────────
docker exec praesidium-web mkdir -p /app/modules/billing/templates/widgets

docker cp modules/billing/templates/widgets/billing_revenue_chart.html \
    praesidium-web:/app/modules/billing/templates/widgets/billing_revenue_chart.html

docker cp modules/billing/templates/widgets/billing_ar_aging_snapshot.html \
    praesidium-web:/app/modules/billing/templates/widgets/billing_ar_aging_snapshot.html

docker cp modules/billing/templates/widgets/billing_timekeeper_kpi.html \
    praesidium-web:/app/modules/billing/templates/widgets/billing_timekeeper_kpi.html

docker cp modules/billing/templates/widgets/billing_bill_state_summary.html \
    praesidium-web:/app/modules/billing/templates/widgets/billing_bill_state_summary.html

docker cp modules/billing/templates/widgets/billing_ai_chat.html \
    praesidium-web:/app/modules/billing/templates/widgets/billing_ai_chat.html

# ── Updated firm_matter_tree (shared widget template) ────────────────────
docker cp modules/widgets/templates/widgets/firm_matter_tree.html \
    praesidium-web:/app/modules/widgets/templates/widgets/firm_matter_tree.html

# ── Billing service: new widget_service.py ───────────────────────────────
docker cp modules/billing/services/widget_service.py \
    praesidium-web:/app/modules/billing/services/widget_service.py

# ── Billing views: updated routes (replaces redirect) ────────────────────
docker cp modules/billing/api/views.py \
    praesidium-web:/app/modules/billing/api/views.py

echo "=== All files copied. Restarting container... ==="
docker restart praesidium-web

echo "=== Done. Verify: ==="
echo "  curl -sI http://10.10.60.10:8000/billing/ | grep -E 'HTTP|Location'"
echo "  curl -s 'http://10.10.60.10:8000/widgets/billing_bill_state_summary' | head -3"
