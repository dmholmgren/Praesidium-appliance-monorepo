#!/bin/bash
# Run on WEB-01: bash billing_diag.sh

echo "=== 1. Billing route registration ==="
docker exec praesidium-web grep -rn "billing_root\|billing_home\|/billing" /app/modules/billing/api/views.py | head -20

echo ""
echo "=== 2. Widget internal server error ==="
docker exec praesidium-web python3 -c "
import asyncio, traceback
import sys
sys.path.insert(0, '/app')

async def test():
    try:
        from modules.billing.services.widget_service import get_billing_bill_state_summary
        result = await get_billing_bill_state_summary({'tenant_id': 'hjmm-prod', 'request': None})
        print('Service result:', result)
    except Exception as e:
        traceback.print_exc()

asyncio.run(test())
"

echo ""
echo "=== 3. Template search path — can widget_routes find billing templates? ==="
docker exec praesidium-web python3 -c "
from jinja2 import Environment, FileSystemLoader, select_autoescape
dirs = [
    '/app/modules/widgets/templates',
    '/app/modules/dms/templates',
    '/app/modules/billing/templates',
    '/app/modules/ediscovery/templates',
    '/app/core/templates',
]
env = Environment(loader=FileSystemLoader(dirs), autoescape=select_autoescape(['html']))
try:
    t = env.get_template('widgets/billing_bill_state_summary.html')
    print('Template found:', t.filename)
except Exception as e:
    print('Template NOT found:', e)
"

echo ""
echo "=== 4. Check how billing router is registered in app.py ==="
docker exec praesidium-web grep -n "billing\|register_billing" /app/app.py | head -20

echo ""
echo "=== 5. Billing views.py route decorators ==="
docker exec praesidium-web grep -n "@views\|async def billing" /app/modules/billing/api/views.py | head -10
