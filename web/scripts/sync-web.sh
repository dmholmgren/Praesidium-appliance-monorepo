#!/bin/bash
# sync-web.sh — Sync /opt/praesidium to praesidium-web container and rebuild image
#
# Usage:
#   bash sync-web.sh           — rsync only, restart container (fast, no rebuild)
#   bash sync-web.sh --rebuild — rsync + docker build + recreate container (slow)
#   bash sync-web.sh --rebuild --no-cache — force full rebuild from scratch
#
# The bind mount /opt/praesidium:/app means most file changes are live immediately
# after a container restart. --rebuild is only needed when:
#   - Dockerfile changes (new system packages)
#   - requirements.txt changes (new Python packages)
#   - New directories that aren't bind-mounted

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
CONTAINER="praesidium-web"
IMAGE="praesidium-platform:latest"
COMPOSE_FILE="$APP_DIR/docker-compose.yml"

REBUILD=false
NO_CACHE=""

for arg in "$@"; do
  case $arg in
    --rebuild)   REBUILD=true ;;
    --no-cache)  NO_CACHE="--no-cache" ;;
  esac
done

echo "=== Praesidium Web Sync ==="
echo "App dir:   $APP_DIR"
echo "Container: $CONTAINER"
echo "Rebuild:   $REBUILD"
echo ""

# ── 1. Rsync local files into container ──────────────────────────────────────
# Bind mount handles /opt/praesidium → /app automatically.
# Docker cp is used for files that need to bypass bind mount caching.
echo ">> Syncing files via docker cp..."

# Core application files — always sync
docker cp "$APP_DIR/app.py"                    "$CONTAINER:/app/app.py"
docker cp "$APP_DIR/modules/"                  "$CONTAINER:/app/modules/"
docker cp "$APP_DIR/core/"                     "$CONTAINER:/app/core/"

echo "   app.py, modules/, core/ synced"

# ── 2. Clear pycache to prevent stale bytecode ───────────────────────────────
echo ">> Clearing __pycache__..."
docker exec "$CONTAINER" find /app -name "*.pyc" -delete 2>/dev/null || true
docker exec "$CONTAINER" find /app -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
echo "   pycache cleared"

# ── 3. Rebuild image if requested ────────────────────────────────────────────
if [ "$REBUILD" = true ]; then
  echo ""
  echo ">> Building image via docker compose $NO_CACHE..."
  echo "   (This takes several minutes when libreoffice/tesseract are involved)"
  cd "$APP_DIR"
  docker compose -f "$COMPOSE_FILE" build $NO_CACHE web
  echo "   Image built successfully"

  echo ""
  echo ">> Recreating container..."
  docker compose -f "$COMPOSE_FILE" stop web
  docker compose -f "$COMPOSE_FILE" rm -f web
  docker compose -f "$COMPOSE_FILE" up -d web

  echo "   Container recreated"
else
  # ── 4. Restart existing container ──────────────────────────────────────────
  echo ""
  echo ">> Restarting $CONTAINER..."
  docker restart "$CONTAINER"
fi

# ── 5. Wait for startup and verify ───────────────────────────────────────────
echo ""
echo ">> Waiting for startup..."
sleep 4

# Check container is running
STATUS=$(docker inspect --format='{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "unknown")
echo "   Container status: $STATUS"

if [ "$STATUS" = "running" ]; then
  # Quick health check
  echo ">> Checking startup log..."
  docker logs "$CONTAINER" --tail 10 2>&1 | grep -E "OK|FAIL|started|error|Error|Uvicorn" || true
  
  # Verify key binaries if rebuild
  if [ "$REBUILD" = true ]; then
    echo ""
    echo ">> Verifying installed binaries..."
    for bin in libreoffice tesseract pdftoppm antiword; do
      PATH_OUT=$(docker exec "$CONTAINER" which "$bin" 2>/dev/null || echo "NOT FOUND")
      echo "   $bin: $PATH_OUT"
    done
  fi
  
  echo ""
  echo "=== Sync complete ==="
else
  echo ""
  echo "!!! Container not running — check logs:"
  echo "    docker logs $CONTAINER --tail 50"
  exit 1
fi
