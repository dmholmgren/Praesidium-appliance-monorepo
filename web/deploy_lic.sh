#!/bin/bash
# deploy_legal_intelligence_chat.sh
# Run from WEB-01 after uploading all files via Bitvise
# Upload targets:
#   ediscovery_chat_route.py  → /opt/praesidium/modules/ediscovery/routes/chat.py
#   legal_intelligence_chat.html → /opt/praesidium/modules/ediscovery/templates/ediscovery/widgets/legal_intelligence_chat.html
#   ediscovery_overview.html  → /opt/praesidium/modules/ediscovery/templates/ediscovery/ediscovery_overview.html

set -e

echo "=== Creating widget template directory ==="
docker exec praesidium-web mkdir -p /app/modules/ediscovery/templates/ediscovery/widgets

echo "=== Copying chat route ==="
docker cp /opt/praesidium/modules/ediscovery/routes/chat.py \
  praesidium-web:/app/modules/ediscovery/routes/chat.py

echo "=== Copying Legal Intelligence Chat widget template ==="
docker cp /opt/praesidium/modules/ediscovery/templates/ediscovery/widgets/legal_intelligence_chat.html \
  praesidium-web:/app/modules/ediscovery/templates/ediscovery/widgets/legal_intelligence_chat.html

echo "=== Copying updated eDiscovery overview ==="
docker cp /opt/praesidium/modules/ediscovery/templates/ediscovery/ediscovery_overview.html \
  praesidium-web:/app/modules/ediscovery/templates/ediscovery/ediscovery_overview.html

echo "=== Copying ingest status widget ==="
docker cp /opt/praesidium/modules/ediscovery/templates/ediscovery/widgets/ediscovery_ingest_status.html \
  praesidium-web:/app/modules/ediscovery/templates/ediscovery/widgets/ediscovery_ingest_status.html

echo "=== Copying production status widget ==="
docker cp /opt/praesidium/modules/ediscovery/templates/ediscovery/widgets/ediscovery_production_status.html \
  praesidium-web:/app/modules/ediscovery/templates/ediscovery/widgets/ediscovery_production_status.html

echo "=== Copying widget service ==="
docker cp /opt/praesidium/modules/ediscovery/services/widget_service.py \
  praesidium-web:/app/modules/ediscovery/services/widget_service.py

echo "=== Restarting web container ==="
docker restart praesidium-web

echo "=== Done ==="
echo ""
echo "Next: register the chat router in app.py"
echo "  from modules.ediscovery.routes.chat import router as ediscovery_chat_router"
echo "  app.include_router(ediscovery_chat_router)"
