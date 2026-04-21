# Praesidium Platform — Makefile
# VM: MAIN-PRD-WEB-01 (10.10.60.10)

.PHONY: up down build migrate test lint health

up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose build --no-cache

logs:
	docker compose logs -f web

migrate:
	docker compose exec web alembic upgrade head

migrate-gen:
	docker compose exec web alembic revision --autogenerate -m "$(msg)"

test:
	docker compose exec web pytest tests/ -v --cov=core --cov-report=term-missing

health:
	curl -s http://localhost:8000/health | python3 -m json.tool

shell:
	docker compose exec web python3

bash:
	docker compose exec web bash
