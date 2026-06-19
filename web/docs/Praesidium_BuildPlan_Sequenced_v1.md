# Praesidium — Sequenced Build Plan v1

**Date:** June 16, 2026
**Purpose:** Dependency-ordered build sessions with parallelization map and ready-to-paste chat prompts for Claude Code.
**Companion specs:** `Praesidium_Court_Hearing_Architecture_v1_0.md` + `_v1_1_addendum.md`
**Inputs already on the appliance** (each session reads its source scope, don't re-scope):
`Praesidium_Trial_Center_Scope_v2.md`, `Praesidium_AppellateRecord_Buildout_2026-06-16.md`, `Praesidium_AppellateHomepage_FrontendHandoff_v1.md`, `praesidium_unified_ingest_handoff.md`.

---

## Global rules (apply to every session)

- **Session protocol:** read `/mnt/skills/user/praesidium-dev/SKILL.md` -> read the latest ChatPrompts delta (currently **v18.5**, check for newer) -> read live state via Appliance Admin MCP before proposing -> one component, deploy-test-verify -> emit a ChatPrompts delta at close.
- **Migrations NEVER run in parallel.** Single Alembic head chain. Every `*_migration` session is serialized through one agent. Always `alembic heads` before numbering (chain is past `0104`).
- **Parallel Claude Code instances require the MCP `stateless_http=True` patch first** — concurrent sessions cross-talk the response queue. That patch is **Session 0**.
- **No two parallel sessions edit the same file** (esp. `matter-dashboard.jsx`, `layout_tabs` seeds, the migration chain).
- DB discipline: `TRIM(tenant_id)`, `CAST(:p AS uuid)`, exclude eDiscovery folders from DMS pipelines, supersede-not-delete.

---

## Dependency graph

```
S0  MCP stateless patch ──────────────► (enables parallel lanes)

P1  migration: hearing primitives ─┐
P2  persisted classification       ├─ shared prereqs (P1 serial; P2/P3/P4 parallel-OK)
P3  shared matter resolver         │
P4  six-tab shell confirm          ┘
        │
        ├──────────────► LANE A (Hearing/Calendar)   A1->A2->A3->A4->A5->A6->A7
        ├──────────────► LANE B (Email/PST)          B1->B2->B3->B4
        ├──────────────► LANE C (Court surfaces)     C1->{C2,C3,C4}->C5
        ├──────────────► LANE D (Depositions)        D1->D2->D3->D4
        ├──────────────► LANE E (Appellate)          E1->E2->E3->E4
        ├──────────────► LANE F (Matter drill-down)  F1
        └──────────────► LANE G (Meetings)           G1

Cross-lane consumers:
  A4 (seeding) is richest AFTER B4 + D1/E1 embeddings (run with what's available, re-run later)
  C4 (hearing transcripts tab) consumes Lane A hearings
  C1 / E1 coordinate the record chunk store (open item #2)
  F1 reads module tabs (build placeholders first, fill as lanes land)
```

**Run-together groups (post-S0):** {P2, P3, P4} . then {A1, B1, C1*, D1, E1, G1} . then the rest of each lane in order. (*C1 is a migration -> serialize with P1/other migrations.)

---

## SESSION 0 — MCP stateless patch
**Goal:** patch `praesidium-mcp` to `stateless_http=True`; restart to flush crossed queues. Prereq for any parallel work.
**Chat prompt:**
> Continue Praesidium. Read the dev skill and latest delta. Patch the MCP server to `stateless_http=True` so concurrent sessions don't cross-talk the response queue, `docker restart praesidium-mcp`, and verify two simultaneous sessions stay isolated with the sentinel-echo test. Emit a delta.

---

## SHARED PREREQUISITES

### P1 — Migration: hearing primitives *(migration — serialize)*
**Deliverable:** `hearings`, `hearing_reschedules`, `hearing_signals`, + `calendar_crosscheck_log.discrepancy_type` extension (`hearing_missing_notice`, `unmatched_notice`). Schemas in Architecture v1.0 §3.
**Chat prompt:**
> Continue Praesidium per the dev skill + latest delta. `alembic heads` first. Create one migration adding `hearings`, `hearing_reschedules`, `hearing_signals` (columns per Court_Hearing_Architecture v1.0 §3) and extending `calendar_crosscheck_log.discrepancy_type` with `hearing_missing_notice` and `unmatched_notice`. Grants to `praesidium_db`. Verify with a follow-up SELECT. Emit a delta.

### P2 — Persisted classification *(depends: none; parallel-OK)*
**Goal:** `classification_results` is empty — type decisions aren't recorded. Persist every classifier decision (reviewable, correctable). Gates clustering/typing everywhere.
**Chat prompt:**
> Continue Praesidium. `classification_results` is empty — confirm where `classify_text` in the extraction path makes its decision and wire it to persist every classification (dms_document_id, document_type_id, method, confidence, model) with a review/supersede lifecycle. Backfill a sample so we can measure accuracy. Emit a delta.

### P3 — Shared matter resolver *(depends: none; parallel-OK)*
**Goal:** one resolver for calendar->matter, email->matter, dms->matter. Consumed by Lanes A, B, F.
**Chat prompt:**
> Continue Praesidium. Build a shared matter resolver (cause number, party names/domains, contacts, attendees), confidence-scored, attorney-confirmable, corrections never overwritten. It must serve calendar_events (0/482 linked), email_segments (email_matter_assignments=0), and dms. Single service both the hearing lane and the email lane call. Emit a delta.

### P4 — Six-tab shell confirm *(depends: none; parallel-OK)*
**Goal:** confirm/generalize the `useNavTabs(layout_slug, default_tab)` + `TabBar` + `layout_tabs` pattern (already live on `matters_home`) as the shared landing shell + a seeding helper for new `layout_slug`s.
**Chat prompt:**
> Continue Praesidium. Confirm the `useNavTabs`/`TabBar`/`layout_tabs` landing pattern and give me a clean helper to seed a new six-tab `layout_slug`. We'll use it for `court_home`, `depositions_home`, and the matter drill-down. No new plumbing — verify the generic widget render path (TR §10) is the panel mechanism. Emit a delta.

---

## LANE A — Hearing / Calendar *(depends: P1, P2, P3)*

- **A1 — Hearing classifier + matter-link on calendar_events** -> creates `hearings` in `pending`.
- **A2 — Notice/order extractor** -> hearing_type, new date, prior date, moving party (needs party-role resolution; flag if blocked).
- **A3 — Reconciliation resolver wiring** -> hearing<->notice bidirectional (uses P3).
- **A4 — AI-guided seeding** -> collectors over doc-vector (1.32M) + time-vector (2,832) + one-time exchange (482) -> `hearing_signals` -> cluster -> LLM ratify UI. **Target matter: Victory** (known history). *Best after B4 + embed lanes; run with available witnesses, re-run.*
- **A5 — Forward loop** -> append-at-hearing DMS entry point + crosscheck alert extension (daily reflag of `hearing_missing_notice`/`unmatched_notice`, severity by proximity). Note `oral_no_notice` terminal state.
- **A6 — Hearing workspace projection** -> typed-event->workspace dispatch; "Reset N×" badge = `COUNT(hearing_reschedules)`; tappable timeline to source docs.
- **A7 — CalDAV publish** -> Praesidium -> Stalwart; inbound device write-back enters as a signal, never direct truth.

**Kickoff prompt (A4, the flagged session):**
> Continue Praesidium per the dev skill + latest delta + Court_Hearing_Architecture v1.0 §6 / v1.1 §A2. Build the AI-guided hearing seeding flow for the Victory matter: signal collectors over the doc vector base (dms_chunk_embeddings) + structural classifier, the time-record vector base (billing_chunk_embeddings), and the one-time exchange snapshot, writing `hearing_signals`; cluster by hearing_type + temporal proximity + party; then an LLM-driven ratify loop (button in the matter edit modal) that narrates each cluster, flags single-source dates, and asks targeted confirm/correct questions before committing `hearings` + `hearing_reschedules`. Re-runnable; corrections locked. Use the Anthropic adapter (BYOK). Emit a delta.

---

## LANE B — Email / PST *(depends: P3; P2 helps)*

- **B1 — Bulk PST collector (tenant admin)** -> pffexport intake -> dms_document(email) -> existing segmenter -> `normalized_hash` dedup; routing tag (firm-archive vs custodian); `pst_import_batches` tracking; idempotent/resumable.
- **B2 — Email chunker ON** -> email_segments -> email_chunks -> email_chunk_embeddings (burst shim/V100).
- **B3 — Email matter resolver run** -> populate email_matter_assignments (uses P3).
- **B4 — Historical email backfill** -> feeds A4 as the email witness.

**Kickoff prompt (B1):**
> Continue Praesidium per the dev skill + latest delta + Court_Hearing_Architecture v1.1 §A2. Build the bulk PST/email collector in tenant admin: upload PST -> pffexport extraction (NOT readpst) -> per-message .eml -> dms_document(folder_category=email) -> existing segmenter (email_segments, normalized_hash dedup against the existing 4,390). Add an upload routing tag (firm mailbox archive vs custodian collection) driving global vs custodian-scoped dedup. Add a `pst_import_batches` tracking table for idempotent, resumable runs. Don't chunk yet — that's B2. Emit a delta.

---

## LANE C — Court surfaces *(per Trial_Center_Scope_v2; depends P4; C1 is a migration)*

- **C1 — Migration: `document_identifiers` + `trial_exhibit_objections`** *(resolve open item: unify vs split `deposition_exhibit_links`/`trial_exhibits` first)* — serialize with other migrations.
- **C2 — Pleadings & Motions tabs** (filtered PDF viewer + exhibit hyperlinks into motion body — extraction already exists).
- **C3 — Trial exhibits 3-panel** (party grouping, collapsible, bulk offered/admitted/objection/withdrawn).
- **C4 — Hearing Transcripts tab** (consumes Lane A hearings + transcript pipeline).
- **C5 — Court six-tab landing** (`court_home`: Pleadings | Motions | Depositions | Hearing Transcripts | Trial Exhibits | Trial Dashboard).

**Kickoff prompt (C5):**
> Continue Praesidium per the dev skill + latest delta + Praesidium_Trial_Center_Scope_v2.md + Architecture v1.1 §A4. Seed the `court_home` layout_slug with six tabs (Pleadings, Motions, Depositions, Hearing Transcripts, Trial Exhibits, Trial Dashboard) using the useNavTabs/TabBar/layout_tabs pattern, and assemble the landing as widget panels. Trial Dashboard surfaces pleadings nested live-vs-mooted and the reschedule history. Emit a delta.

---

## LANE D — Depositions & Transcripts *(per depo scope)*

- **D1 — Depo pipeline** (transcript -> canonical -> Q&A primitives -> ModernBERT-768/ES, ledger_dag + 0064 SKIP LOCKED). *Coordinate record chunk store with E1.*
- **D2 — Transcript viewer** (left-pane selectable list, transcript center, linked exhibits).
- **D3 — Designations / objections + reports** (eliminate TrialDirector export-then-Word).
- **D4 — Depositions & Transcripts six-tab landing** (`depositions_home`: Overview | Transcripts | Exhibits | Designations | Witnesses & Prep | Reports).

**Kickoff prompt (D4):**
> Continue Praesidium per the dev skill + latest delta. Seed `depositions_home` with six tabs (Overview, Transcripts, Exhibits, Designations, Witnesses & Prep, Reports) on the useNavTabs/TabBar/layout_tabs pattern and build the panels; the Transcripts tab is the viewer with a left-pane selectable transcript list, transcript center, and linked exhibits. Emit a delta.

---

## LANE E — Appellate *(per appellate handoffs; independent)*

- **E1 — Record chunk+embed lane** (geometry substrate corpus `record` -> chunks -> embeddings; coordinate store with D1).
- **E2 — Unified record/transcript/exhibit search endpoint** (template off `ai_api.py`).
- **E3 — Appellate six-tab home** (Workspaces & Projects | Matter Intelligence | Reporter's Record | Clerk's Record | Briefing | Billing).
- **E4 — Briefing split-screen + cite-on-mark** (mark in record -> cite at cursor in brief; search record/transcript/exhibits).

**Kickoff prompt (E1):**
> Continue Praesidium per the dev skill + latest delta + Praesidium_AppellateRecord_Buildout_2026-06-16.md. Build the record chunk+embed lane reading from the geometry substrate (doc_layout_tokens, corpus `record`) — NOT dms_documents. Decide the chunk store (dms_chunks source_type=record_document vs dedicated record_chunks) in coordination with the depo pipeline so we don't fork two record-embed lanes. ModernBERT-768, shared vector space. Emit a delta.

---

## LANE F — Matter landing drill-down *(depends P4; reads module data)*

- **F1 — Matter home intelligence drill-down** — noun-first; Discovery/Pleadings/etc. tabs as hierarchical "done/pending" read-outs, scoped projections; typed sub-tabs by `matter_type` (litigation / appellate / transactional). Build placeholders for tabs whose lane hasn't landed; fill as lanes complete.

**Kickoff prompt (F1):**
> Continue Praesidium per the dev skill + latest delta + Architecture v1.1 §A4. Rebuild the matter home as noun-first intelligence drill-down: tabs are scoped read-outs of "what's been done / what's pending," hierarchical, not work surfaces. Type the tab set by matter_type (litigation/appellate/transactional). The Discovery tab shows discovery state for the matter (counts, pending, overdue) drilling into detail — it does not launch discovery work. Use layout_tabs. Placeholder any tab whose backing lane isn't built yet. Emit a delta.

---

## LANE G — Meetings *(small; independent)*

- **G1 — Meeting dispatch** — meeting-type calendar event -> `meeting_workspaces` row (calendar_event_id link, LiveKit). Same projection as hearings. Rooms deferred.

**Kickoff prompt (G1):**
> Continue Praesidium per the dev skill + latest delta. Wire the typed-event->workspace dispatch so a meeting-type calendar event auto-creates a `meeting_workspaces` row (calendar_event_id, workspace_type, LiveKit-backed). Mirror the hearing projection. Room reservations are out of scope. Emit a delta.

---

## Suggested wall-clock ordering

1. **S0** (alone).
2. **{P1} then {P2, P3, P4}** — P1 is the migration; P2/P3/P4 can run as up to three parallel instances once S0 is in.
3. **First parallel wave:** A1, B1, D1, E1, G1, C1(migration—serialize).
4. **Second wave:** A2->A3, B2->B3->B4, C2/C3, D2/D3, E2, F1(placeholders).
5. **Convergence:** A4 (after B4 + embed lanes), A5->A6->A7, C4->C5, D4, E3->E4, F1(fill).

---

## Document control

| Field | Value |
|---|---|
| Document | Praesidium_BuildPlan_Sequenced_v1.md |
| Version | 1.0 |
| Date | June 16, 2026 |
| Latest delta at authoring | v18.5 (verify newer before each session) |
| Alembic chain | past 0104 — `alembic heads` before every migration |
| Patent | Pending — 64/015,486 + 64/020,027 + 64/033,333 + Series 4 |
| Inventor | Dennis M. Holmgren, Reg. No. 54,168 |
