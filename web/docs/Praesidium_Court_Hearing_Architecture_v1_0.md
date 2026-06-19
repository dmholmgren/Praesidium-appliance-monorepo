# Praesidium — Court / Hearing System & Calendar Architecture

**Version:** 1.0
**Date:** June 16, 2026
**Status:** Architecture frozen — ground-zero build plan
**Author:** Dennis M. Holmgren (USPTO Reg. No. 54,168)
**Scope:** Court workspace (renamed from Trial Center), hearing lifecycle, dual-calendar topology under Stalwart/CalDAV, AI-guided historical seeding

---

## 0. First principles for this build

- **Primitive-not-chunk.** Hearings, reschedules, and signals are Layer-1 primitives. Embeddings are Layer-3 derivatives consumed during seeding, never the store.
- **Provenance is sacred.** No date is ever overwritten. Every move and every conclusion is stamped to the witness that produced it. The current behavior (calendar sync overwriting start_at) is a provenance violation this build closes.
- **Projection.** Bytes live in DMS. Workspaces (hearing, meeting, deposition, appeal) are queries against the matter database, not storage containers.
- **Everything is data.** The event-type -> workspace mapping is a dispatch row, not hardcoded logic.
- **Structural-first, model-on-residue.** Deterministic typing/extraction first; LLM only on ambiguity. The AI-guided seeding session is the deliberate exception.
- **Three independent witnesses to "when."** No single source is truth for a hearing date. Reconciliation produces the canonical answer with per-source provenance.

---

## 1. Calendar topology (Praesidium as source of truth)

Exchange is deprecated. The Praesidium calendar is authoritative. Stalwart is the transport/serving layer over CalDAV. Devices subscribe (read).

system-derived events (rules / deadline calc / document extraction) -> ai_calendar_events
human-entered events (administrative staff, ratified settings) -> calendar_events
both native to Praesidium -> internal cross-check -> reconciled authoritative view -> Stalwart CalDAV (praesidium-mail) -> device calendars (subscribe / read)

**Patent posture preserved.** Independent Claim 3 (dual calendar + cross-check) recites a system-generated calendar (read-only to humans), a separately maintained administrative calendar, and a cross-check agent — none of it depends on Exchange. The cross-check now runs between two systems wholly under Praesidium control; enablement and the malpractice-defense narrative (TR 11.5) are strengthened.

**Reuse, unchanged:** migration 0037_dual_calendar (calendar_events, ai_calendar_events, calendar_crosscheck_log) and the daily Cross-Check Agent 450. Discrepancy taxonomy extended (see 3).

**Publish path (net-new):** Praesidium -> Stalwart CalDAV. Confirm/enable the Stalwart DAV listener (v0.16 supports CalDAV).

**Inbound rule:** a device-created event written back via CalDAV does NOT become truth directly — it enters as an inbound signal and passes reconciliation. This enforces "new stuff goes through the pipeline."

---

## 2. Typed-event -> workspace projector

A single dispatch maps event_type -> workspace creator. Adding a type is a data row.

| event_type | Workspace | Table | Status |
|---|---|---|---|
| hearing | Hearing workspace | hearings (new) | Build target |
| meeting | Meeting / conference space | meeting_workspaces (exists, calendar_event_id + LiveKit) | Wire dispatch |
| deposition | Deposition prep surface | deposition_transcripts et al. (exists) | AI-guided ingest |
| trial | Court homepage / trial dashboard | projection | Later |
| (room reservation) | — | — | Deferred (no room tables) |

---

## 3. Core primitives (new)

### hearings — durable identity
id (PK, durable hearing identity, NOT the calendar event id); tenant_id (CHAR(36), TRIM); matter_id; current_event_id (FK calendar_events, repointable); hearing_type, judge, courtroom; status, outcome; original_start_at (immutable baseline); current_start_at (denormalized); notice_status (pending | attached | reconciled | oral_no_notice); notice_document_id (FK dms_documents, nullable); transcript_doc_id; confidence; provenance.

oral_no_notice is a deliberate terminal state (set from the bench) carrying who/why — silences the daily alert. The split between pending and legitimately-none keeps the alert credible and prevents alert fatigue.

### hearing_reschedules — append-only move log
id, hearing_id, tenant_id; sequence_no; from_start_at, to_start_at, delta_days; source (notice | order | exchange | time_signal | manual); source_document_id (FK dms_documents); source_time_entry_id (FK time_entries); moving_party, reason; confidence, detected_at.

"Reset N times" = COUNT(*) per hearing_id. Timeline = ordered rows, each linked to its source.

### hearing_signals — evidence ledger (keystone of the backfill)
id, tenant_id, matter_id; signal_type (notice | order | exchange_event | time_entry | email); source_ref; candidate_date, party; match_score; resolved_into_hearing_id (nullable); collected_at.

All evidence lands here BEFORE any hearing is concluded. Makes the reconstruction auditable, idempotent, re-runnable.

### Crosscheck taxonomy extension
Add to calendar_crosscheck_log.discrepancy_type: hearing_missing_notice (hearing in pending), unmatched_notice (a hearing_notice doc not linked). The daily agent's clear-unresolved-then-rewrite behavior already IS "reflag daily until resolved." Severity escalates by proximity.

---

## 4. Shared core (used by both the live loop and seeding)

1. **Persisted classifier.** classification_results is currently empty — fix first: every decision written, reviewable, correctable.
2. **Notice / order extractor.** Given a court document: hearing_type, new date, prior date (if a reset), moving party. One extractor serves reschedule capture, transcript routing, and the trial dashboard.
3. **Reconciliation resolver.** Bidirectional matcher: a hearing seeking its notice, an orphan notice seeking its hearing. Cause number, matter, court/judge, date-in-doc vs date-on-calendar, parties. Confidence-scored, attorney-confirmable, corrections never overwritten.

---

## 5. Forward ingestion pipeline (documents moving in)

doc arrives (DMS drop | e-file | append-at-hearing | Stalwart email) -> classifier (persisted) -> if court setting/notice/order: notice extractor (dates, parties) -> reconciliation resolver -> match hearing or create pending -> write hearing_signals + hearing_reschedules -> dual-calendar cross-check -> daily alert (reflag until resolved) -> reconciled calendar published to Stalwart CalDAV.

- Append-at-hearing is a DMS entry point (writes canonical, links notice_document_id, runs extractor, flips notice_status=attached, resolves the open alert).
- Email on the box: turn on the email chunker against Stalwart so email-borne notices become document signals.

---

## 6. AI-guided backfill seeding (dedicated session)

Historical reconstruction is interactive, LLM-guided — not a silent batch. Launched from a button (matter edit modal). Collectors + hearing_signals ledger run underneath; the LLM presents triangulated proposals and the attorney ratifies.

Witnesses (Exchange now one-time only):
| Witness | Source | Knows | Blind to |
|---|---|---|---|
| Document vector DB (1.32M chunks) + structural | dms_documents | what the court ordered | hearings whose notices you never got |
| Time-record vector base (2,832 chunks) | time_entries | what consumed attorney time (proof of occurrence) | unbilled / future settings |
| Legacy Exchange snapshot (482 rows) | exchange_import | last scheduled state at cutover | one-time only; source then dead |

Session flow (per matter): collect -> hearing_signals; cluster by hearing_type + temporal proximity + party (one cluster = one hearing); ordered distinct dates = reschedule chain (earliest = original_start_at); LLM narrates, flags single-source dates, asks targeted confirm/correct; on ratify commit hearings + hearing_reschedules; corrections locked.

Rollout: matter-by-matter, on demand. Prove on Victory (known history) first. Anthropic adapter (BYOK).

---

## 7. Known gaps / prerequisites

| # | Gap | Impact | Fix |
|---|---|---|---|
| 1 | classification_results empty | cluster on untrusted types | persist classifier decisions |
| 2 | party_role_id 100% NULL (7,064 rows) | moving_party unknowable | party-role resolver |
| 3 | calendar 0/482 matter-linked, event_type NULL | nothing recognizes a hearing | matter-link + hearing classifier |
| 4 | email_chunk_embeddings = 0 | email-borne notices invisible | email chunker on Stalwart |
| 5 | Stalwart CalDAV listener | publish path | verify/enable DAV |
| 6 | structural party extractor ~39% carry, 55% Haiku | cost + recall | v3.1 precision pass (deferred) |

---

## 8. Build order

1. Migrations — hearings, hearing_reschedules, hearing_signals; crosscheck taxonomy ext.
2. Shared core — persist classification -> hearing classifier + matter-link -> notice extractor -> reconciliation resolver.
3. AI-guided seeding — collectors -> ledger -> clustering -> LLM session + ratify UI.
4. Forward loop — append-as-entry-point, live reconciliation, crosscheck alert extension, email chunker.
5. CalDAV publish — Praesidium -> Stalwart; inbound write-back as signal.
6. Court homepage / hearing workspace — projection; "Reset N times" badge.
7. Meeting dispatch + trial dashboard — wire meeting projection; pleading/discovery nesting (separate primitive, later).

---

## 9. Patent notes (Series 4 candidates)

- Multi-source temporal reconstruction of a litigation event chain from heterogeneous vector corpora (documents + time records), cross-corroborated, with an auditable evidence ledger.
- Time records as an independent occurrence witness for calendar reconstruction.
- AI-guided interactive database seeding with human-in-the-loop ratification.
- Dual-calendar claim (Independent Claim 3) unaffected by Exchange deprecation; CalDAV publication is an implementation detail of the calendar service interface.

---

## 10. Next session — AI-guided seeding (scoped)

- Migration 1 (the three primitives + taxonomy extension) lands first.
- Then the seeding session: collectors (doc-vector + structural, time-vector, one-time exchange), hearing_signals writer, clustering, LLM ratify loop. Target matter: Victory.

---

## Document control

| Field | Value |
|---|---|
| Document | Praesidium_Court_Hearing_Architecture_v1_0.md |
| Version | 1.0 |
| Date | June 16, 2026 |
| Tenant | HJMM 986c0fee-1390-43bb-ad28-8cd1db6de53f |
| Alembic head at freeze | confirm alembic heads before numbering migration 1 |
| Patent | Pending — 64/015,486 + 64/020,027 + 64/033,333 + Series 4 |
| Inventor | Dennis M. Holmgren, USPTO Reg. No. 54,168 |

*Patent Pending — Confidential — Attorney Work Product*
