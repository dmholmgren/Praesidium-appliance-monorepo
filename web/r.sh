#!/bin/bash
# Praesidium WEB — deploy + restart loop (appliance edition)
#
# Usage from /opt/praesidium-web/:
#   ./r.sh         — restart web (no rebuild, picks up bind-mount edits)
#   ./r.sh build   — rebuild image, restart web AND proc workers
#   ./r.sh test    — restart web, then run pytest inside the container
#
# Bind mount means edits to /opt/praesidium-web/*.py are live in the
# container immediately. Restart only needed when imports change or
# the FastAPI app structure changes.
#
# Image rebuild ('./r.sh build') is needed when:
#   - requirements.txt changes
#   - Dockerfile changes
#   - new system packages

set -e

cd /opt/praesidium-web

COMPOSE="docker compose -f docker-compose.appliance.yml"
PROC_COMPOSE="docker compose -f /opt/praesidium-proc/docker-compose.yml"

case "${1:-}" in
  build)
    echo "[r.sh] rebuilding praesidium-platform:latest image..."
    $COMPOSE build web
    echo "[r.sh] restarting web..."
    $COMPOSE up -d web
    echo "[r.sh] restarting proc workers (same image)..."
    $PROC_COMPOSE up -d worker
    echo "[r.sh] done"
    ;;

  test)
    echo "[r.sh] restarting web..."
    $COMPOSE restart web
    echo "[r.sh] running pytest inside web container..."
    $COMPOSE exec web pytest -x --tb=short
    ;;

  *)
    echo "[r.sh] restarting web (bind-mount, no rebuild)..."
    $COMPOSE restart web
    echo "[r.sh] tailing logs (Ctrl-C to exit)..."
    $COMPOSE logs -f --tail=20 web
    ;;
esac
