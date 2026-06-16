#!/usr/bin/env python3
"""
jobs/spine_chunker.py

The `chunk-from-spine` stage of the unified ingestion loop:

    extract -> segment -> match taxonomy -> [CHUNK-FROM-SPINE] -> embed

Reads the LIVE spine (document_sections) for a document and emits retrieval
chunks, one or more per section, sub-splitting long sections on a size budget at
natural boundaries. Chunk char offsets are inherited from the section and remain
§0-true: chunk.content is sliced straight out of the document's CANONICAL text, so

    canonical[chunk.char_start:chunk.char_end] == chunk.content

holds by construction and is asserted before any write. Because the segmenter
already grounded the section to the same canonical string the geometry tokens
index, a chunk's (char_start, char_end) feed resolve_boxes() directly -> page
rectangles for redaction / spine-highlight. That is the join that closes the loop.

Embedding is DECOUPLED: this writer fills `content` + `embedded_content` (the
text the embedder will consume) but never calls a model. A separate embed pass
populates *_chunk_embeddings once the ModernBERT-768 path is live.

Shared core, per-corpus sink:
    ediscovery -> ediscovery_chunks, governed by chunking_strategies ->
                  ediscovery_document_chunking_runs (run_id FK), with strategy +
                  content fingerprints for cache-skip.
    dms        -> dms_chunks (run_id free).
    (email lane = email_segments -> email_chunks is a separate component.)

Canonical basis per corpus (same column the segmenter + geometry use):
    ediscovery -> ediscovery_documents.extracted_text
    dms        -> dms_documents.content_text

Usage (inside the praesidium-web container):
    docker exec praesidium-web python3 /app/jobs/spine_chunker.py \
        --corpus ediscovery --doc 6c86e216-e97a-46aa-88bc-5d2cc7011e22 --dry-run
    docker exec praesidium-web python3 /app/jobs/spine_chunker.py \
        --corpus ediscovery --doc 6c86e216-e97a-46aa-88bc-5d2cc7011e22
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
import uuid

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

log = logging.getLogger("spine_chunker")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

CHUNKER_VERSION = "spine_v1"
STRATEGY_SLUG = "canonical_spine_v1"
# chunker_kind must satisfy ck_chunking_strategies_chunker_kind; our structural
# section-based splitter maps to 'paragraph'.
CHUNKER_KIND = "paragraph"
# Intended embedder for chunks under this strategy (embed pass is deferred).
EMBED_MODEL = "freelaw/modernbert-embed-base_finetune_512"
EMBED_DIM = 768

# Size budget (chars). ModernBERT context ~512 tok ~= ~2000 chars.
TARGET = 2000
OVERLAP = 200
MINCHARS = 200
# eDiscovery-only: merge runs of small adjacent sections up to ~this many
# chars before chunking (email bodies get segmented per-line, which would
# otherwise yield one micro-chunk per line). 0 disables. Gated to
# corpus=='ediscovery' in process_doc; depositions/DMS are unaffected.
EDISCOVERY_PACK = int(os.environ.get("EDISCOVERY_CHUNK_PACK", "1500"))

PAGE_BREAK = "\f"
_BREAKS = ("\n", ". ", "; ", ", ", " ")

CORPUS = {
    "ediscovery": {"table": "ediscovery_documents", "canon_col": "extracted_text",
                   "fk_col": "ediscovery_document_id"},
    "dms":        {"table": "dms_documents", "canon_col": "content_text",
                   "fk_col": "dms_document_id"},
}


def get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return "host=172.28.0.1 port=5432 dbname=praesidium user=praesidium"
    for p in ("postgresql+asyncpg://", "postgresql+psycopg2://"):
        if url.startswith(p):
            return "postgresql://" + url[len(p):]
    return url


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()  # 64 chars -> char(64)


def _trim(text: str, s: int, e: int) -> tuple[int, int]:
    while s < e and text[s].isspace():
        s += 1
    while e > s and text[e - 1].isspace():
        e -= 1
    return s, e


def chunk_section(content: str, target=TARGET, overlap=OVERLAP, minchars=MINCHARS):
    """Sub-split a section's content into (local_start, local_end) spans whose
    trimmed slices tile the section. Offsets are LOCAL to `content`."""
    spans: list[tuple[int, int]] = []
    n = len(content)
    if n == 0:
        return spans
    if n <= target:
        ls, le = _trim(content, 0, n)
        if le > ls:
            spans.append((ls, le))
        return spans
    pos = 0
    while pos < n:
        end = min(pos + target, n)
        if end < n:
            ws = max(pos + target - 300, pos + minchars)
            we = min(pos + target + 150, n)
            if ws < we:
                window = content[ws:we]
                for brk in _BREAKS:
                    bi = window.rfind(brk)
                    if bi >= 0:
                        cand = ws + bi + len(brk)
                        if cand > pos:
                            end = cand
                            break
        ls, le = _trim(content, pos, end)
        if le > ls:
            spans.append((ls, le))
        if end >= n:
            break
        pos = max(pos + 1, end - overlap)
    return spans


def _resolve_canonical(cur, corpus, tenant_id, doc_id):
    # Use the SAME basis the segmenter wrote sections against: the persisted
    # geometry canonical (doc_geometry.canonical_text via load_geometry) when it
    # exists, else the corpus text column. Keeps chunk offsets §0-aligned with the
    # sections (no section_canonical_drift). No-op for eDiscovery where
    # extracted_text already equals the geometry canonical.
    try:
        from modules.ediscovery.services.geometry_io import load_geometry
        g = load_geometry(corpus, doc_id)
        if g is not None and g[0]:
            return g[0]
    except Exception:
        pass
    spec = CORPUS[corpus]
    cur.execute(
        f"SELECT {spec['canon_col']} FROM {spec['table']} "
        f"WHERE id = %(d)s::uuid AND TRIM(tenant_id) = %(t)s",
        {"d": doc_id, "t": tenant_id.strip()},
    )
    row = cur.fetchone()
    return row[0] if row else None


def _live_sections(cur, corpus, doc_id):
    spec = CORPUS[corpus]
    cur.execute(f"""
        SELECT id::text, section_index, section_type, section_label,
               content, char_start, char_end, page_start, page_end
        FROM document_sections
        WHERE {spec['fk_col']} = %(d)s::uuid AND superseded_by_run_id IS NULL
        ORDER BY section_index
    """, {"d": doc_id})
    return cur.fetchall()


def build_chunks(canonical, sections, pack_target=None):
    """Shared core: sections -> §0-true chunk dicts (corpus-agnostic).
    Returns (chunks, errors). Each chunk carries canonical offsets + primitive
    linkage. content is sliced from canonical so §0 holds by construction."""
    chunks, errors = [], []
    cidx = 0
    if pack_target:
        valid = []
        for (sid, sindex, stype, slabel, scontent, scs, sce, ps, pe) in sections:
            if scs is None or sce is None:
                errors.append((sindex, "null_section_offsets")); continue
            if canonical[scs:sce] != scontent:
                errors.append((sindex, "section_canonical_drift")); continue
            valid.append((sid, sindex, stype, slabel, scs, sce, ps, pe))
        i = 0
        while i < len(valid):
            g = valid[i]; gstart, gend = g[4], g[5]; members = [g]; j = i + 1
            while j < len(valid) and (valid[j][5] - gstart) <= pack_target:
                gend = valid[j][5]; members.append(valid[j]); j += 1
            gcontent = canonical[gstart:gend]; first = members[0]
            sidxs = [m[1] for m in members]
            for (ls, le) in chunk_section(gcontent):
                cs, ce = gstart + ls, gstart + le
                content = canonical[cs:ce]
                if content != gcontent[ls:le]:
                    errors.append((first[1], "chunk_slice_mismatch")); continue
                chunks.append({
                    "chunk_index": cidx,
                    "char_start": cs, "char_end": ce,
                    "content": content,
                    "section_id": first[0], "primitive_id": first[0],
                    "primitive_type": "section",
                    "section_label": first[3],
                    "token_count": max(1, len(content) // 4),
                    "meta": {"section_index": first[1], "section_type": first[2],
                             "page_start": first[6], "page_end": members[-1][7],
                             "merged_sections": sidxs, "chunker": CHUNKER_VERSION},
                })
                cidx += 1
            i = j
        return chunks, errors
    for (sid, sindex, stype, slabel, scontent, scs, sce, ps, pe) in sections:
        if scs is None or sce is None:
            errors.append((sindex, "null_section_offsets"))
            continue
        # Re-affirm §0 at the section level against the CURRENT canonical.
        if canonical[scs:sce] != scontent:
            errors.append((sindex, "section_canonical_drift"))
            continue
        for (ls, le) in chunk_section(scontent):
            cs, ce = scs + ls, scs + le
            content = canonical[cs:ce]
            if content != scontent[ls:le]:
                errors.append((sindex, "chunk_slice_mismatch"))
                continue
            chunks.append({
                "chunk_index": cidx,
                "char_start": cs, "char_end": ce,
                "content": content,
                "section_id": sid, "primitive_id": sid, "primitive_type": "section",
                "section_label": slabel,
                "token_count": max(1, len(content) // 4),
                "meta": {"section_index": sindex, "section_type": stype,
                         "page_start": ps, "page_end": pe,
                         "chunker": CHUNKER_VERSION},
            })
            cidx += 1
    return chunks, errors


def _get_or_create_strategy(cur, tenant_id):
    cur.execute("SELECT id::text FROM chunking_strategies WHERE slug = %s LIMIT 1",
                (STRATEGY_SLUG,))
    row = cur.fetchone()
    if row:
        return row[0]
    sid = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO chunking_strategies
          (id, tenant_id, slug, display_name, description, chunker_kind,
           chunk_size, chunk_overlap, rules, embedding_model, embedding_dimension,
           status, version)
        VALUES (%s::uuid, %s, %s, %s, %s, %s,
                %s, %s, '{}'::jsonb, %s, %s, 'published', 1)
    """, (sid, tenant_id, STRATEGY_SLUG, "Canonical Spine Chunker v1",
          "Chunks derived per spine primitive (document_sections); "
          "canonical offsets, §0-true; embedding deferred.",
          CHUNKER_KIND, TARGET, OVERLAP, EMBED_MODEL, EMBED_DIM))
    log.info("created chunking strategy %s (%s)", STRATEGY_SLUG, sid)
    return sid


def _strategy_fingerprint():
    return _sha(f"{STRATEGY_SLUG}|{CHUNKER_KIND}|{TARGET}|{OVERLAP}|{EMBED_MODEL}|{EMBED_DIM}|v1")


def write_ediscovery(cur, tenant_id, doc_id, canonical, chunks, force):
    strat_id = _get_or_create_strategy(cur, tenant_id)
    sfp = _strategy_fingerprint()
    cfp = _sha(canonical)

    if not force:
        cur.execute("""
            SELECT r.id FROM ediscovery_document_chunking_runs r
            WHERE r.document_id = %s::uuid AND r.strategy_fingerprint = %s
              AND r.content_fingerprint = %s AND r.status = 'completed'
              AND EXISTS (SELECT 1 FROM ediscovery_chunks c WHERE c.run_id = r.id)
            LIMIT 1
        """, (doc_id, sfp, cfp))
        if cur.fetchone():
            return {"status": "cache_hit", "chunks": 0}

    run_id = str(uuid.uuid4())
    t0 = time.time()
    # One live chunk set + run per (doc, strategy). Clear prior chunks first
    # (frees the chunks.run_id FK), then any prior/orphan run row (e.g. left by a
    # re-segmentation after render/OCR), so the (document_id, strategy_id) unique
    # key is free. Makes the chunker safely re-runnable.
    cur.execute("DELETE FROM ediscovery_chunks WHERE document_id = %s::uuid", (doc_id,))
    cur.execute("DELETE FROM ediscovery_document_chunking_runs "
                "WHERE document_id = %s::uuid AND strategy_id = %s::uuid", (doc_id, strat_id))
    cur.execute("""
        INSERT INTO ediscovery_document_chunking_runs
          (id, tenant_id, document_id, strategy_id, strategy_fingerprint,
           content_fingerprint, status, chunk_count, started_at)
        VALUES (%s::uuid, %s, %s::uuid, %s::uuid, %s, %s, 'running', 0, NOW())
    """, (run_id, tenant_id, doc_id, strat_id, sfp, cfp))

    rows = [(
        str(uuid.uuid4()), tenant_id, doc_id, run_id, c["chunk_index"],
        c["char_start"], c["char_end"], c["content"], c["token_count"],
        c["section_label"], json.dumps(c["meta"]), c["content"],
        c["section_id"], c["primitive_type"], c["primitive_id"],
    ) for c in chunks]
    psycopg2.extras.execute_values(cur, """
        INSERT INTO ediscovery_chunks
          (id, tenant_id, document_id, run_id, chunk_index, char_start, char_end,
           content, token_count, section_label, chunk_metadata, chunked_at,
           embedded_content, section_id, primitive_type, primitive_id)
        VALUES %s
    """, rows, template=(
        "(%s::uuid,%s,%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s::jsonb,NOW(),"
        "%s,%s::uuid,%s,%s::uuid)"
    ))
    cur.execute("""
        UPDATE ediscovery_document_chunking_runs
        SET status='completed', chunk_count=%s, finished_at=NOW(),
            duration_ms=%s
        WHERE id=%s::uuid
    """, (len(rows), int((time.time() - t0) * 1000), run_id))
    return {"status": "written", "chunks": len(rows), "run_id": run_id}


def write_dms(cur, tenant_id, doc_id, canonical, chunks, force):
    # run_id is unconstrained for dms_chunks. matter/client/file fields are
    # nullable -> left NULL in v1; matter linkage backfill is a follow-up.
    run_id = str(uuid.uuid4())
    cur.execute("""
        DELETE FROM dms_chunks
        WHERE source_type='dms_document' AND source_id=%s::uuid
    """, (doc_id,))
    rows = [(
        str(uuid.uuid4()), tenant_id, doc_id, run_id, c["chunk_index"],
        c["char_start"], c["char_end"], c["content"], c["content"],
        c["token_count"], (c["section_label"] or "")[:100], json.dumps(c["meta"]),
        c["meta"].get("page_start"),
        c["section_id"], c["primitive_type"], c["primitive_id"],
    ) for c in chunks]
    psycopg2.extras.execute_values(cur, """
        INSERT INTO dms_chunks
          (id, tenant_id, source_type, source_id, run_id, chunk_index,
           char_start, char_end, content, embedded_content, token_count,
           section_label, chunk_metadata, chunked_at, page_number,
           section_id, primitive_type, primitive_id)
        VALUES %s
    """, rows, template=(
        "(%s::uuid,%s,'dms_document',%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s,"
        "%s::jsonb,NOW(),%s,%s::uuid,%s,%s::uuid)"
    ))
    return {"status": "written", "chunks": len(rows), "run_id": run_id}


SINKS = {"ediscovery": write_ediscovery, "dms": write_dms}


def process_doc(cur, corpus, tenant_id, doc_id, dry_run, force):
    canonical = _resolve_canonical(cur, corpus, tenant_id, doc_id)
    if canonical is None:
        return {"doc": doc_id, "status": "doc_not_found"}
    if not canonical.strip():
        return {"doc": doc_id, "status": "empty_canonical"}
    sections = _live_sections(cur, corpus, doc_id)
    if not sections:
        return {"doc": doc_id, "status": "no_spine"}

    _pack = EDISCOVERY_PACK if (corpus == "ediscovery" and EDISCOVERY_PACK > 0) else None
    chunks, errors = build_chunks(canonical, sections, pack_target=_pack)
    if errors:
        return {"doc": doc_id, "status": "chunk_errors",
                "errors": errors[:10], "error_count": len(errors)}
    if not chunks:
        return {"doc": doc_id, "status": "no_chunks"}

    if dry_run:
        return {"doc": doc_id, "status": "dry_run", "chunks": len(chunks),
                "sections": len(sections),
                "sample": [{"i": c["chunk_index"], "start": c["char_start"],
                            "end": c["char_end"], "len": len(c["content"]),
                            "prim": c["primitive_type"]} for c in chunks[:6]]}

    res = SINKS[corpus](cur, tenant_id, doc_id, canonical, chunks, force)
    res["doc"] = doc_id
    res["sections"] = len(sections)
    return res


def iter_target_docs(cur, corpus, tenant_id, limit, collection=None):
    spec = CORPUS[corpus]
    params = {"t": tenant_id.strip()}
    coll_clause = ""
    if collection and corpus == "ediscovery":
        coll_clause = f"AND EXISTS (SELECT 1 FROM ediscovery_documents d WHERE d.id = s.{spec['fk_col']} AND d.collection_id = %(coll)s::uuid)"
        params["coll"] = collection
    cur.execute(f"""
        SELECT DISTINCT s.{spec['fk_col']}::text
        FROM document_sections s
        WHERE TRIM(s.tenant_id) = %(t)s
          AND s.{spec['fk_col']} IS NOT NULL
          AND s.superseded_by_run_id IS NULL
          {coll_clause}
        ORDER BY 1
        {f'LIMIT {int(limit)}' if limit else ''}
    """, params)
    return [r[0] for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser(description="Spine-grounded chunker")
    ap.add_argument("--corpus", required=True, choices=list(CORPUS.keys()))
    ap.add_argument("--tenant", default=os.environ.get(
        "TENANT_ID", "986c0fee-1390-43bb-ad28-8cd1db6de53f"))
    ap.add_argument("--doc", default=None)
    ap.add_argument("--all", action="store_true",
                    help="All corpus docs that have a live spine")
    ap.add_argument("--collection", default=None,
                    help="Scope --all to one collection id (ediscovery)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="Ignore cache-skip")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.doc and not args.all:
        ap.error("provide --doc <id> or --all")

    conn = psycopg2.connect(get_db_url())
    conn.autocommit = False
    log.info("=== Spine Chunker (%s) corpus=%s tenant=%s dry_run=%s ===",
             CHUNKER_VERSION, args.corpus, args.tenant.strip(), args.dry_run)
    try:
        with conn.cursor() as cur:
            docs = [args.doc] if args.doc else \
                iter_target_docs(cur, args.corpus, args.tenant, args.limit, args.collection)
            if not args.doc:
                log.info("targets: %d docs", len(docs))
            n_docs = n_chunks = 0
            for d in docs:
                res = process_doc(cur, args.corpus, args.tenant, d, args.dry_run, args.force)
                st = res["status"]
                if st in ("written", "dry_run"):
                    n_chunks += res.get("chunks", 0)
                    if st == "written":
                        n_docs += 1
                    log.info("  %s -> %s (%d chunks)%s", d, st, res.get("chunks", 0),
                             "  sample=" + json.dumps(res["sample"]) if res.get("sample") else "")
                elif st == "cache_hit":
                    log.info("  %s -> cache_hit", d)
                else:
                    log.warning("  %s -> %s %s", d, st,
                                {k: v for k, v in res.items() if k not in ("doc", "status")})
            if args.dry_run:
                conn.rollback()
                log.info("DRY RUN -- rolled back. docs=%d chunks=%d", len(docs), n_chunks)
            else:
                conn.commit()
                log.info("COMMIT -- docs_chunked=%d chunks=%d", n_docs, n_chunks)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
