#!/bin/bash
# billing_fix.sh — Run on WEB-01

echo "=== 1. Verify views.py has billing_home route (not redirect) ==="
docker exec praesidium-web grep -n "billing_home\|RedirectResponse\|billing_root" /app/modules/billing/api/views.py | head -5

echo ""
echo "=== 2. Syntax check widget_service.py ==="
docker exec praesidium-web python3 -c "
import ast, sys
with open('/app/modules/billing/services/widget_service.py') as f:
    src = f.read()
try:
    ast.parse(src)
    print('Syntax OK')
except SyntaxError as e:
    print(f'SyntaxError at line {e.lineno}: {e.msg}')
"

echo ""
echo "=== 3. Test billing/ route directly ==="
curl -sI http://10.10.60.10:8000/billing/ | head -5
curl -sI -L http://10.10.60.10:8000/billing/ | head -5

echo ""
echo "=== 4. Check FastAPI registered routes for /billing ==="
docker exec praesidium-web python3 -c "
import sys; sys.path.insert(0,'.')
# Just list what routes the billing views router has
from modules.billing.api.views import views
for r in views.routes:
    print(r.methods, r.path)
" 2>/dev/null
