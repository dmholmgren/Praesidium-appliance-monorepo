# Praesidium — Legal Practice Intelligence Platform

Multi-tenant, AI-native legal practice management platform.

## Quick Start (MAIN-PRD-WEB-01)

```bash
cp .env.example .env
# Fill in all <REPLACE> values

docker compose up -d
docker compose exec web alembic upgrade head
curl http://localhost:8000/health
```

## VM Architecture

| VM | IP | Compose File | Containers |
|---|---|---|---|
| MAIN-PRD-WEB-01 | 10.10.60.10 | docker-compose.yml | web, nginx |
| MAIN-PRD-PROC-01 | 10.10.60.12 | docker-compose.proc.yml | worker, scheduler, redis, meilisearch |
| MAIN-PRD-FBRG-01 | 10.10.60.13 | docker-compose.fbrg.yml | file-bridge |
| MAIN-PRD-WSS-01 | 10.10.60.14 | docker-compose.wss.yml | whisper |

## Mandatory Rules

1. `tenant_id CHAR(36) NOT NULL` — first non-PK column, every table
2. All DB access: `TenantSession` only
3. All external services: `core/services/` interfaces only
4. All AI calls: `AIService` only
5. All DB writes: `write_audit()` via `core/audit.py`
6. Backend: Python/FastAPI. Frontend: HTMX + Tailwind. No React.
7. Config: environment variables only
8. Background work: RQ jobs only
9. Schema changes: Alembic migration files only
10. ALL branding via `BrandingService`
