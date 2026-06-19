# Praesidium Redline — Faithful Renderer (OnlyOffice review path): Code Build Work Order v1

Date: 2026-06-15  Owner: DMH  Mode: autonomous/background Claude Code on the live appliance
Prereq context: /app/docs/Praesidium_Redline_Engine_BuildScope_v1.md (architecture + op-list schema).

## 0. Probe findings that set this scope (verified live 2026-06-15 — re-verify in Phase 0)
- documentbuilder CLI is NOT installed in praesidium-onlyoffice (onlyoffice/documentserver:8.3). Do NOT scope around a headless OnlyOffice document-builder; it isn't there.
- JWT is enabled on the DS (JWT_ENABLED, JWT_SECRET, JWT_HEADER set). Every conversion/command call must be JWT-signed with the secret from container env.
- DS has no published host ports — internal docker network only (via praesidium-nginx / container name).
- MECHANISM DECISION: OnlyOffice does NOT author tracked revisions for us. We author them by splicing w:ins/w:del into the genuine source .docx (preserves all formatting because we edit the real document). OnlyOffice is the review surface (display + human accept/reject — already the DMS editor) and the converter (docx<->pdf, JWT-signed). We own detection + revision placement; the document server owns rendering/review.

## 1. Deliverable & boundary
render_faithful_redline(source_docx_path, op_list, author) -> staged tracked-changes .docx that: (1) preserves source formatting (styles, numbering, tables, headers/footers); (2) carries real OOXML revisions (w:ins/w:del) matching the op list; (3) stages into existing .tmp/redlines/ and is promotable via existing .../compare/{id}/save; (4) opens cleanly in the OnlyOffice DMS editor with working accept/reject.
Input contract = the op list (BuildScope sec 2). Renderer is decoupled from the spine engine via this seam: build & verify against a FIXTURE op list (Dallas Portfolio); wire to live engine later.
Out of scope: spine-alignment engine, geometry/PDF overlay (R3), AI tier (R4), the OnlyOffice docbuilder route.

## 2. Operating rules (autonomous, LIVE appliance — non-negotiable)
- Additive only. Do not modify/risk the existing WmlComparer/LibreOffice path. Add faithful renderer as a NEW compare method (method="spine-faithful") behind an explicit flag; default unchanged.
- Idempotent + guarded. shutil.copy2 timestamped backup before editing any existing file; exact-anchor patchers, assert count==1, abort-on-mismatch, py_compile gate after each edit.
- Never docker compose up --force-recreate. Code at bind mount /opt/praesidium-web -> /app (edits live); docker cp only for new files/dirs.
- No writes to any matter DMS folder during build/test. Use .tmp/redlines/ staging and the fixture only.
- DB: TRIM(tenant_id) (CHAR(36)); asyncpg CAST(:p AS type) never ::type; new tables GRANT TO praesidium_db; Alembic additive+guarded (statement-by-statement IF NOT EXISTS, per 0040_canonical_spine); check alembic heads before numbering.
- Stop-and-report on any spike-gate failure. Do not force a fragile path. Write findings to /app/docs/.
- Running change log; JWT secret read from env at runtime, never written to disk/logs.

## 3. Phase 0 — Read-state & verify (trust nothing blind)
- document_sections columns: id, section_index, section_type, section_label, content, char_start, char_end, page_start, page_end, superseded_by_run_id, dms_document_id, logical_document_id; CORPUS fk map; sec0 invariant (canonical[char_start:char_end]==content).
- comparison_engine.py: POST /compare (stages .tmp/redlines/, nightly purge), POST .../compare/{id}/save (promote), _run_compare/_run_wml_compare/_run_lo_compare, _convert_to_docx, _redline_staging_dir, CompareRequest/SaveRedlineRequest. Find exact insertion point for a new method.
- How a storage_path resolves to a real .docx (/mnt/praesidium, /mnt/legacy* mounts).
- OnlyOffice: confirm container/network, JWT secret env var, conversion endpoint, and OnlyOffice's existing role as the DMS docx editor (review surface to reuse). Re-confirm documentbuilder absent.

## 4. Phase 1 — SPIKE (go/no-go gate): splice a valid revision into a real source docx
On seller.docx, prove the full loop for one insertion and one deletion:
1. Locate target text in the source docx run structure (see sec 9 crux); split runs at boundary.
2. Wrap inserted text in <w:ins w:author w:date>; deleted text in <w:del>...<w:delText>. Ensure revisions are valid.
3. Re-zip; assert output (a) valid docx, (b) contains new w:ins/w:del, (c) preserves surrounding formatting.
4. Open result in the OnlyOffice DMS editor; confirm tracked-change display + accept/reject; confirm JWT-signed conversion (docx->pdf) succeeds.
GATE: all four pass -> proceed. If run-boundary splicing cannot be made reliable (esp. inside tables), STOP and report; fallback = already-validated self-contained FLAT renderer (rebuild from op-list text, formatting lost) as interim.
(Optional, only if sec 4 fails and time allows: add onlyoffice/documentbuilder image and test whether its Office API can author revisions. Research, not primary.)

## 5. Phase 2 — Renderer module
render_faithful_redline(source_docx_path, op_list, author):
- Iterate ops in document order. Non-equal ops: insert->w:ins; delete->w:del/w:delText; replace->w:del old + w:ins new; replace_block->block-level del+ins.
- Resolve-don't-transcribe: inserted/deleted text comes verbatim from the op (old_text/new_text) from the deterministic diff. Renderer only places markup — no model, no paraphrase.
- Preserve all source formatting (the entire point vs self-contained). Splice at run boundaries; split runs, never rewrite paragraphs wholesale.
- Stamp w:author, w:date. Stage to _redline_staging_dir(tenant_id); return redline_id consistent with the save endpoint.
- Register as method="spine-faithful" in _run_compare; default untouched.

## 6. Phase 3 — Persistence (create IF absent; engine build may add these)
- redline_runs (id, tenant_id, matter_id, source_doc_id, revised_doc_id, method, status, created_at) + redline_ops (op schema, FK run_id). Additive guarded migration; GRANT TO praesidium_db.

## 7. Phase 4 — Acceptance / self-verification (do not mark done if any fail)
- Fixture: Dallas Portfolio op list. Provide a generator running the validated align->diff->gate over seller.docx->buyer.docx (or checked-in dallas_ops.json). Render source = seller.docx.
- Verify: (1) w:ins/w:del count>0, authored correctly; (2) formatting preserved — table count matches source (9), numbering/styles intact on spot-check; (3) changes correspond to op list — spot-check 5: CIRCLE->CI, Prairier->Prairie, deleted tax-assessment tail, earnest-money reword, one replace_block; (4) stages and .../save promotes a copy; (5) opens in OnlyOffice with accept/reject; (6) idempotent re-run.
- On any failure, write diagnosis to /app/docs/; do not declare deployed.

## 8. Phase 5 — Report
Write /app/docs/Praesidium_Redline_OnlyOffice_BuildReport_<date>.md: mechanism chosen, renderer API, new compare method name, migration revision, acceptance results, known limitations (esp. table splicing), deferrals — so the next interactive session resumes cleanly.

## 9. Risks / honest unknowns
- THE CRUX — op anchor -> source-docx location. Op list is keyed to document_sections over the CANONICAL text; source .docx run structure does NOT line up with canonical offsets (same class as the sec0 seam). Renderer must locate each op's old_text in the source docx by TEXT-ANCHORED match (analogous to resolve_boxes_by_text), not by trusting offsets. Main engineering + most likely failure point — budget for it.
- Table splicing is the fragile case; revisions inside w:tbl cells need run-split inside w:tc. If unreliable, mark table-internal changes as block-level del+ins of the cell and note the limitation.
- JWT must be signed correctly or DS conversion calls 401 — verify in Phase 0.
- No documentbuilder — do not rely on it; OnlyOffice is review/convert only.
- Determinism: detection/placement stays model-free; this build adds no AI (that is R4).
