# Praesidium — Court / Hearing Architecture v1.1 (Addendum to v1.0)

**Date:** June 16, 2026
**Read alongside:** `Praesidium_Court_Hearing_Architecture_v1_0.md`
**Status:** Additive — does not change the v1.0 hearing primitives; adds the email/PST intake, the CalDAV publish detail, the landing-page layer, and reconciliation with the module scopes done in recent sessions.

---

## A1. Changelog from v1.0

1. Exchange is a **temporary inbound connector only**, deprecated at cutover. Praesidium calendar is source of truth, published outward via Stalwart CalDAV (v1.0 §1 stands).
2. **New:** bulk PST/email collector in tenant admin (§A2).
3. **New:** email matter resolver as part of the shared matter-resolver family (§A3).
4. **New:** landing-page / `layout_tabs` layer for Court, Depositions & Transcripts, and matter drill-down (§A4).
5. Email becomes a **populated** backfill witness (was assumed in v1.0 §6; now sourced from PSTs).

---

## A2. Bulk PST / email collector (tenant admin)

One new intake feeding the existing email normalization pipeline. The pipeline already exists and is the target shape — do not invent a new one:

```
PST upload (tenant admin)
  -> pffexport extraction            <- NOT readpst (1hr timeout dies on large PSTs)
  -> per-message .eml -> dms_document (folder_category=email)
  -> existing segmenter -> email_segments (thread-split; normalized_hash; sent_date; author; is_top)
  -> email chunker (turn ON; never run) -> email_chunks -> email_chunk_embeddings (V100 / RunPod burst shim)
  -> email matter resolver -> email_matter_assignments
  -> available to AI-guided hearing seeding as a populated witness
```

**Verified live state (2026-06-16):** `email_segments` = 4,390 (~ the 60-day connector pull), thread-aware with `normalized_hash` dedup. `email_chunks` = 0, `email_chunking_jobs` = 0 (chunker never run). `email_matter_assignments` = 0. `ediscovery_collections` = 37, `ediscovery_custodians` = 5.

**Design rules:**
- **Routing tag on upload:** "firm mailbox archive" (-> email intelligence + backfill; **global** dedup on `normalized_hash`, one canonical message + mailbox/folder membership metadata) vs. "custodian collection for matter X" (-> eDiscovery; **custodian-scoped** dedup, identical-across-custodians both retained). Same extraction tech, opposite dedup policy. Build the tag so the collector isn't rebuilt for eDiscovery later.
- **Idempotent + resumable:** new `pst_import_batches` tracking table (extracted / deduped / chunked / embedded counts per PST), so a worker crash resumes rather than restarts. Same pattern as `billing_chunking_jobs`.
- **Dedup against the 60-day pull** is free at schema level via `normalized_hash`.
- **Embedding stays local/burst** (ModernBERT-768, `praesidium-embed` V100 or `praesidium-embed-burst-shim` RunPod, identical checkpoint, cosine 0.999998) — sovereignty + no per-token API rent on a million historical emails.

**Why it matters for the hearing backfill:** email is usually the *earliest and most explicit* reschedule witness ("Judge moved MSJ to the 18th" / "OC agreed to continue"), and often the *only* record of a move that produced no formal notice. Lighting up the email vector base materially upgrades the seeding triangulation, not just its volume.

---

## A3. Shared matter resolver

`email_matter_assignments` = 0, calendar `matter_id` = 0/482, and DMS matter-link all need the same resolver. **Build once, consume everywhere.** Match signals: cause number, party names/domains, known contacts, attendees. Confidence-scored, attorney-confirmable, corrections never overwritten by re-runs. Hearings, PST/email, and the matter drill-down all depend on it.

---

## A4. Landing-page / `layout_tabs` layer

Every landing page is the same pattern — **not** a new plumbing build:
`useNavTabs(layout_slug, default_tab)` + `TabBar`, tabs as rows in `layout_tabs` (unique `(layout_slug, tab_slug)`). `matters_home` already runs 6 tabs this way. Adding/reordering tabs is a DB-only INSERT/UPDATE/DELETE. Widgets/panels follow the `widget_registry` + `layout_registry` generic render path (TR §10). The work for each landing is **panels, not plumbing.**

**Court module landing — six tabs** (per `Praesidium_Trial_Center_Scope_v2.md`):
Pleadings | Motions | Depositions | Hearing Transcripts | Trial Exhibits | Trial Dashboard
(Hearing Transcripts tab consumes the v1.0 hearing primitives; Trial Dashboard surfaces the live-vs-mooted pleading nesting + reschedule history.)

**Depositions & Transcripts module landing — six tabs** (per depo pipeline scope; confirm exact set):
Overview | Transcripts | Exhibits | Designations | Witnesses & Prep | Reports
(Transcript viewer: left-pane selectable list, transcript center, linked exhibits — already scoped.)

**Appellate matter home — six tabs** (already scoped, behavioral branch `matter_type='appellate'`):
Workspaces & Projects | Matter Intelligence | Reporter's Record | Clerk's Record | Briefing | Billing

**Matter landing (noun-first intelligence drill-down):** tabs are *scoped read-outs* of "what's been done / what's pending," hierarchical, not work surfaces. Typed by `matter_type` (litigation / appellate / transactional). The Discovery tab answers state, it doesn't launch discovery work — that lives in the left-nav process workflows.

> Naming to reconcile: this thread renamed **Trial Center -> Court**; a recent chat also introduced a top-level **Trial** module and a separate **Deal Room** top-level (build later). Confirm the final top-level nav set before seeding `ui_nav_items` / `layout_tabs`.

---

## A5. Open items

| # | Item | Note |
|---|---|---|
| 1 | `deposition_exhibit_links` vs `trial_exhibits` | Unify or keep split? (Trial_Center scope open item A) — drives `document_identifiers` backfill source count. |
| 2 | Record chunk store | `dms_chunks(source_type='record_document')` vs dedicated `record_chunks` — coordinate depo + appellate so there aren't two divergent record-embed lanes. Record text lives in the **geometry substrate** (`doc_layout_tokens`, corpus `record`), not `dms_documents`. |
| 3 | Final top-level nav set | Court vs Trial vs Depositions & Transcripts vs Deal Room — lock before seeding nav. |
| 4 | Stalwart CalDAV listener | verify/enable on `praesidium-mail`. |

*Patent Pending — 64/015,486 + 64/020,027 + 64/033,333 + Series 4 — Dennis M. Holmgren, Reg. No. 54,168.*
