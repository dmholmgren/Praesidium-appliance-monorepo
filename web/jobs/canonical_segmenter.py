#!/usr/bin/env python3
"""
jobs/canonical_segmenter.py  (v2 -- geometry-aware paragraph segmentation)

The `segment` stage of the unified ingestion loop:

    extract -> [SEGMENT] -> match taxonomy -> chunk-from-spine -> embed

v2 cuts canonical text into PARAGRAPH primitives using the persisted geometry
artifact (doc_layout_tokens), not blank lines in the flattened string. On a
born-digital PDF the producer emits a single '\\n' per line and '\\f' per page and
NEVER a blank line, so the v1 whitespace rule degenerated to PAGE-level segments.
The real paragraph signal is the VISUAL VERTICAL GAP between paragraphs, which
lives in the token y/h coordinates:

    paragraph break  <=>  line-to-line pitch exceeds ~1.5x the document's median
                          line pitch  (a page change is always a break)

block_no/line_no (fitz grouping, page-local) give clean line reconstruction when
present and lower the gap threshold as corroboration; y-clustering is used when
they are null (tokens persisted before the block/line migration).

Offsets are EXACT BY CONSTRUCTION: each token carries (char_start,char_end) into
the SAME canonical string load_geometry returns, so a paragraph's span is just
[first_token.char_start .. last_token.char_end], trimmed, and

    canonical[char_start:char_end] == segment.content

holds for every segment -- asserted before any write (the §0 invariant).

Canonical source:
    - PRIMARY: load_geometry(corpus, doc_id) -> the geometry canonical + tokens
      (same string the boxes, structural_pass and chunker index). Used whenever a
      persisted geometry header exists.
    - FALLBACK (no geometry header yet): the corpus text column, segmented by the
      v1 blank-line/page-break regex. Degraded (page-level) but never breaks.

Output: document_sections rows (one live run per invocation; prior live sections
for a doc are marked superseded_by_run_id = this run).

Usage (inside the praesidium-web container):
    # dry run one doc -- prints segments + §0 result, writes nothing
    docker exec praesidium-web python3 /app/jobs/canonical_segmenter.py \
        --corpus dms --doc d19cd919-4a6e-4d47-9219-9341f5c21e90 --dry-run

    # commit
    docker exec praesidium-web python3 /app/jobs/canonical_segmenter.py \
        --corpus dms --doc d19cd919-4a6e-4d47-9219-9341f5c21e90
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import statistics
import sys
import uuid
from collections import defaultdict

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

log = logging.getLogger("canonical_segmenter")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

PAGE_BREAK = "\f"
# Fallback boundary (no geometry): page break, or a blank line.
BOUNDARY = re.compile(r"\f|\n[ \t]*\n[ \t\n]*")

# Geometry paragraph thresholds (normalized 0-1 page coords).
GAP_FACTOR = 1.5          # pitch > 1.5x median line pitch  -> paragraph break
GAP_FACTOR_BLOCK = 1.15   # smaller pitch suffices if fitz block_no also changed

SEGMENTER_GEOM = "canonical_v2_geom"
SEGMENTER_FALLBACK = "canonical_v1_fallback"

# Per-corpus resolution of (canonical column, document FK column).
CORPUS = {
    "ediscovery": {
        "table": "ediscovery_documents",
        "canon_col": "extracted_text",
        "fk_col": "ediscovery_document_id",
    },
    "dms": {
        "table": "dms_documents",
        "canon_col": "content_text",
        "fk_col": "dms_document_id",
    },
}


def get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return "host=172.28.0.1 port=5432 dbname=praesidium user=praesidium"
    for p in ("postgresql+asyncpg://", "postgresql+psycopg2://"):
        if url.startswith(p):
            return "postgresql://" + url[len(p):]
    return url


def _page_of(text: str, pos: int) -> int:
    """1-based page number of a char offset (count of \\f before it + 1)."""
    return text.count(PAGE_BREAK, 0, pos) + 1


# ── geometry-aware paragraph segmentation ─────────────────────────────────────
def _reconstruct_lines(tokens) -> list[dict]:
    """Group tokens into lines. Prefer fitz (page, block_no, line_no) when present
    (page-local indices uniquely identify a line); else cluster by (page, y).
    Each line: page, top, bottom, cs, ce, block."""
    have_lineno = any(getattr(t, "line_no", None) is not None for t in tokens)
    groups: dict = defaultdict(list)
    for t in tokens:
        if have_lineno and getattr(t, "line_no", None) is not None:
            key = (t.page_number, t.block_no, t.line_no)
        else:
            key = (t.page_number, round(t.y, 3))
        groups[key].append(t)

    lines = []
    for ts in groups.values():
        lines.append({
            "page": ts[0].page_number,
            "top": min(t.y for t in ts),
            "bottom": max(t.y + t.h for t in ts),
            "cs": min(t.char_start for t in ts),
            "ce": max(t.char_end for t in ts),
            "block": getattr(ts[0], "block_no", None),
        })
    lines.sort(key=lambda L: L["cs"])   # reading order == canonical order
    return lines


def segment_by_geometry(canonical: str, tokens) -> list[dict]:
    """Cut canonical into paragraph segments using the vertical-gap signal."""
    lines = _reconstruct_lines(tokens)
    if not lines:
        return []

    # Median line pitch (top-to-top) across consecutive same-page lines.
    pitches = [b["top"] - a["top"]
               for a, b in zip(lines, lines[1:])
               if a["page"] == b["page"] and b["top"] - a["top"] > 0]
    med = statistics.median(pitches) if pitches else 0.0

    # Group lines into paragraphs.
    paras = [[lines[0]]]
    for a, b in zip(lines, lines[1:]):
        boundary = False
        if b["page"] != a["page"]:
            boundary = True
        elif med > 0:
            gap = b["top"] - a["top"]
            block_changed = (a["block"] is not None and b["block"] is not None
                             and a["block"] != b["block"])
            if gap > GAP_FACTOR * med:
                boundary = True
            elif block_changed and gap > GAP_FACTOR_BLOCK * med:
                boundary = True
        if boundary:
            paras.append([b])
        else:
            paras[-1].append(b)

    segments = []
    idx = 0
    for plines in paras:
        s, e = plines[0]["cs"], plines[-1]["ce"]
        while s < e and canonical[s].isspace():
            s += 1
        while e > s and canonical[e - 1].isspace():
            e -= 1
        if e <= s:
            continue
        content = canonical[s:e]
        if not content.strip():
            continue
        segments.append({
            "section_index": idx,
            "char_start": s,
            "char_end": e,
            "content": content,
            "page_start": plines[0]["page"],
            "page_end": plines[-1]["page"],
            "section_type": "segment",
        })
        idx += 1
    return segments


# ── fallback: v1 blank-line / page-break segmentation on plain canonical ──────
def segment_canonical(text: str) -> list[dict]:
    if not text:
        return []
    raw_spans: list = []
    cursor = 0
    for m in BOUNDARY.finditer(text):
        raw_spans.append((cursor, m.start()))
        cursor = m.end()
    raw_spans.append((cursor, len(text)))

    segments = []
    idx = 0
    for s, e in raw_spans:
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e <= s:
            continue
        content = text[s:e]
        if not content.strip():
            continue
        segments.append({
            "section_index": idx,
            "char_start": s,
            "char_end": e,
            "content": content,
            "page_start": _page_of(text, s),
            "page_end": _page_of(text, e),
            "section_type": "segment",
        })
        idx += 1
    return segments


def _resolve_canonical(cur, corpus: str, tenant_id: str, doc_id: str):
    spec = CORPUS[corpus]
    cur.execute(
        f"SELECT {spec['canon_col']} FROM {spec['table']} "
        f"WHERE id = %(d)s::uuid AND TRIM(tenant_id) = %(t)s",
        {"d": doc_id, "t": tenant_id.strip()},
    )
    row = cur.fetchone()
    return row[0] if row else None


def _load_geometry(corpus: str, doc_id: str):
    """Return (canonical, tokens) from the persisted geometry artifact, or None."""
    try:
        from modules.ediscovery.services.geometry_io import load_geometry
    except Exception:
        return None
    g = load_geometry(corpus, doc_id)
    if g is None:
        return None
    canonical, tokens, _meta = g
    if not canonical or not tokens:
        return None
    return canonical, tokens


def _label(content: str) -> str:
    first = content.strip().splitlines()[0] if content.strip() else ""
    first = re.sub(r"\s+", " ", first).strip()
    return first[:120] or None


def process_doc(cur, corpus: str, tenant_id: str, doc_id: str, run_id: str,
                dry_run: bool) -> dict:
    # PRIMARY: geometry artifact (paragraph cut). FALLBACK: corpus text + regex.
    geo = _load_geometry(corpus, doc_id)
    if geo is not None:
        canonical, tokens = geo
        segments = segment_by_geometry(canonical, tokens)
        method = SEGMENTER_GEOM
    else:
        canonical = _resolve_canonical(cur, corpus, tenant_id, doc_id)
        if canonical is None:
            return {"doc": doc_id, "status": "doc_not_found"}
        if not canonical.strip():
            return {"doc": doc_id, "status": "empty_canonical"}
        segments = segment_canonical(canonical)
        method = SEGMENTER_FALLBACK

    if not segments:
        return {"doc": doc_id, "status": "no_segments", "method": method}

    # §0 self-check against the SAME canonical the offsets index.
    bad = [s["section_index"] for s in segments
           if canonical[s["char_start"]:s["char_end"]] != s["content"]]
    if bad:
        return {"doc": doc_id, "status": "offset_check_failed", "method": method,
                "bad_indices": bad[:10], "bad_count": len(bad)}

    if dry_run:
        return {"doc": doc_id, "status": "dry_run", "method": method,
                "segments": len(segments), "offsets_ok": len(segments),
                "sample": [{"i": s["section_index"], "start": s["char_start"],
                            "end": s["char_end"], "page": s["page_start"],
                            "label": _label(s["content"])}
                           for s in segments[:6]]}

    spec = CORPUS[corpus]
    cur.execute(
        f"UPDATE document_sections SET superseded_by_run_id = %(run)s::uuid "
        f"WHERE {spec['fk_col']} = %(d)s::uuid AND superseded_by_run_id IS NULL",
        {"run": run_id, "d": doc_id},
    )

    rows = []
    for s in segments:
        rows.append((
            str(uuid.uuid4()), tenant_id, doc_id, run_id,
            s["section_index"], s["section_type"], _label(s["content"]),
            s["content"], s["char_start"], s["char_end"],
            s["page_start"], s["page_end"],
            json.dumps({"segmenter": method, "corpus": corpus}),
        ))
    fk = spec["fk_col"]
    psycopg2.extras.execute_values(cur, f"""
        INSERT INTO document_sections
          (id, tenant_id, {fk}, extraction_run_id, section_index, section_type,
           section_label, content, char_start, char_end, page_start, page_end,
           nesting_depth, parent_section_id, attributes, extracted_at)
        VALUES %s
    """, rows, template=(
        "(%s::uuid,%s,%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,0,NULL,%s::jsonb,NOW())"
    ))
    return {"doc": doc_id, "status": "written", "method": method,
            "segments": len(segments)}


def iter_target_docs(cur, corpus: str, tenant_id: str, limit: int, collection=None):
    """Docs of the corpus that have canonical text and NO live sections yet."""
    spec = CORPUS[corpus]
    params = {"t": tenant_id.strip()}
    coll_clause = ""
    if corpus == "ediscovery":
        coll_clause = ("AND EXISTS (SELECT 1 FROM doc_layout_tokens t "
                       "WHERE t.doc_id = d.id AND t.corpus='ediscovery')")
        if collection:
            coll_clause += " AND d.collection_id = %(coll)s::uuid"
            params["coll"] = collection
    elif corpus == "dms":
        # only docs with a FINAL dms geometry artifact (gated PDFs) -- never raw
        # eDiscovery/production files that happen to live in dms_documents.
        coll_clause = ("AND EXISTS (SELECT 1 FROM doc_layout_tokens t "
                       "WHERE t.doc_id = d.id AND t.corpus='dms')")
    cur.execute(f"""
        SELECT d.id::text
        FROM {spec['table']} d
        WHERE TRIM(d.tenant_id) = %(t)s
          AND d.{spec['canon_col']} IS NOT NULL
          AND length(d.{spec['canon_col']}) > 0
          {coll_clause}
          AND NOT EXISTS (
            SELECT 1 FROM document_sections s
            WHERE s.{spec['fk_col']} = d.id
              AND s.superseded_by_run_id IS NULL)
        ORDER BY d.id
        {f'LIMIT {int(limit)}' if limit else ''}
    """, params)
    return [r[0] for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser(description="Canonical structural segmenter (v2 geom)")
    ap.add_argument("--corpus", required=True, choices=list(CORPUS.keys()))
    ap.add_argument("--tenant", default=os.environ.get(
        "TENANT_ID", "986c0fee-1390-43bb-ad28-8cd1db6de53f"))
    ap.add_argument("--doc", default=None, help="Single document id")
    ap.add_argument("--all", action="store_true",
                    help="Process all corpus docs with canonical and no live sections")
    ap.add_argument("--collection", default=None,
                    help="Scope --all to one collection id (ediscovery)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.doc and not args.all:
        ap.error("provide --doc <id> or --all")

    conn = psycopg2.connect(get_db_url())
    conn.autocommit = False
    run_id = str(uuid.uuid4())
    log.info("=== Canonical Segmenter (v2 geom) ===")
    log.info("corpus=%s tenant=%s run_id=%s dry_run=%s",
             args.corpus, args.tenant.strip(), run_id, args.dry_run)
    try:
        with conn.cursor() as cur:
            if not args.dry_run:
                cur.execute(
                    "INSERT INTO extraction_runs "
                    "(id, tenant_id, run_type, source_type, extraction_model, "
                    " status, started_at) "
                    "VALUES (%s::uuid, %s, 'canonical_segment', %s, %s, "
                    "        'running', NOW())",
                    (run_id, args.tenant, args.corpus, SEGMENTER_GEOM),
                )
            if args.doc:
                docs = [args.doc]
            else:
                docs = iter_target_docs(cur, args.corpus, args.tenant, args.limit, args.collection)
                log.info("targets: %d docs", len(docs))

            n_written = n_seg = 0
            methods: dict = {}
            for d in docs:
                res = process_doc(cur, args.corpus, args.tenant, d, run_id, args.dry_run)
                if res["status"] in ("written", "dry_run"):
                    n_seg += res.get("segments", 0)
                    methods[res.get("method")] = methods.get(res.get("method"), 0) + 1
                    if res["status"] == "written":
                        n_written += 1
                    log.info("  %s -> %s [%s] (%d segs)%s", d, res["status"],
                             res.get("method"), res.get("segments", 0),
                             "  sample=" + json.dumps(res["sample"]) if res.get("sample") else "")
                else:
                    log.warning("  %s -> %s %s", d, res["status"],
                                {k: v for k, v in res.items() if k not in ("doc", "status")})
            if args.dry_run:
                conn.rollback()
                log.info("DRY RUN -- rolled back. docs=%d segments=%d methods=%s",
                         len(docs), n_seg, methods)
            else:
                cur.execute(
                    "UPDATE extraction_runs SET status='completed', "
                    "document_count=%s, documents_processed=%s, completed_at=NOW() "
                    "WHERE id=%s::uuid",
                    (len(docs), n_written, run_id),
                )
                conn.commit()
                log.info("COMMIT -- docs_written=%d segments=%d methods=%s run_id=%s",
                         n_written, n_seg, methods, run_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
