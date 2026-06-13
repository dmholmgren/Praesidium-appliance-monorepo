# Praesidium Build Brief — LiveKit Recovery + Deposition Modules

**Audience:** Claude Code, running on the appliance (10.10.0.10), scoped to the source repos.
**Author of record:** Dennis Holmgren. **Version:** v1 (2026-06-13).
**Method:** This brief sits on top of `/opt/praesidium-web/.claude/skills/praesidium-method/SKILL.md` and root `CLAUDE.md`. Those rules are not restated in full here; obey them. This brief adds objective, scope, sequence, and acceptance criteria.

---

## 0. Guardrail amendment (READ FIRST)

The method skill and root `CLAUDE.md` rule #7 previously listed **livekit** alongside mail/onlyoffice and `/mnt` data mounts as off-limits to agentic work. That fence is **lifted for LiveKit only**, deliberately, for the scope of this brief (rule #7 and the method skill guardrail line are both amended to match). The discipline that replaces it:

> **Agentic guardrails (amended 2026-06-13):** `livekit` and `livekit-egress` configs/compose/app-layer are **in scope** for agentic edits, under this discipline: (1) commit or branch the relevant tree before editing (all trees are under version control as of v18.3); (2) **never** delete, truncate, or overwrite a recording, transcript, designation, or any provenance/chain-of-custody row — production save ≠ export, and that invariant is absolute; (3) every config/compose change is deploy-and-verify (`docker_logs` immediately after, plus a live probe) before moving on; (4) one component per session. **Still off-limits:** mail data + TLS private keys, onlyoffice, `/mnt/legacy*` and other `/mnt` data mounts (read-only at most), and any destructive ZFS/Docker volume operation without explicit human go.

If you (Claude Code) cannot satisfy the discipline above for a given step, stop and surface it rather than proceeding.

---

## 1. Objective

Bring native conferencing back online and build the deposition surface as two cooperating modules, both instances of the **composite meeting primitive** defined in ChatPrompts v14.9:

- **Module A — Live Deposition View / Exhibit Push.** The *live* half: a depo room where the questioning attorney publishes exhibits to all participants, with each push captured as a matter-attributed record.
- **Module B — Sync + Playback (TimeCoder / DepoView equivalent).** The *preservation* half: bind the certified transcript to the video timeline (page:line → timecode), then a synchronized viewer with designation / counter-designation / clip workflow.

These are not net-new architecture. v14.9 already specifies the Deposition meeting type (deponent briefing, exhibit list **with order**, prior testimony, key topics, court reporter info, location, video widget) and the composite primitive (room + canvas + whiteboard + recording + transcript + chat + dial-in, all correlated rows on one matter). Build into that, do not reinvent it.

---

## 2. Load-bearing constraints for this build (subset; full set in method skill)

- **Verify live schema before writing any query or migration.** `query_schema` first. Do not trust any table/column named in §4 below until you have confirmed what already exists — v14.9 was a spec session and may or may not have created `meetings`/binder tables. Capture any raw-SQL drift as a migration.
- Tenant `986c0fee-1390-43bb-ad28-8cd1db6de53f`, CHAR(36) with trailing spaces — **always `TRIM(tenant_id)`**.
- asyncpg: **never** `:param::type`; always `CAST(:param AS uuid)`; never bind `None` into a CAST.
- §0 canonical-text invariant holds for transcripts: one canonical string per transcript document; `canonical[char_start:char_end] == stored_text` for every line primitive.
- `dms_documents` mixes DMS + eDiscovery; exhibit/transcript ingestion must respect the eDiscovery folder exclusions — but note exhibits *are* matter documents and may legitimately live in DMS folders. Distinguish by folder, not by guessing.
- **Provenance is sacred.** The certified transcript is the legal record (see §3). Nothing in this build may destroy or silently rewrite it.
- Deploy mechanics: bind mount `/opt/praesidium-web → /app` (host edits live for existing files; `docker cp` for new files); clear `__pycache__`; verify the *container* has the change via `read_container_file`; `docker_logs` after every deploy. Frontend builds via `cd /opt/praesidium-ui && ./build.sh` → `static/js/` (no `dist/`).

---

## 3. The litigator invariant (do not get this wrong)

The **court reporter's certified transcript is the legal record.** ASR/Whisper output captured from the LiveKit recording is a *working/fallback* layer only. Designations used in court must reference the **official page:line**, not ASR line numbers. Therefore:

- The pipeline must accept the reporter's deliverable (ASCII / E-Tran `.txt` / `.ptx`) plus the video, and **align** the official transcript to the video timeline.
- The auto-captured recording + ASR transcript is the same-day working copy and a safety net if the reporter's sync is late or absent. It is never substituted for the certified record once that arrives.
- Sync is a *forced-alignment* problem: match the official transcript text against the timestamped ASR timeline, then map official `page:line` → `video_timecode_ms`. Confidence per line is stored; low-confidence lines flag for human review. Never silently emit an unverified timecode as authoritative.

---

## 4. Proposed data model (PROPOSE — verify against live schema first)

Treat these as the *target*. Reconcile with whatever `meetings`/binder tables already exist before generating migrations. Everything-is-data: a designation is a row, a clip is a query over timecoded line primitives.

- `meetings` (composite primitive; may partially exist) — `id, tenant_id, matter_id, meeting_type, livekit_room_id, canvas_id, whiteboard_id, recording_path, transcript_id, chat_log_id, court_reporter, location, scheduled_start, actual_start, actual_end, status`.
- `deposition_exhibits` — `id, tenant_id, meeting_id, matter_id, dms_document_id, marked_number, marked_label, sort_order, published_at, published_by, page_count, content_hash`. One row per push event; `sort_order` is the exhibit list ordering from the binder.
- `deposition_transcripts` — `id, tenant_id, meeting_id, matter_id, source ('certified'|'asr'), reporter_name, format, dms_document_id, page_count, line_count, certified bool, ingested_at`.
- `transcript_lines` (line primitives) — `id, transcript_id, page_no, line_no, speaker, text, char_start, char_end, video_timecode_ms NULL, sync_confidence NULL`.
- `transcript_sync_runs` (alignment provenance) — `id, official_transcript_id, asr_transcript_id, method, coverage_pct, mean_confidence, aligned_at, aligned_by`.
- `designations` — `id, tenant_id, matter_id, transcript_id, party, kind ('designation'|'counter'|'objection'), start_page, start_line, end_page, end_line, purpose, status ('proposed'|'objected'|'overruled'|'sustained'|'admitted'), ruling, ruled_by, created_by, created_at`.
- `designation_clips` — `id, designation_id, start_timecode_ms, end_timecode_ms, clip_path, format, rendered_at`.

Migrations: one per logical group, named per convention, chained off current head. Design standards apply to all new tables/UI: checkboxes on the **right**, sortable column headers, light/dark per-user theme.

---

## 5. Task sequence (one component per session; deploy-and-test before proceeding)

### T0 — LiveKit recovery (BLOCKER; do first)
**Goal:** `praesidium-livekit` healthy and a browser client can join a room. **Plus** determine whether `livekit-egress` exists at all — depo recording is impossible without it, and it is a *separate* service from the SFU.

Diagnostic-first, not config-first:
1. `docker logs praesidium-livekit` (last ~200 lines) — read the actual loop cause before changing anything.
2. Inspect `livekit.yaml`: the `redis:` stanza is the prime suspect (v14.x note: Redis reachability). Single-node LiveKit does **not** require Redis — it's only for multi-node. Either point it at the correct address/password/db (verify against the running redis container + env) **or** remove the redis block for single-node operation. Confirm API key/secret are set and match what the token-minting backend uses.
3. Confirm the container is on the correct docker network and its advertised IP/ports match the nginx front door and the WebRTC UDP range.
4. Check for `livekit-egress` in compose. If absent, that is a **finding to surface** — recording (T2/T3 input) needs it, with access to a recordings path on `saspool` (DMS/bulk), and it must never write into eDiscovery folders.

**Acceptance:** container stays `Up` across a 5-minute watch; `docker logs` clean of restart errors; a token mints and a test client joins a room and publishes a track. Record the root cause in the session note.
**Guardrail:** branch/commit the livekit tree before editing config. Do not touch recordings storage destructively.

### T1 — Live Deposition View + Exhibit Push
**Goal:** From a matter, start a Deposition meeting; participants join the room; the questioning attorney selects an exhibit from the matter's DMS/exhibit list and **publishes** it to all participants, creating a `deposition_exhibits` row.

Scope:
- Backend: meeting create/start endpoints (reuse/extend composite-primitive routes), token minting, exhibit-publish endpoint that writes the row (marked number, sort_order, publisher, timestamp, page_count, content_hash) and broadcasts a `show_exhibit` event over the **LiveKit data channel** (not screen-share).
- Frontend: depo room view (React, build via `build.sh`); a synchronized exhibit pane rendering the pushed document via the existing OnlyOffice/pdf path; an exhibit picker drawn from the binder exhibit list; live "Exhibit N now showing" state for all participants.
- Provenance is the differentiator: the exhibit already lives in the DMS, and the push is a matter-attributed record — not an ephemeral screen-share.

**Acceptance:** pytest over the publish endpoint (row written correctly, tenant TRIM, CAST usage); live curl mints a token; manual: two browser clients in a room, attorney publishes Exhibit 14, both panes update, one `deposition_exhibits` row exists with correct `sort_order` and hash.

### T2 — Transcript ingest + video sync
**Goal:** Ingest the certified transcript (and capture/derive the ASR working copy), then align official `page:line` → `video_timecode_ms`.

Scope:
- Certified transcript ingest: accept ASCII / E-Tran `.txt` / `.ptx`; parse into `transcript_lines` honoring the §0 canonical-text invariant (page_no, line_no, speaker, char offsets). Store raw deliverable as the `dms_document_id`.
- ASR working copy: from `livekit-egress` MP4 → track egress → Whisper/diarization → timestamped `asr` transcript. This is already video-aligned by construction.
- Alignment: forced/sequence alignment between official text and ASR timeline; write `video_timecode_ms` + `sync_confidence` per official line; record a `transcript_sync_runs` row (coverage %, mean confidence). Low-confidence lines flag for review. Never emit an unverified timecode as authoritative.

**Acceptance:** pytest on the parser (canonical invariant holds; offsets round-trip); a sync run over a sample produces ≥ target coverage with per-line confidence; a `transcript_sync_runs` provenance row exists; certified record untouched.

### T3 — DepoView viewer + designations
**Goal:** Synchronized viewer (scroll transcript → video follows; click video → transcript follows). Select a page:line range → create a `designation`; counter-designate; log objections and rulings; render clips.

Scope:
- Frontend viewer over timecoded `transcript_lines`; designation creation as a range; designations/counter-designations/objection-ruling workflow as rows (`designations`); status transitions.
- Clip render: `designation_clips` from start/end timecodes against the source MP4 (via egress/ffmpeg path); store clip + provenance.
- Tables get sortable headers; checkboxes right; theme-aware.

**Acceptance:** create a designation over a known range, render a clip whose duration matches the page:line span; counter-designation and an objection→ruling round-trip as rows; nothing overwrites the certified transcript.

### T4 — Export / interop (decide scope with Dennis; see §7)
**Goal (if greenlit):** export designations/clips to a litigation interchange format (PTX/LEF for TrialDirector/Sanction) so co-counsel and experts can consume them. v1 may keep the viewer internal and defer this.

---

## 6. Out of scope / deferred

- SIP dial-out / "add participant" mid-conference (Asterisk bridge) — deferred; depo v1 is browser + invited participants.
- Multilingual transcript handling (Spanish RV:/De:, German AW:/WG:) — relevant to the eDiscovery threading pipeline, not depo v1.
- Whiteboard/canvas preservation into the meeting object beyond what already exists.

## 7. Open decisions for Dennis

1. **Export format (T4):** ship a PTX/LEF export for TrialDirector/Sanction interop in v1, or keep the viewer internal and defer? (Affects T3 data shapes slightly.)
2. **Certified transcript ingest path:** manual upload of the reporter's deliverable for v1, or wire an intake folder/connector now?
3. **Egress storage location:** confirm the `saspool` path for recordings/clips and that it is outside any eDiscovery folder tree.

---

*Build into the composite meeting primitive. Complexity under the hood, simplicity on screen. Provenance is sacred — the certified transcript is the record.*
