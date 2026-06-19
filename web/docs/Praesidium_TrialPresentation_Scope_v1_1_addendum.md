# Praesidium Trial Presentation — Scope v1.1 Addendum

**Extends:** `Praesidium_TrialPresentation_Scope_v1.md` (read first — §§4,5,6,9 referenced here)
**Date:** June 18, 2026
**Adds:** Presentation Center landing page · drag-to-stage board · annotation toolset matched to TrialDirector/Sanction · saved annotations in the tree · Trial Witness Project auto-stage · right-click present/stage actions · **multi-user + trial-consultant driver model**

---

## A. Competitor toolset — the match-and-exceed checklist

Concrete feature parity target (researched June 18). "Have" = already in the platform; "Build" = this module; "Beat" = our differentiator.

| Capability | TrialDirector | Sanction | Praesidium |
|---|:--:|:--:|---|
| Callout / zoom (rubber-band + pull-out magnified box) | ✓ | ✓ | **Build** (Slice 4) |
| Highlight (translucent) | ✓ | ✓ | **Build** |
| Arrow / line / rectangle / ellipse | ✓ | — | **Build** |
| Freehand draw | ✓ | — | **Build** |
| Text annotation | ✓ | ✓ | **Build** |
| Sticky note | ✓ | ✓ (Case Crafter et al.) | **Build** |
| Redaction (cover-on-feed) | ✓ | ✓ | **Build** (non-destructive overlay) |
| **Tear-out** (excerpt pull, jagged-edge option) | — | ✓ | **Build** |
| Laser pointer | ✓ | — | **Build** (live cursor) |
| Exhibit stamp / sticker (auto-increment, Bates, case info) | ✓ | ✓ | **Have** (`exhibit_sticker_engine.py` / `bates_engine.py`) |
| Per-tool color + opacity | ✓ | ✓ | **Build** |
| Show/hide markups, undo | ✓ | ✓ | **Build** |
| Bulk rotate doc / page; clear all annotations | — | ✓ | **Build** (viewer ops) |
| Side-by-side two exhibits / composite | ✓ | ✓ | **Beat** — N-up composite per display (§B staging) |
| Freeze output while prepping next | ✓ | — | **Have/Build** (per-role hold, v1 §6) |
| Clear / blackout on objection | ✓ | — | **Have/Build** (per-role blank + judicial override, v1 §6) |
| **Save Stage** (pre-built composite screen) | ✓ | — | **Beat** — saved, per-display, multi-user (§B) |
| Sequence / playlist auto-advance | ✓ | ✓ | **Build** (exhibit queue, v1 §8.1) |
| Witness / trial workbook | ✓ | ✓ | **Beat** — Trial Witness *Project*, auto-stages (§D) |
| Video depo clip + synced transcript | ✓ | ✓ | **Have** (`transcript_lines` + LiveKit, v1 Slice 5) |
| Apply markups / export annotations | ✓ | ✓ | **Build** (PyMuPDF burn, on demand) |
| Frameless full-screen second-monitor output | ✓ (freeze/show) | — | **Build/Have** — pop-out display source (§B.1 Type 2), BroadcastChannel |
| Case awareness / indexed record | ✗ | partial (CaseMap) | **Beat** — native unified data model |
| Multi-user live operation | ✗ | ✗ | **Beat** (§F) |

Everything above the "Beat" rows is parity; the Beat rows are why we replace them.

---

## B. Presentation Center — the landing page

The Trial Center "Present" tab opens the **Presentation Center**, three regions:

Two display-source types, both registered as role-assigned tiles in the Rack:

***Type 1 — Networked display (QR/token).*** "Add Display" → generates a QR + role label (judge, jury, witness, counsel, co-counsel, reporter, gallery, presenter-confidence). Scan from any browser device (tablet, PC+monitor, Statio) → it registers and a live thumbnail of *what that display currently shows* appears in the rack. Transport: token WebSocket (v1 §10), unauthenticated.

***Type 2 — Local pop-out display (second monitor).*** "Pop out display" → opens a **frameless, chromeless `window.open()` window** the operator drags onto a second physical monitor (the courtroom cable / jury monitor / external projector) and toggles **full screen** (Fullscreen API `requestFullscreen()`). Where the browser supports the **Window Management API** (`window.getScreenDetails()`), the Rack can auto-place it on a named second screen with one click instead of a manual drag. It is role-assigned exactly like a networked display (assign it "jury" if that's the cable it feeds).
- **Transport: BroadcastChannel** (`praesidium-conference-space`, the depot pop-out channel), so it's same-browser and effectively **instant** — no LAN round-trip, no token. The pop-out subscribes to the channel and renders only the state addressed to its assigned role (same routing as §F/v1 §6).
- Still a real display in every other sense: appears as a Rack tile with Live/Hold/Blank, logs `display_join`/`display_leave` to the audit, receives pushes and per-display composites like any other.
- Reuses the depot's proven `window.open()` + BroadcastChannel pop-out path (v13.5) — the net-new is registering it as a routed, role-bearing display rather than a passive mirror.

Each display tile (either type): role label, source type, connection state, **Live / Hold / Blank** toggle, current-frame thumbnail. This is the provisioning surface; the routing matrix (v1 §6) is its behavior.

**B.2 Staging Board (center) — drag-and-drop onto displays**
- A column (drop zone) per provisioned display **plus** a shared "Bench" column of staged-but-unrouted items.
- Drag any item (exhibit, pleading, saved annotation set, depo clip, photo) from the tree, search, or Bench onto a display column → it's queued/staged for that display, not yet pushed.
- A display column can hold a **composite** (N-up): e.g. zoomed contract excerpt + depo clip + photo on the jury display at once — our generalization of TrialDirector's Save Stage, but per-display and shareable.
- **Push** per column (or "Push all") sends the staged composite to that display; **Save Stage** persists the composite for recall.
- Bench items are the on-deck queue — drag to a display when the moment comes.

**B.3 Editing Tools (overlay) — displays need markup**
- Selecting a staged item opens it in the shared `pdf-annotation-viewer.jsx` with the full annotation toolbar (§C). Markup applies to the staged instance and routes with the push.
- Ephemeral markup (live laser/callout during exam) vs. saved annotation set (§C) — same model as v1 §9.

---

## C. Annotation toolset + save-to-tree + save-to-staging

**C.1 Toolbar** (matches the §A checklist): select/pan · callout-zoom (rubber-band + pull-out box) · highlight · arrow · line · rect · ellipse · freehand · text · sticky note · redaction (feed-only cover) · tear-out (jagged option) · laser (live cursor) · stamp/sticker (reuse engines) · color+opacity picker · show/hide · undo/redo · clear-all · rotate page/doc · **Save as annotation set** / **Apply markups (burn to PDF)**.

**C.2 Saved annotations are first-class objects** (`trial_annotation_sets`, v1 §4):
- **Appear at the bottom of the exhibit/pleadings tree**, nested under their source document (e.g. *Exhibit 14 ▸ "Cross — para 3 callout"*). Sortable, named per witness/topic/argument.
- **Appear in the Presentation Center** as draggable, stage-able items (Bench or directly onto a display) and as pushable objects in right-click menus (§E). A saved annotation set is a routable thing in its own right.
- Non-destructive; switching sets mid-exam keeps all work (v1 §9). Burn-to-PDF is a separate explicit export (save ≠ export).

---

## D. Trial Witness Project → auto-stage

Reuse the **project** primitive (witness-prep projects already exist; June 16 scope places them under the party groups in the Trial Exhibits nav, Trial-Mode = closed-record).

- A **Trial Witness Project** carries: the examination **outline** + an **ordered exhibit list** (project_documents, `role='exhibit'`, sequenced; nesting via `parent_id` from 0023) + any saved annotation sets.
- **Opening the Presentation Center "for" that witness auto-populates the Bench** with the project's exhibits in outline order (and pins the saved annotation sets). One action: witness → staged, ordered, ready.
- The outline rides along as a presenter-confidence rail (visible only on the `presenter` role) so the questioning sequence and the on-deck exhibit move together.
- This is Sanction/TrialDirector's "witness workbook" — but it's a live project object in the unified model, so the same exhibits, designations, and annotations flow from prep → depo → trial with zero re-import.

**Build note:** auto-stage = read the project's ordered `project_documents` + linked annotation sets → seed the session Bench. No new storage; it's a projection of the project into the staging board.

---

## E. Right-click → present / send-to-staging

Context menu on rows in **Trial Exhibits, Discovery, and Pleadings** lists (Trial Center):
- **Present to display ▸** (submenu lists currently provisioned displays by role) → immediate push to that display (logs `push`, v1 §5).
- **Send to staging area** → drops the item on the Bench (no push).
- **Send to display ▸** (stage on a specific display column without pushing).
- For a document with saved annotation sets, a nested submenu offers the clean doc or any set.

Mechanically: the menu actions call the session push/stage API (v1 §5) with `{document_id | annotation_set_id, target}`. Available only when a presentation session is active for the matter; greyed otherwise.

---

## F. Multi-user + trial-consultant driver (the architectural lift)

The depot relay is single-operator. Trial is a team sport: **any connected attorney can push, and a trial consultant can run the board.** This is the one place that meaningfully changes v1's transport.

**F.1 Connection classes on the same session**
- **Console participants** (attorneys, paralegal, trial consultant) — **authenticated** WS (`/api/v1/present/sessions/{id}/console`, session cookie). Can stage, annotate, push, blank/hold, mark.
- **Networked display clients** — **token-only** WS (`/present/{token}`, unauthenticated, v1 §10). Render only.
- **Local pop-out displays** (§B.1 Type 2) — same-browser child windows of a console, fed over **BroadcastChannel** (no socket/token). Render only, role-assigned, audit-logged. Because they ride the console's own session, a multi-machine trial team can each spawn their own second-monitor output locally while sharing one server-authoritative board.

**F.2 Server-authoritative shared state, broadcast to all consoles**
- The session object (Bench, per-display staged composites, routing/hold/blank, current frames) is the single source of truth. Any console mutation broadcasts to **all** consoles (so every attorney sees the same board live) and resolves to display clients as needed.
- Concurrency: **last-write-wins per display cell**, every mutation stamped with `actor_id` + `actor_role` in `trial_presentation_events` (v1 §4). The audit answers "who put that in front of the jury."
- **Soft driver lock (optional, recommended):** a console can "Take the board" → others see "Pat is driving" and their push becomes "request/stage" (queues to Bench) rather than direct-to-jury, preventing two people fighting the jury display. Release returns to co-equal. Default per Dennis = co-equal push; the lock is a courtroom-discipline affordance, not a hard gate.
- **Trial consultant = a console role** with full board control but (config option) no authority to *mark* exhibits into the record (marking stays attorney-gated, v1 §7). They drive; attorneys make the record.

**F.3 Infra reality**
- Single appliance, single web worker → in-memory session dict still works because all consoles + displays hit the same process (same as depot). **But** consoles now subscribe too, so add a console broadcast channel alongside the display one.
- The moment a second web worker exists, this needs Redis pub/sub (contract unchanged — same note as depot v13.5). Flag, don't build yet.
- Audit (`trial_presentation_events`) persists regardless of live-state backend — it's the record.

---

## G. Data model deltas (on top of v1 §4)

- `trial_presentation_sessions.config` jsonb gains `staging` (per-display composites) and `driver_console_id` (nullable, for soft lock).
- `trial_saved_stages` (optional, if Save Stage should persist beyond a session): `id, tenant_id, matter_id, session_id?, name, layout jsonb (items + positions + per-item annotation_set_id), created_by`. If stages are session-scoped only, fold into `sessions.config` instead — **decide in §J**.
- No new exhibit/annotation tables beyond v1 §4; tree placement (§C.2) and witness auto-stage (§D) are projections, not storage.

---

## H. Build slices (revised — supersedes v1 §11 sequencing)

| Slice | Deliverable | Notes |
|------|-------------|-------|
| **0** | Recon gate (v1 §1) + relay generalize-vs-sibling decision | unchanged |
| **1** | Migration (v1 §4 + §G) + **multi-participant** session API: console (auth) + display (token) WS, server-authoritative shared state, audit append with actor | multi-user baked in from the start, not retrofit |
| **2** | Display Rack provisioning + role-aware Display Client + QR (v1 §10 + §B.1) | includes **local pop-out** display source: frameless `window.open()`, Fullscreen API, optional Window Management API screen-placement, BroadcastChannel transport |
| **3** | Presentation Center landing: **Staging Board** (drag-to-display, Bench, composite) + routing matrix + push/blank/hold; **right-click present/stage** (§E) | the demo surface |
| **4** | Annotation toolbar full set (§A/§C.1) on shared viewer; **saved sets in tree + in Presentation Center** (§C.2); Save Stage | parity + beat |
| **5** | Trial Witness Project auto-stage (§D) | depends on Slice 3 board |
| **6** | Depo-clip playback + impeachment side-by-side (v1 Slice 5) | reuse LiveKit |
| **7** | Judicial override + witness-preview (v1 §6) | |
| **8** | Sequential numbering + synchronous DMS write-back (v1 §7) + AI callout/designation surfacing | attorney-gated marking |
| **9** | Session audit → exhibit index export (v1 §8.2) | |

Slices 1–4 now deliver the full multi-user Presentation Center with competitor-parity markup. That is the trial-ready, demo-ready core.

---

## I. Patent additions (beyond v1 §12)

| Element | Why new matter |
|---|---|
| **Multi-operator role-differentiated presentation** | S3-006 described a single Presenter. Co-equal authenticated pushers + a non-attorney *driver* console + per-operator attribution in an immutable session audit is new — document for non-provisional. |
| **Shared drag-to-stage board with per-display composites** | Per-display, multi-user staging surface (vs. single-operator Save Stage) routed by role — new. |
| **Witness-project-driven auto-staging** | Exhibits/outline/annotations flow prep→depo→trial in one object and auto-populate the live board — the cross-phase continuity is the claim, absent from import-based incumbents. |
| **Annotation set as a routable record object** | Saved annotation set lives in the matter tree and is independently pushable/stageable — not a per-file treatment. |

Capture the multi-operator + driver interplay carefully — it is the strongest new claim in this addendum (March 24, 2027 deadline).

---

## J. Decisions needed (terse pass; adds to v1 §13)

1. **Driver model:** co-equal push only, or co-equal **plus** the optional soft "Take the board" lock? (Recommend build the lock — courtrooms need it.)
2. **Trial consultant authority:** can the consultant *mark exhibits into the record*, or is marking attorney-only? (Recommend attorney-only; consultant drives.)
3. **Save Stage persistence:** session-scoped (in `config`) or durable `trial_saved_stages` table recallable across sessions? (Durable is more useful for trial-day reuse.)
4. **Redaction on feed:** cover-only overlay (never alters bytes) — confirm that's the intent vs. ever burning a true redaction. (Recommend cover-only here; true redaction stays in the eDiscovery production pipeline.)
5. **Composite limit:** cap items-per-display (e.g. 4) for legibility on a jury monitor, or unbounded? 

Answers to 1–2 gate Slice 1; 3–5 gate Slice 4.
