#!/bin/bash
echo "Restarting..."
docker restart praesidium-web
sleep 6
echo "Test results:"
docker exec praesidium-web pytest tests/ -v --tb=short 2>&1 | tail -50
