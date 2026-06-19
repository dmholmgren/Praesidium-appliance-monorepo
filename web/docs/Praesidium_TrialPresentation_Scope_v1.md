# Praesidium Trial Presentation Module — Scope v1.0

**Module:** M14 TrialDesk — Live Presentation Engine (the deferred S3-006 "later phase")
**Surface:** Trial Center → new **Present** tab + per-role Display Client
**Date:** June 18, 2026
**ChatPrompts baseline:** v18.5 (+ June 16 Trial Center scope, June 15/16 depo staging build)
**Patent:** S3-005 / S3-006 / S3-007 (Series 3) + non-provisional new matter
**Status:** SCOPE — recon gate not yet run

---

## 0. Frame

The Trial Center already has the static surface: Pleadings · Motions · Depositions · Hearing Transcripts · **Trial Exhibits** (3-panel: party-grouped nav / viewer / offer-admit-obj controls + Annotations tab). The June 16 scope explicitly parked the **live push-presentation engine** as "a later phase." This doc is that phase.

The bet from the depo work holds: **we are not building an HDMI matrix switch with a software skin.** Sanction and TrialDirector mirror one source to every output and treat the case record as an import problem. We start from a fully-indexed matter and route **independently per display by role**, with the record writing itself. That per-display, role-differentiated, judicially-gated routing is both the competitive moat and the S3-006/S3-007 claim.

**The reuse thesis:** the depot module already shipped the hard infrastructure (session + token + WebSocket relay + zero-chrome display page + QR provisioning). The trial module is mostly (a) **generalizing** that relay so trial and depo share one core, and (b) adding the trial-only layer on top: **role routing, per-display blank/hold, judicial override, saved/ephemeral annotation sets, depo-clip impeachment, and synchronous DMS exhibit write-back.**

---

## 1. Recon Gate — run before any code (Part 0 of the build)

Do not write a line until these are confirmed live (Appliance Admin MCP). Remembered contracts below are from deltas v13.5/v13.6 + the June 16 scope and **must be verified, not trusted**:

| # | Read | What we're confirming |
|---|------|------------------------|
| R1 | `read_container_file` → `core/services/presentation_ws.py` | Exact session model (`_sessions` dict shape), state object keys, endpoint paths (`POST/DELETE /api/v1/present/sessions`, `GET /present/{token}`), token scheme (`secrets.token_urlsafe`), WS message envelope |
| R2 | `grep -rl "present" /opt/praesidium-ui/src` + locate the Trial Exhibits component | Exact JSX filename for the Trial Exhibits 3-panel + the existing Annotations tab; the shared `pdf-annotation-viewer.jsx` props (`readOnly`, `focusPage` confirmed v18.1) |
| R3 | `query_schema` on `project_documents`, `document_identifiers`, `transcript_lines`, `transcript_exhibits`, `depositions` | Exhibit register columns (`party`, `status`, `exhibit_label`, `parent_id` nesting from 0023), designation page:line model, video timecode availability |
| R4 | `run_readonly_query` — does any `*_present*` / `conference_session*` / `trial_*` table already exist? | Avoid Alembic drift; v13.5 listed `conference_sessions`/`conference_session_log` as *pending* (never built) — confirm still absent |
| R5 | `alembic_current` | Head before new migration (v18.5 said `0058_extraction_queue`) |
| R6 | Confirm LiveKit pop-out video viewer component + how depo video clips are currently played | Reuse path for clip-to-display in Slice 5 |

If R1 shows the depo relay is too entangled to generalize cleanly, fall back to a **sibling** `trial_presentation_ws.py` that copies the proven pattern rather than refactoring a working depo surface mid-trial-prep. **No duct tape, but also don't destabilize a working depo relay right before a trial.**

---

## 2. What we inherit (reused, do not rebuild)

| Component | Source | Reuse in trial |
|-----------|--------|----------------|
| Session + crypto token + WS relay | `presentation_ws.py` | Core transport — generalize to carry a **role** per connected display |
| `/present/{token}` zero-chrome display page | depot | Becomes the **Display Client**, now role-aware |
| QR provisioning | depot Conference tab | Same scan-to-connect; QR now encodes/assigns a **role** |
| `BroadcastChannel('praesidium-conference-space')` | depot | Same-browser second monitor / popped-out console |
| Shared `pdf-annotation-viewer.jsx` (`readOnly`, `focusPage`, percent overlay) | v18.1 | Display Client render + annotation overlay base |
| Exhibit sticker / Bates engine (PyMuPDF) | `bates_engine.py` / `exhibit_sticker_engine.py` | Exhibit-label stamping on push |
| Trial Exhibits register (party-grouped, status, objection overlay) | June 16 Trial Center | The **exhibit queue source** for the Presenter Console |
| `document_identifiers` cross-link | v14.6 | PX-/DX- trial designations alongside Bates / depo Ex. numbers |
| `transcript_lines` + designations (page:line) | June 15/16 depo build | Depo-clip impeachment + read-along |
| LiveKit pop-out viewer | prior video work | Video depo clip playback to displays |

---

## 3. The gap we're closing vs. Sanction / TrialDirector

| Capability | Sanction / TrialDirector | Praesidium Trial |
|---|---|---|
| Display routing | One source mirrored to all (HDMI matrix) | **Independent per-display routing by role** |
| Blank a specific display | Hardware/manual | **Per-role blank/hold**, software, instant |
| Witness-preview before publish | Awkward / separate monitor | **"Show witness only"** then publish to jury on command |
| Judge control | None | **Judicial override** — tribunal console can blank jury on a sustained objection |
| Record assembly | Import everything from zero | Exhibit queue **is** the indexed Trial Exhibits register |
| Exhibit log | Manual / export-and-paste | **Self-writing immutable session audit** → trial exhibit index |
| Markup | Destructive-ish, per-file | **Named annotation sets**, ephemeral vs. saved, non-destructive |
| Impeachment | Manual side-by-side | **Exhibit ⟷ depo clip** published together, clip is page:line-resolved |
| AI | None | **Issue-map-driven callout + designation surfacing** |
| Display hardware | Proprietary boxes | **Any browser via QR** — tablet, PC+monitor, Statio node |

---

## 4. Data model (net-new)

Live routing state stays **in-memory** (single appliance, same as depot). But the **audit trail must persist** — the patent constraint is "every display event logged in the session audit," and provenance-is-sacred means the trial exhibit log is a record, not a UI artifact. Two tables:

**`trial_presentation_sessions`**
- `id uuid pk`, `tenant_id char(36)` (TRIM), `matter_id uuid`
- `title text`, `kind text` (`trial` | `hearing`), `status text` (`active`|`ended`)
- `exhibit_numbering_start int`, `created_by uuid`, `started_at`, `ended_at`
- `config jsonb` (role→default-publish map, courtroom label)

**`trial_presentation_events`** (immutable append-only — the self-writing log)
- `id uuid pk`, `session_id uuid fk`, `tenant_id char(36)`
- `event_type text` (`push`|`blank`|`hold`|`annotate`|`mark_exhibit`|`clip_play`|`judge_override`|`witness_preview`|`display_join`|`display_leave`)
- `document_id uuid null`, `exhibit_label text null`, `page int null`
- `target_roles text[]` (which displays this event addressed)
- `actor_id uuid` (who), `actor_role text`
- `payload jsonb` (annotation set id, clip span page:line + timecode, objection ruling)
- `occurred_at timestamptz default now()` — **never updated, never deleted**

**`trial_annotation_sets`** (saved, non-destructive markup)
- `id uuid pk`, `tenant_id`, `matter_id uuid`, `document_id uuid`
- `name text` (per witness / per topic / per argument), `created_by uuid`
- `annotations jsonb` (callouts, arrows, highlights, zoom regions — percent coords matching the viewer convention), `is_locked bool`

Reuse for exhibit identity: trial designations (PX-23 / DX-14) land in **`document_identifiers`**, not a new column — one document, many identifiers across contexts. Exhibit status (offered/admitted/withdrawn) + objection ruling already modeled on the Trial Exhibits surface (June 16) — **the push engine reads that, doesn't duplicate it.**

Migration = one Alembic revision, three tables, off the current head (confirm R5). Capture as migration only — no raw-SQL drift.

---

## 5. Backend — generalize the relay + add role routing

Target file: shared `presentation_ws.py` (or sibling `trial_presentation_ws.py` per R1 outcome).

- **Session create** `POST /api/v1/present/sessions` gains `kind`, `matter_id`, `exhibit_numbering_start`, `config`. On `kind='trial'` it writes a `trial_presentation_sessions` row.
- **Per-display role.** Each WS connection carries a `role` (from the QR/token — see §10). Server keeps `session.displays = {connection_id: {role, label, hold:false}}`.
- **Routing primitive.** Presenter actions specify `target_roles` (default = session config's publish set). The relay fans out only to matching connections. This is the whole differentiator — the relay stops being a broadcast bus and becomes a **router**.
- **State object (extended):** `{ document_id, page, zoom, annotation_set_id, ephemeral_annotations, exhibit_label, presenting, clip }` — each display renders the **last state addressed to its role** (so a held/blanked jury display keeps its prior frame or a blank card while counsel-table advances).
- **Every routed action appends a `trial_presentation_events` row** before/with the fan-out (async, but the mark-exhibit write-back is **synchronous** per §7).
- Keep the in-memory dict; add a TODO for Redis pub/sub only if a second web worker is ever introduced (contract is identical — swap backend, clients unchanged, same note as v13.5).

Endpoints added:
- `POST /api/v1/present/sessions/{id}/push` — `{document_id, page, target_roles, annotation_set_id?, clip?}`
- `POST /api/v1/present/sessions/{id}/blank` and `/hold` — `{target_roles}`
- `POST /api/v1/present/sessions/{id}/mark` — sequential numbering + **synchronous DMS write-back** (§7)
- `POST /api/v1/present/sessions/{id}/override` — tribunal role only (§6)
- `GET  /api/v1/present/sessions/{id}/log` — audit → exhibit index export (§8)

All queries: `TRIM(tenant_id)`, `CAST(:param AS uuid)`, `AsyncSessionLocal`.

---

## 6. Display roles + routing matrix (the centerpiece)

Roles (software config, not wiring — S3-007):

`presenter_console` · `counsel_table` · `co_counsel` (remote) · `witness` · `jury` · `tribunal` (judge) · `court_reporter` · `gallery` (optional) · `remote_observer` (Zoom/virtual-cam, Viaticum Connect tie-in)

Behaviors:
- **Explicit push only** (S3-006 invariant): nothing is visible to any non-presenter display until the presenter pushes. No auto-advance, no ambient preview.
- **Witness-preview:** push to `witness` alone ("permission to approach / show the witness") → then one tap promotes the same exhibit to `jury` + `tribunal` + `counsel_table`.
- **Per-role blank/hold:** blank `jury` while arguing admissibility; counsel table and presenter keep working. Classic TrialDirector "hot seat" but software and per-role.
- **Judicial override (S3-007):** `tribunal` console can blank `jury` independent of the presenter (sustained objection / sidebar). Logged as `judge_override`. This is a claim element absent from all prior art.
- **Court reporter display** shows the live, self-writing exhibit log (number, label, Bates, witness, time marked) — exportable as the exhibit index at adjournment.

Routing matrix UI on the Presenter Console: a compact grid of role chips, each toggling live/blank/hold, with the current frame thumbnail per role so the attorney can *see* what the jury sees vs. what the witness sees.

---

## 7. Sequential numbering + synchronous DMS write-back (S3-006)

- Numbering continues from `exhibit_numbering_start` (default: last number in prior sessions for the matter — surfaced from the exhibit register).
- **Mark = synchronous:** the exhibit is not shown as "marked" in the console until the DMS write confirms. Marked exhibit metadata (number, session id, marking attorney, document id, timestamp) writes through to the DMS + `trial_presentation_events`. **Save ≠ export** — marking is the DB event; the exhibit index export (§8) is the separate delivery event.
- Marked exhibits land with their trial designation in `document_identifiers`; the Trial Exhibits register status flips via the existing offer/admit overlay — no parallel state.

---

## 8. Frontend surfaces

**8.1 Presenter Console — Trial Center → "Present" tab**
- Exhibit **queue** sourced from the Trial Exhibits register (party-grouped, admitted/anticipated; respects the closed-record lock). Drag-reorder (same pattern as depo staging).
- **Routing matrix** (§6) with per-role live thumbnails.
- **Confidence monitor:** presenter sees the next exhibit + queued annotation set *before* pushing.
- Push / Witness-preview / Blank / Hold / Mark controls.
- Transcript scroll rail for read-along + clip launch.
- Checkboxes on the **right**; sortable headers; light/dark — design standards apply.

**8.2 Display Client — role-aware `/present/{token}`**
- Zero-chrome, full-viewport, black, auto-reconnect, ping/pong keepalive (inherited).
- Renders only state addressed to its role; blank card on blank/hold; gold "Praesidium — waiting" on idle.
- No auth — token (now role-scoped) is the credential.

**8.3 Annotation/markup layer (§9 detail)** — overlay on the shared `pdf-annotation-viewer.jsx`.

Nav: the **Present** tab is one row in the Trial Center tab set + (if a standalone entry is wanted) one `ui_nav_items` INSERT. Everything-is-data.

---

## 9. Annotation / markup layer (beats both products)

Over the shared viewer (percent-coord overlay, already used by eDiscovery/DMS redaction):
- Tools: callout box, arrow, highlight, freehand, **zoom/spotlight region**, line-redact (for sealed material on the jury feed only).
- **Two modes:** *ephemeral* (live during exam, cleared on next push, logged as `annotate` event) and *saved* (`trial_annotation_sets` — named per witness/topic/argument, switchable mid-presentation without losing work).
- **Non-destructive:** annotations never alter the underlying exhibit bytes (provenance).
- **Named participant layers:** counsel / co-counsel / judge can each have toggleable layers (the `feature_trialdesk_annotation` claim).
- **Per-display routing of annotations:** a presenter callout can go to counsel-table only (work product) or be published to the jury — annotations inherit the §6 routing.

---

## 10. QR / remote-display provisioning (what you asked for, with roles)

Same scan-to-connect as depot, plus role assignment:
1. Presenter Console shows a QR per role (or one QR → device picks/gets assigned a role on the join screen).
2. Scan from iPad / Fire tablet / Chromebox / **PC-attached monitor** (any browser) / Statio node.
3. Device lands on role-scoped `/present/{token}`; server registers `{connection_id, role, label}` and logs `display_join`.
4. Presenter sees the device appear in the routing matrix and can rename/reassign its role live.
5. `display_leave` logged on disconnect; session end blanks all.

Token carries the role claim (or a short server-side role map keyed by token) — **confirm in R1** whether to extend the existing token or add a `?role=` param validated server-side.

---

## 11. Build slices (one component per session)

| Slice | Deliverable | Depends on |
|------|-------------|-----------|
| **0** | Recon gate (§1) + decision: generalize vs. sibling relay | — |
| **1** | Migration: `trial_presentation_sessions` + `_events` + `_annotation_sets`; session create/end API + audit append | R3,R4,R5 |
| **2** | Role-aware Display Client + QR role provisioning (§10) | Slice 1 |
| **3** | Presenter Console "Present" tab: queue from exhibit register + routing matrix + push/blank/hold (§6, §8.1) | Slice 2 |
| **4** | Annotation layer — ephemeral + saved sets, per-display routing (§9) | Slice 3 |
| **5** | Depo-clip playback to displays + impeachment side-by-side (exhibit ⟷ page:line clip) | Slice 3, R6 |
| **6** | Judicial override + witness-preview flow (§6) | Slice 3 |
| **7** | Sequential numbering + synchronous DMS write-back (§7); AI callout/designation surfacing | Slice 3 |
| **8** | Session audit → exhibit index export (§8.2 reporter log → Word/PDF) | Slice 1 |

Slices 1–3 deliver a usable, demoable trial presenter (push, route, blank, mark) — that's the trial-ready MVP. 4–8 are the "exceeds both products" layer.

---

## 12. Patent coverage

| Claim | Element here |
|------|---------------|
| **S3-006 Role-Differentiated Trial Presentation** | Presenter/Observer session, explicit-push invariant, sequential numbering, **synchronous DMS write-back**, immutable session audit (`trial_presentation_events`) |
| **S3-007 Software-Defined Courtroom Network** | Per-display role routing as software config, per-role blank/hold, **judicial override**, QR-provisioned browser display nodes, Statio node target |
| **feature_trialdesk_annotation** | Named participant layers, ephemeral vs. saved sets, per-display annotation routing, non-destructive overlay |
| **NEW (non-provisional)** | Issue-map-driven AI callout/designation surfacing; impeachment mode binding a trial exhibit to a page:line-resolved deposition clip; exhibit queue sourced from the closed-record register rather than imported |

Capture any late-session design insight (esp. the routing-matrix + judicial-override interplay) as non-provisional matter — March 24, 2027 deadline.

---

## 13. Decisions needed (one terse pass)

1. **Relay:** generalize `presentation_ws.py` to serve both depo + trial, or sibling `trial_presentation_ws.py`? (Lean sibling if R1 shows entanglement — don't destabilize depo before this trial.)
2. **Role assignment at join:** one QR + device self-selects role, or one QR per role? (One-QR-per-role is more foolproof in a live courtroom; one QR is faster to set up.)
3. **Gallery/public + remote_observer:** in scope for this trial, or defer the Zoom/virtual-cam (Viaticum Connect) leg to a later slice?
4. **Judicial override:** real feature for this trial (judge actually gets a console), or build the mechanism + log but keep it dark until a judge will use it?
5. **Annotation persistence default:** ephemeral-first (clears on push) or sticky-until-cleared?

Answer 1–2 and Slice 0/1 can start; 3–5 only gate Slices 4–6.
