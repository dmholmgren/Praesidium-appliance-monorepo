# CLAUDE.md — Praesidium Appliance (praesidium-web)

Production system for a live law firm (HJMM Legal). Read `.claude/skills/praesidium-method/SKILL.md` before any code, SQL, migration, or deploy work — it is mandatory, not optional.

## Absolute rules (violations have caused production failures)

1. `TRIM(tenant_id)` in every query — tenant_id is CHAR(36) with trailing spaces.
2. `CAST(:param AS uuid)` always; `::uuid` shorthand never; never bind None into a CAST.
3. Verify live schema before writing queries. Verify container state after deploys (`docker logs`, read file inside container).
4. New files need `docker cp`; existing files are live via bind mount. Clear `__pycache__` after Python changes.
5. Alembic = schema evolution only. Chain lives at `core/db/migrations/versions/`.
6. Extraction/chunking/embedding must exclude eDiscovery folders: `01-*`, `12-eDiscovery/`, `Production/`, `load_files/`.
7. Never touch `/opt/praesidium-mail`, `-livekit`, `-onlyoffice`, or `/mnt/` data mounts. Never commit `.env*` (non-template), `*.bak*`, keys, certs.
8. pytest passing is not done — new endpoints require live end-to-end curl verification through real auth/middleware.
9. No duct tape. Build correct, not fast. When uncertain on a production-touching action, stop and ask.

## Layout

- Backend: this repo (`/opt/praesidium-web`), FastAPI, bind-mounted to `/app` in the `praesidium-web` container (no psql binary inside — use Python for SQL).
- Frontend: `/opt/praesidium-ui` (React/Vite); `./build.sh` outputs to `/opt/praesidium-web/static/js/` (no dist/).
- MCP servers: `/opt/praesidium-mcp` (admin, :8891), `/opt/praesidium-mcp-app` (user, :8890).
- HJMM tenant_id: `986c0fee-1390-43bb-ad28-8cd1db6de53f`.
