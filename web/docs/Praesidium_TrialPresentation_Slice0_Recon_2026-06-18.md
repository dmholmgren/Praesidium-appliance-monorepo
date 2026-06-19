# Praesidium Trial Presentation — Slice 0 Recon Findings

**Date:** June 18, 2026
**For:** `Praesidium_TrialPresentation_Scope_v1*.md` (corrects the data model + relay sections against live state)
**Method:** live reads on the appliance (alembic_current, information_schema, query_schema, read_container_file). Supersedes remembered contracts where they conflict.

---

## 1. Relay decision — **SIBLING, not generalize**

`core/services/presentation_ws.py` (the depot relay) read in full. It is:
- Pure **in-memory** (`_sessions: dict`, `_ws_clients: dict`) — **touches no DB**.
- A flat **broadcast bus** — no role concept; every client gets the same `state_update`.
- WS endpoint **`/ws/present/{token}`**; HTML display at `GET /present/{token}`; token = `secrets.token_urlsafe(24)`.
- State object: `{document, page, zoom, annotations, exhibit_label, presenting}`.
- `created_by` stored as a **username string**, not a user id.

Trial needs roles, per-display routing, DB-backed session + audit, and marking writes into the trial tables. Adding that to the depot file entangles a working depo surface with trial logic right before trial. **Decision: build `trial_presentation_ws.py` as a sibling** that copies the proven session/token/WS/QR pattern and adds the trial layer. Depot relay untouched.

---

## 2. Live-state corrections (bake into all slices)

| Item | Scope said | Live truth |
|------|-----------|-----------|
| Alembic head | `0058_extraction_queue` | **`0133_trial_discovery_tab`** |
| User id type | `uuid` (actor_id) | **`bigint`** (matches `markup_sets.owner_user_id`, `doc_annotations.created_by`) |
| WS path | `/present/{token}` | **`/ws/present/{token}`** (HTML page is `/present/{token}`) |
| Display render | assumed real | **SIMULATED lorem-ipsum** — real render never shipped (v13.5 pending) |

---

## 3. Existing trial schema — **reuse, do not recreate**

The June trial-center build already created (confirmed via information_schema):

- **`trial_proceedings`** — case-level trial/appeal container (caption, cause_number, trial_court, court_of_appeals, appellate_cause_number, dates). The live presentation session **FKs into this** (`trial_id`).
- **`trial_exhibits`** — the exhibit register / **queue source**: party, exhibit_number, exhibit_label, document_id + document_source, sponsoring_witness, status, admitted, marked/offered/ruling page+line+locus, `rr_*` reporter-record fields, conditional.
- **`trial_exhibit_state`** — status rollup: status, admitted, conditional, color_state, objection_count / open_objection_count / sustained_count.
- **`trial_exhibit_objections`**, **`trial_exhibit_usages`** (exhibit↔transcript locus: transcript_id, page, line, char_start/end, snippet — *not* a live display log), **`trial_preservation`** (appellate index).
- **`doc_annotations`** — full annotation primitive: geometry (x/y/w/h numeric), text offsets, redaction_style/label, highlight_color, freehand `path_data`, soft-delete, **`markup_set_id`**.
- **`markup_sets`** — saved annotation-set parent: name, kind, scope, owner_user_id (bigint), **`embossed_document_id`/`embossed_path` (burn-to-PDF already built)**, meta.

No `*present*` / `conference_session*` tables exist — depot session state is purely in-memory, so our new tables won't collide.

---

## 4. Revised data model (replaces v1 §4 / v1.1 §G)

**Drop** `trial_annotation_sets` → reuse **`markup_sets` + `doc_annotations.markup_set_id`**. Burn-to-PDF = existing `embossed_*`. Ephemeral vs. saved = `markup_sets.scope`/`kind`.

**Marking** writes existing **`trial_exhibits`** (numbering/label/loci) + **`trial_exhibit_state`** (status/admitted) + **`trial_exhibit_objections`** — no parallel state.

**Net-new (Slice 1), two tables only:**
- **`trial_presentation_sessions`** — `id uuid`, `tenant_id char(36)`, `proceeding_id uuid` (→ trial_proceedings), `matter_id uuid`, `title text`, `status text`, `exhibit_numbering_start int`, `created_by bigint`, `config jsonb`, `started_at`, `ended_at`.
- **`trial_presentation_events`** — immutable audit: `id uuid`, `session_id uuid` (→ sessions), `tenant_id char(36)`, `event_type text`, `document_id uuid null`, `exhibit_id uuid null` (→ trial_exhibits), `page int null`, `target_roles text[]`, `actor_id bigint`, `actor_role text`, `payload jsonb`, `occurred_at timestamptz default now()`.

---

## 5. New required build item (was missing)

**Real document render on the display.** The dumb terminal currently injects lorem-ipsum. For trial it must show the actual exhibit. Build path = **pre-rasterize the exhibit page to an image and push the image URL** to the display (dumb terminal stays dumb; identical pixels on any device; light on WiFi). This becomes a first-class slice in the trial cut, highest priority alongside the session core.

---

## 6. Revised Slice 1

Small: **2-table Alembic migration off `0133_trial_discovery_tab`** (`trial_presentation_sessions` + `trial_presentation_events`) + the sibling `trial_presentation_ws.py` session create/end + audit-append skeleton. The §J decisions (one-QR-vs-per-role, annotation persistence, video y/n) gate Slices 3/4 — **Slice 1 is unblocked.**
