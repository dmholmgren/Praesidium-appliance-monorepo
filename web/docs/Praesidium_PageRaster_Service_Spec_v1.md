# Praesidium Page-Raster Service — Spec v1.0

**Date:** June 18, 2026
**One primitive, three consumers:** trial presentation display · eDiscovery review viewer · review-set prefetch.
**Status:** SPEC — buildable; grounded in live recon (PyMuPDF already in stack via `bates_engine`/`exhibit_sticker`; `doc_layout_tokens` are normalized 0–1; `GET /source-pdf/{doc_id}` already streams `dms_documents`).

---

## 1. Why this exists

Three surfaces independently need "render a document page fast and identically everywhere":
- **Trial display** — the dumb terminal currently injects lorem-ipsum; it must show the real exhibit page, instantly, on any device (Slice 0 recon item).
- **eDiscovery viewer** — pdf.js drags on scanned/image productions and rapid paging.
- **Review-set prefetch** — paging through a batch should never make a reviewer wait.

Build it once as a **page-raster service**: render a page to a WebP image on demand, cache it, serve the cache thereafter, and prefetch by review-set order.

---

## 2. Doctrine fit

- **Layer-3, regenerable.** A raster is a derivative of the document bytes (like an embedding). Bytes live in DMS/eDiscovery; the raster is a **cache/projection, never the system of record.** It can be deleted and rebuilt at any time — zero provenance risk.
- **Lazy, not bulk.** Render only pages a human actually opens (plus bounded prefetch). **Never** pre-rasterize the production corpus — same trap as the chunk/embed folder-exclusion rule. On-view fill means you only pay for what's viewed.
- **Structural-first.** Deterministic render (PyMuPDF); no model anywhere in this path.

---

## 3. Render core

`modules/render/page_raster.py` (new), `render_page(corpus, doc_id, page, rendition, width) -> bytes`:
- Resolve source bytes the same way `/source-pdf/{doc_id}` does (tenant-scoped: `CAST(:d AS uuid)` + `TRIM(tenant_id)`). Corpus ∈ {`dms`, `ediscovery`}.
- `fitz.open(...)`; `page.get_pixmap(dpi=...)` sized to target **width** (default ~1600px display tier); encode **WebP** (fallback PNG for line-art if needed).
- Sync lib → run via `run_in_threadpool` (same as the geometry resolver).
- Returns image bytes + intrinsic page dimensions (for overlay scaling).

---

## 4. Cache

- **Key:** `(tenant_id, corpus, doc_id, rendition, version, page, width)`.
- **`version`** = a hash/marker of the source bytes (e.g., `dms_documents` content hash or mtime, or production rendition id). Re-OCR / new production ⇒ version changes ⇒ new key ⇒ stale images never served. **This is the invalidation mechanism — there is no in-place overwrite.**
- **Storage:** NVMe `datapool` (hot, read-heavy, regenerable):
  `/datapool/cache/page_raster/{tenant_id}/{doc_id}/{rendition}/{version}/{page}_{width}.webp`
- **GC:** LRU sweep by atime + a hard "drop all rasters for doc X / version < N" call on re-render. No DB row strictly required; optional lightweight index table only if we want cache stats/eviction policy in SQL.

---

## 5. Endpoints

- **Lazy fetch (authenticated, review/DMS):**
  `GET /api/v1/render/page/{corpus}/{doc_id}/{page}?rendition=native_pdf&w=1600`
  → cache hit: stream WebP (`Cache-Control: private, immutable`). Miss: render → cache → stream. Enforces the **same authz as `/source-pdf`** (tenant + matter scope). Returns `X-Page-Dims` header for overlay scaling.
- **Prefetch / warm (authenticated):**
  `POST /api/v1/render/prewarm` `{corpus, items:[{doc_id, pages:[..]}], rendition, width}`
  → enqueues **Redis/RQ** background render jobs (bounded concurrency). Used by review-set read-ahead.
- **Trial display fetch (token-scoped, unauthenticated):**
  `GET /present/page/{session_token}/{exhibit_id}/{page}?w=1600`
  → validates the **presentation session token** (not a login), confirms the exhibit belongs to that session's matter, then serves from the same cache/render core. Keeps the dumb terminal unauthenticated while never exposing arbitrary docs.

---

## 6. Access modes (security — do not skip)

Two wrappers over **one** render/cache core:
- **Authenticated** (review viewer, DMS) — full login + matter scope, via the `/source-pdf` authz path.
- **Session-token-scoped** (trial dumb terminal) — the presentation session token is the credential; only exhibits staged in *that* session are reachable. A short-lived signed URL, **not** the authenticated review endpoint.

Never let the unauthenticated display path reach arbitrary `doc_id`s — it is bounded to the session's staged exhibit set.

---

## 7. Overlays unchanged

Redaction / highlight / callout overlays keep using **percent / `doc_layout_tokens` (0–1)** coordinates rendered client-side over the image. The raster is just the base layer; the existing `pdf-annotation-viewer` overlay system sits on top, scaled by `X-Page-Dims`. No overlay rework.

---

## 8. Consumer wiring

- **Trial display** — replace the lorem-ipsum `applyState()` body with an `<img src=/present/page/{token}/{exhibit_id}/{page}>`. Push messages carry `exhibit_id` + `page` instead of simulated text.
- **eDiscovery viewer** — image-first: render the page `<img>` from the authenticated endpoint; keep **pdf.js as fallback** for text-native docs where the selectable text layer matters (in-viewer find/copy), or overlay the OCR text layer if both are wanted.
- **Review-set prefetch** — on opening a set, `POST /prewarm` the next N docs (set order) so "next" is instant. Pairs with §10 of the review-set caching note.

---

## 9. Tradeoffs / caveats

- **Text layer:** a raster has no selectable text. Keep pdf.js for text-native review, or serve OCR text as a positioned overlay if selection/search-in-doc is required on rasterized docs.
- **Deep zoom:** one display-tier image (≈1600px) reads fine; "zoom into fine print" needs a second higher-res tier (`w=3000`) rendered on demand, or tiled (IIIF/DZI) later. Start with two width tiers; defer tiling.
- **Measure first:** confirm the eDiscovery viewer's actual slow stage (PDF transfer / pdf.js parse vs. ES/vector query vs. OCR). Raster wins the first two; it won't touch a slow query.

---

## 10. Build slices

| Slice | Deliverable |
|------|-------------|
| **PR-1** | Render core (`page_raster.py`) + authenticated `GET /api/v1/render/page/...` + datapool cache + versioned key. Wire eDiscovery viewer image-first (pdf.js fallback). |
| **PR-2** | `POST /prewarm` + Redis/RQ jobs; review-set read-ahead. |
| **PR-3** | Token-scoped `GET /present/page/...` for the trial display; swap dumb-terminal lorem-ipsum → real page image. |
| **PR-4** | Second resolution tier (zoom) + LRU GC sweep. |

PR-1 + PR-3 are the trial-cut dependency (real render on the display). PR-1 + PR-2 are the eDiscovery/review win.

---

## 11. Open decisions

1. **Default display width** — 1600px enough for jury monitors + review, or default 2000px? (Bigger = sharper, heavier on WiFi.)
2. **Cache index** — filesystem-only (simplest), or a small `page_raster_cache` table for stats/eviction in SQL?
3. **OCR text overlay on rasters** — needed in the eDiscovery viewer (search/select on scanned docs), or is pdf.js-fallback-for-text-native enough for now?
4. **Version source** — content hash of source bytes (robust, costs a hash) vs. `updated_at`/rendition id (cheap, trust the timestamp)?
