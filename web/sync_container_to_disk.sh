#!/bin/bash
# sync_container_to_disk.sh
# Syncs live container state back to /opt/praesidium on WEB-01
# Run before any restart or when you want disk to reflect container state
# Usage: bash /opt/praesidium/sync_container_to_disk.sh

set -e
CONTAINER="praesidium-web"
APP_DIR="/opt/praesidium"

echo "=== Praesidium Container → Disk Sync ==="
echo "Container: $CONTAINER"
echo "Target:    $APP_DIR"
echo ""

# Core
echo "[1/8] Core app files..."
docker cp $CONTAINER:/app/app.py $APP_DIR/app.py
docker cp $CONTAINER:/app/core/auth/routes.py $APP_DIR/core/auth/routes.py

# eDiscovery routes
echo "[2/8] eDiscovery routes..."
mkdir -p $APP_DIR/modules/ediscovery/routes
docker cp $CONTAINER:/app/modules/ediscovery/routes/chat.py $APP_DIR/modules/ediscovery/routes/chat.py
docker cp $CONTAINER:/app/modules/ediscovery/routes/collection_status.py $APP_DIR/modules/ediscovery/routes/collection_status.py
docker cp $CONTAINER:/app/modules/ediscovery/routes/partials.py $APP_DIR/modules/ediscovery/routes/partials.py
docker cp $CONTAINER:/app/modules/ediscovery/routes/upload.py $APP_DIR/modules/ediscovery/routes/upload.py

# eDiscovery services
echo "[3/8] eDiscovery services..."
mkdir -p $APP_DIR/modules/ediscovery/services
docker cp $CONTAINER:/app/modules/ediscovery/services/widget_service.py $APP_DIR/modules/ediscovery/services/widget_service.py

# eDiscovery templates
echo "[4/8] eDiscovery templates..."
mkdir -p $APP_DIR/modules/ediscovery/templates/ediscovery/widgets
mkdir -p $APP_DIR/modules/ediscovery/templates/ediscovery/partials
docker cp $CONTAINER:/app/modules/ediscovery/templates/ediscovery/ediscovery_overview.html $APP_DIR/modules/ediscovery/templates/ediscovery/ediscovery_overview.html
docker cp $CONTAINER:/app/modules/ediscovery/templates/ediscovery/ediscovery_home.html $APP_DIR/modules/ediscovery/templates/ediscovery/ediscovery_home.html
docker cp $CONTAINER:/app/modules/ediscovery/templates/ediscovery/collection_status.html $APP_DIR/modules/ediscovery/templates/ediscovery/collection_status.html
docker cp $CONTAINER:/app/modules/ediscovery/templates/ediscovery/widgets/ediscovery_ingest_status.html $APP_DIR/modules/ediscovery/templates/ediscovery/widgets/ediscovery_ingest_status.html
docker cp $CONTAINER:/app/modules/ediscovery/templates/ediscovery/widgets/ediscovery_production_status.html $APP_DIR/modules/ediscovery/templates/ediscovery/widgets/ediscovery_production_status.html
docker cp $CONTAINER:/app/modules/ediscovery/templates/ediscovery/widgets/legal_intelligence_chat.html $APP_DIR/modules/ediscovery/templates/ediscovery/widgets/legal_intelligence_chat.html
docker cp $CONTAINER:/app/modules/ediscovery/templates/ediscovery/partials/collections_partial.html $APP_DIR/modules/ediscovery/templates/ediscovery/partials/collections_partial.html
docker cp $CONTAINER:/app/modules/ediscovery/templates/ediscovery/partials/review_partial.html $APP_DIR/modules/ediscovery/templates/ediscovery/partials/review_partial.html
docker cp $CONTAINER:/app/modules/ediscovery/templates/ediscovery/partials/productions_partial.html $APP_DIR/modules/ediscovery/templates/ediscovery/partials/productions_partial.html

# Widget templates
echo "[5/8] Widget templates..."
docker cp $CONTAINER:/app/modules/widgets/templates/widgets/ediscovery_drop_zone.html $APP_DIR/modules/widgets/templates/widgets/ediscovery_drop_zone.html

# Scripts
echo "[6/8] Scripts..."
mkdir -p $APP_DIR/scripts
docker cp $CONTAINER:/app/scripts/seed_litigation_folders.py $APP_DIR/scripts/seed_litigation_folders.py 2>/dev/null || echo "  (seed_litigation_folders not in container yet)"
docker cp $CONTAINER:/app/scripts/set_local_passwords.py $APP_DIR/scripts/set_local_passwords.py 2>/dev/null || true

# Connector templates
echo "[7/8] Connector templates..."
docker cp $CONTAINER:/app/core/templates/connectors/list.html $APP_DIR/core/templates/connectors/list.html 2>/dev/null || true
docker cp $CONTAINER:/app/core/templates/connectors/entities.html $APP_DIR/core/templates/connectors/entities.html 2>/dev/null || true

# Alembic migrations
echo "[8/8] Alembic migrations..."
for rev in 0030 0031 0032 0033; do
  docker cp $CONTAINER:/app/alembic/versions/${rev}_*.py $APP_DIR/alembic/versions/ 2>/dev/null || \
  echo "  (migration ${rev} not found in container)"
done

echo ""
echo "=== Sync complete ==="
echo "Disk is now in sync with container state."
echo "Run 'docker restart praesidium-web' if you want to reload from disk."
