---
name: praesidium-method
description: Build, deployment, database, and verification rules for working on the Praesidium appliance (10.10.0.10). Load before editing any code, writing any SQL, running migrations, or deploying anything on this system. Distilled from ChatPrompts v1–v18.2.
---

# The Praesidium Method

Operating rules for the Praesidium Legal appliance. These are not preferences — each rule exists because its violation produced a real production failure.

## Session protocol

1. One component per session. Deploy and test before proceeding to the next.
2. Confirm Alembic head matches expectations before any schema work (`alembic current` inside the praesidium-web container).
3. Read live system state before proposing anything. Never assume; verify schema, file contents, and container state first.
4. No duct tape — build correct, not fast. Never use a migration, patch, or workaround to paper over a broken script.

## Database rules (asyncpg / PostgreSQL 16)

- `tenant_id` is CHAR(36) **with trailing spaces**. Every query touching it MUST use `TRIM(tenant_id)`. HJMM tenant: `986c0fee-1390-43bb-ad28-8cd1db6de53f`.
- UUID parameters: ALWAYS `CAST(:param AS uuid)`. NEVER `:param::uuid` shorthand — asyncpg's named-parameter translation breaks on it and the failure mode is silent in some test paths.
- NEVER bind `None` into `CAST(:param AS uuid)` — use a non-null sentinel string or restructure the query.
- Verify the live schema (`query_schema` / information_schema) before writing any query against a table you haven't confirmed this session.
- The `praesidium-web` container has **no psql binary**. Run SQL via Python (asyncpg/psycopg2) inside the container.
- Alembic migrations are for schema evolution ONLY — never to repair the side effects of a broken script. The canonical chain lives at `core/db/migrations/versions/` (the old `alembic/versions/` tree is retired).

## Deployment rules

- `/opt/praesidium-web` is bind-mounted to `/app` in the container: host edits to EXISTING files are live immediately.
- NEW files or directories not present at container start require `docker cp` (or container restart).
- After any Python change: explicitly clear `__pycache__` for the affected module.
- After any deploy: immediately check `docker logs` for import errors. Then verify the container actually has the change (read the file inside the container) — never trust the host path alone.
- Frontend: source in `/opt/praesidium-ui`; build with `cd /opt/praesidium-ui && ./build.sh`. Vite outputs directly to `/opt/praesidium-web/static/js/` — there is no `dist/`.
- Prefer complete file regeneration over incremental sed patches for complex files.
- `web-01` (10.10.60.10): SSH is blocked for the service account — files cannot be staged there directly.

## Verification standard

pytest is necessary but not sufficient. Every new endpoint surface requires an end-to-end curl integration check against the live appliance: real middleware stack, real auth, real DB writes, real workers. Bugs have shipped behind passing tests three separate times (`::text` cast, LibreOffice `--user-profile`, `PUBLIC_PATHS` middleware gap).

## Data-integrity invariants (architectural law)

- Client data belongs to the client. No operation ever destroys provenance or chain of custody. Never overwrite an original uploader or timestamp.
- Production save ≠ export. Saving a production is a DB event; download is a delivery event. Never combine them.
- §0 canonical-text invariant: one designated canonical string per document; `canonical[char_start:char_end] == stored_text` must always hold. Anything violating it is disposable.
- `dms_documents` mixes DMS working docs and eDiscovery production files under the same matter path. Extraction, chunking, and embedding pipelines MUST exclude eDiscovery folders: `01-*`, `12-eDiscovery/`, `Production/`, `load_files/`.

## Architecture doctrine (when designing, not just deploying)

- Everything is data: connectors, widgets, nav (`ui_nav_items` via `nav_service.py`, Redis-cached), layouts, prompt templates, AI routing are database rows. Adding a panel is an INSERT, not a code change.
- Primitive-not-chunk: data primitives (document_sections, allegations, citations, entities) are Layer 1; embeddings are Layer 3 derivatives. Chunks consume primitives.
- Structural-first, model-on-residue: route work to the cheapest sufficient processor; many work units need no model at all.

## Agentic guardrails (Claude Code on this box)

- Scope: `/opt/praesidium-web`, `/opt/praesidium-ui`, and sibling source repos only. NEVER touch `/opt/praesidium-mail`, `/opt/praesidium-onlyoffice` (live mail data, TLS keys), or any `/mnt/` data mounts. **LiveKit exception (2026-06-13):** `/opt/praesidium-livekit` and `livekit-egress` are IN SCOPE under the discipline in `.claude/briefs/Praesidium_LiveKit_Deposition_BuildBrief_v1.md` §0 — branch before edit; never destroy a recording, transcript, designation, or provenance row; deploy-and-verify each change.
- Never commit `.env*` (except `*.template`), `*.bak*`, keys, or certs.
- Commit checkpoints before and after each component. Small commits, real messages.
- This is a production system serving a live law firm. When uncertain, stop and ask.
