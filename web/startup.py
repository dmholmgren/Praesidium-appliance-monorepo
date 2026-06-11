#!/usr/bin/env python3
"""
Startup wrapper — catches import/startup errors and keeps
the container alive so you can read logs and exec in.
"""
import sys
import os
import time
import traceback

os.environ.setdefault("PYTHONPATH", "/app")

print("=" * 60, flush=True)
print("Praesidium startup check", flush=True)
print(f"Python: {sys.version}", flush=True)
print(f"Working dir: {os.getcwd()}", flush=True)
print(f"DATABASE_URL set: {'yes' if os.environ.get('DATABASE_URL') else 'no'}", flush=True)
print(f"REDIS_URL set: {'yes' if os.environ.get('REDIS_URL') else 'no'}", flush=True)
print("=" * 60, flush=True)

# Test imports one by one
imports = [
    ("sqlalchemy", "import sqlalchemy"),
    ("fastapi", "import fastapi"),
    ("starlette", "import starlette"),
    ("uvicorn", "import uvicorn"),
    ("jinja2", "import jinja2"),
    ("redis", "import redis"),
    ("httpx", "import httpx"),
    ("core.db.base", "from core.db.base import Base"),
    ("core.models", "import core.models"),
    ("core.services.branding", "from core.services.branding import BrandingService"),
    ("core.db.tenant", "from core.db.tenant import TenantResolverMiddleware"),
    ("core.auth.middleware", "from core.auth.middleware import AuthMiddleware"),
    ("app", "import app"),
]

all_ok = True
for name, stmt in imports:
    try:
        exec(stmt)
        print(f"  OK  {name}", flush=True)
    except Exception as e:
        print(f"  FAIL {name}: {e}", flush=True)
        traceback.print_exc()
        all_ok = False
        break  # Stop at first failure

if all_ok:
    print("\nAll imports OK — starting uvicorn...", flush=True)
    os.execvp("uvicorn", [
        "uvicorn", "app:app",
        "--host", "0.0.0.0",
        "--port", "8000",
        "--workers", "1",
        "--log-level", "info",
        "--http", "httptools",
        "--timeout-keep-alive", "120",
    ])
else:
    print("\n*** STARTUP FAILED — container staying alive for debugging ***", flush=True)
    print("Run: docker compose exec web bash", flush=True)
    print("Then: python3 -c 'import app'", flush=True)
    while True:
        time.sleep(3600)
