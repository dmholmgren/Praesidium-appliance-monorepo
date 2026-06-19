# Praesidium Redline Engine — Build Scope v1

**Date:** 2026-06-15  **Owner:** DMH  **Status:** scoped, ready for build chat
**Validated this session in sandbox:** `redline_align.py` (two-version aligner), `redline_tier2.py` (OOXML tracked-changes parser + word diff + similarity gate). Regression corpus: Dallas Portfolio PSA (seller 042822 vs buyer 5/4/22).

---

## 0. What is settled (do not relitigate in build)

1. **Detection is deterministic, end to end.** Block alignment -> per-pair word diff (difflib) -> similarity gate. Proven on a real negotiated PSA: 285 unchanged / 60 word-edits / 5 rewrites / 15 deletes / 112 inserts; word-level edits are clean and draftable on genuine negotiated changes.
2. **All residual noise is structural, never diff quality.** Heading-merge segmentation, signature blocks, exhibit fill-ins, and table cells get mistaken for clauses. Fix: align on the v18.2 section spine (typed sections + boilerplate skip list), not raw w:p boundaries.
3. **The firm's saved redlines carry no OOXML revision markup (3 for 3, formatting-based).** Production path = two-version source alignment (version A vs version B in the DMS), NOT parsing a coarse engine's w:ins/w:del. The OOXML parser path survives only for externally-supplied pre-marked redlines (R5).
4. **Tier-3 model reality (V100, checked live).** Resident now: modernbert-embed (encoder, cannot generate). Generative local = qwen2.5:7b-instruct (ollama, cold). Qwen is a competent describer and an unreliable judge (party-favored call backwards on 1 of 4 real items). Policy: Qwen drafts descriptions; party/materiality calls escalate to Claude or flag for review. Cap num_ctx ~2-4K so it fits on-GPU beside the embedder.

---

## 1. Architecture (tiers)

- Tier 0  extract canonical + section spine (+ geometry tokens if PDF)   [reuse]
- Tier 1  SPINE ALIGNMENT  - anchor identical sections, pair changed by similarity, skip boilerplate  [R1]
- Tier 2  WORD DIFF + GATE - difflib per pair -> op list; gate edit vs replace_block  [R2]
- Tier 2.5 GEOMETRY ANCHOR - resolve_boxes_by_text per op -> page boxes (PDF)  [R3]
- Tier 3  AI CHARACTERIZE  - Qwen describes; Claude judges/escalates; clause memo  [R4]
- Outputs: (a) tracked-changes .docx (save-back to DMS) [R2]; (b) viewer overlay [R3]; (c) page-cited summary [R4]

Invariants: detection never touches a model; resolve-don't-transcribe; cheapest-sufficient processor first; align on the structural substrate, not Word paragraph marks.

---

## 2. Op-list schema (contract between tiers)

RedlineOp: run_id, section_id, seq, kind(equal|insert|delete|replace|replace_block), old_text, new_text, sim, page_anchor(json|null R3), route(none|local|escalate R4), ai_note(R4), favors(Buyer|Seller|Neutral|Review R4).
Gate: per pair, token-similarity < 0.60 -> replace_block (route=escalate); else word-level ops. Thresholds are config.

---

## 3. Build sequence (one component per session)

- R1 - Spine alignment core (first build)
- R2 - Word diff + gate + op-list persistence + tracked-changes output + save-back
- R3 - Geometry anchoring + viewer overlay + quick-redline UI zone
- R4 - Tier-3: Qwen describer (capped ctx) + Claude escalation + clause-grouped summary + AI-redline UI box
- R5 - Phrase-coalescing polish + externally-supplied pre-marked redline path (port redline_tier2.py)

NOTE (merge decision 2026-06-15): DMH directed R1+R2 merged into one deploy.

---

## 4. Unit R1+R2 (merged) - Spine Alignment + Op List + Output

Read-state first: confirm spine table/columns (migration 0040_canonical_spine, section_router.py), canonical source per version, comparison_engine.py integration point, embed endpoint, and the OnlyOffice document-server role for tracked-changes output.

Spec:
1. extract_spine_blocks(tenant_id, doc_id) -> [SpineBlock{section_id, type, text, char_start, char_end}]; drop boilerplate types.
2. align_spine(A,B) -> [pair{kind,a,b}] (difflib anchors + greedy similarity pairing; PAIR_GATE=0.50 config).
3. word diff + gate per edited pair -> op list (schema sec 2).
4. persist redline_runs + redline_ops.
5. render tracked-changes output (decision: hand-rolled OOXML vs OnlyOffice document builder - see risks).

Acceptance (Dallas Portfolio): 60 edits + 5 rewrites preserved; signature/exhibit/table scaffolding no longer standalone insert/delete; McCutchin heading-merge phantoms gone; deterministic.

Deployment conventions:
- App code bind-mount: /opt/praesidium-web/ -> /app (host edits live; docker cp only for new dirs; never docker compose up --force-recreate).
- TRIM(tenant_id) everywhere (CHAR(36)); asyncpg CAST(:param AS type) never ::type.
- Alembic to core/db/migrations/versions/; check alembic heads before numbering; GRANT new tables to praesidium_db.
- HJMM tenant: 986c0fee-1390-43bb-ad28-8cd1db6de53f. Stage scripts via deploy_tmp_script (host /tmp/, never auto-run).

---

## 5. Risks / open decisions

- Tracked-changes output: praesidium-onlyoffice (OnlyOffice DS 8.3) is running - prefer its document builder / editor over hand-rolled OOXML ins/del. DECIDE in build.
- Tabular clauses: PSAs carry 9 tables; confirm spine typing of tables; may need row/cell-keyed align (R1b).
- Embedding-assisted pairing: keep only if it measurably beats text-ratio on Dallas.
- Section 0 seam: until patch_ingest_canonical_pdf applied, geometry boxes via resolve_boxes_by_text bridge (R3).
- Persistence: new redline_runs + redline_ops keyed to op schema.

## 6. Test corpus
- Dallas Portfolio seller/buyer (primary regression).
- McCutchin South seller/buyer (segmentation edge case).
- tier1_redline.docx synthetic OOXML fixture (R5 path).
