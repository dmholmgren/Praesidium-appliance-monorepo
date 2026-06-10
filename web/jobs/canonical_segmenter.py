#!/usr/bin/env python3
"""
jobs/canonical_segmenter.py

The `segment` stage of the unified ingestion loop:

    extract -> [SEGMENT] -> match taxonomy -> chunk-from-spine -> embed

Reads a document's CANONICAL text (the single string that geometry tokens,
chunks, search and redaction all index) and cuts it into structural segment
primitives whose char offsets are exact BY CONSTRUCTION -- the segmenter records
the very indices it cut at, so

    canonical[char_start:char_end] == segment.content

is true for every segment, asserted before any write (the §0 invariant). This is
the producer-side fix for the offset drift in the old spine: we never transcribe
or relocate offsets, we cut and record.

Canonical basis per corpus (must match what the geometry producer indexes):
    ediscovery -> ediscovery_documents.extracted_text   (token-built canonical)
    dms        -> dms_documents.content_text

Segmentation strategy v1 (structural, deterministic):
    boundary = page break (\\f)  OR  blank-line run (\\n\\s*\\n)
The PDF geometry producer emits single \\n between lines/blocks and \\f at page
ends, so on current PDF canonical this yields PAGE-LEVEL segments. Prose / future
block-marked canonical with blank lines yields paragraph segments. Either way the
chunker does the size-bounded sub-splitting downstream; the segmenter only
establishes semantically-meaningful, §0-true primitive boundaries.

Output: document_sections rows (one live run per invocation; prior live sections
for a doc are marked superseded_by_run_id = this run).

Usage (inside the praesidium-web container):
    # dry run one doc -- prints segments + §0 result, writes nothing
    docker exec praesidium-web python3 /app/jobs/canonical_segmenter.py \
        --corpus ediscovery --doc 6c86e216-e97a-46aa-88bc-5d2cc7011e22 --dry-run

    # commit
    docker exec praesidium-web python3 /app/jobs/canonical_segmenter.py \
        --corpus ediscovery --doc 6c86e216-e97a-46aa-88bc-5d2cc7011e22
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import uuid

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

log = logging.getLogger("canonical_segmenter")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

PAGE_BREAK = "\f"
# A boundary is a page break, or a blank line (>=2 newlines optionally with
# intervening spaces/tabs). finditer over this yields the gaps BETWEEN segments.
BOUNDARY = re.compile(r"\f|\n[ \t]*\n[ \t\n]*")

SEGMENTER_VERSION = "canonical_v1"

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


def segment_canonical(text: str) -> list[dict]:
    """Cut canonical text into structural segments with EXACT offsets.

    Returns dicts: section_index, char_start, char_end, content, page_start,
    page_end, section_type. Offsets index `text` directly; content is the
    whitespace-trimmed slice and is guaranteed == text[char_start:char_end].
    """
    if not text:
        return []

    # Raw spans are the regions between boundary matches.
    raw_spans: list[tuple[int, int]] = []
    cursor = 0
    for m in BOUNDARY.finditer(text):
        raw_spans.append((cursor, m.start()))
        cursor = m.end()
    raw_spans.append((cursor, len(text)))

    segments: list[dict] = []
    idx = 0
    for s, e in raw_spans:
        # Trim whitespace but keep offsets pointing at the trimmed content so
        # the §0 slice identity holds.
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


def _label(content: str) -> str:
    first = content.strip().splitlines()[0] if content.strip() else ""
    first = re.sub(r"\s+", " ", first).strip()
    return first[:120] or None


def process_doc(cur, corpus: str, tenant_id: str, doc_id: str, run_id: str,
                dry_run: bool) -> dict:
    spec = CORPUS[corpus]
    canonical = _resolve_canonical(cur, corpus, tenant_id, doc_id)
    if canonical is None:
        return {"doc": doc_id, "status": "doc_not_found"}
    if not canonical.strip():
        return {"doc": doc_id, "status": "empty_canonical"}

    segments = segment_canonical(canonical)
    if not segments:
        return {"doc": doc_id, "status": "no_segments"}

    # §0 self-check: every segment must slice back to its content.
    bad = [s["section_index"] for s in segments
           if canonical[s["char_start"]:s["char_end"]] != s["content"]]
    if bad:
        return {"doc": doc_id, "status": "offset_check_failed",
                "bad_indices": bad[:10], "bad_count": len(bad)}

    if dry_run:
        return {"doc": doc_id, "status": "dry_run", "segments": len(segments),
                "offsets_ok": len(segments),
                "sample": [{"i": s["section_index"], "start": s["char_start"],
                            "end": s["char_end"], "page": s["page_start"],
                            "label": _label(s["content"])}
                           for s in segments[:5]]}

    # Supersede any prior live sections for this doc.
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
            json.dumps({"segmenter": SEGMENTER_VERSION, "corpus": corpus}),
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
    return {"doc": doc_id, "status": "written", "segments": len(segments)}


def iter_target_docs(cur, corpus: str, tenant_id: str, limit: int, collection=None):
    """Docs of the corpus that have canonical text and NO live sections yet.
    Optional collection scoping (ediscovery only)."""
    spec = CORPUS[corpus]
    params = {"t": tenant_id.strip()}
    coll_clause = ""
    if corpus == "ediscovery":
        # Only segment ediscovery docs whose geometry canonical is FINAL
        # (doc_layout_tokens exist) -- never a provisional enrich/body text that
        # a later render/OCR pass will supersede.
        coll_clause = ("AND EXISTS (SELECT 1 FROM doc_layout_tokens t "
                       "WHERE t.doc_id = d.id AND t.corpus='ediscovery')")
        if collection:
            coll_clause += " AND d.collection_id = %(coll)s::uuid"
            params["coll"] = collection
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
    ap = argparse.ArgumentParser(description="Canonical structural segmenter")
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
    log.info("=== Canonical Segmenter (%s) ===", SEGMENTER_VERSION)
    log.info("corpus=%s tenant=%s run_id=%s dry_run=%s",
             args.corpus, args.tenant.strip(), run_id, args.dry_run)
    try:
        with conn.cursor() as cur:
            # Register the run so document_sections.extraction_run_id FK resolves.
            if not args.dry_run:
                cur.execute(
                    "INSERT INTO extraction_runs "
                    "(id, tenant_id, run_type, source_type, extraction_model, "
                    " status, started_at) "
                    "VALUES (%s::uuid, %s, 'canonical_segment', %s, %s, "
                    "        'running', NOW())",
                    (run_id, args.tenant, args.corpus, SEGMENTER_VERSION),
                )
            if args.doc:
                docs = [args.doc]
            else:
                docs = iter_target_docs(cur, args.corpus, args.tenant, args.limit, args.collection)
                log.info("targets: %d docs", len(docs))

            n_written = n_seg = 0
            for d in docs:
                res = process_doc(cur, args.corpus, args.tenant, d, run_id, args.dry_run)
                if res["status"] in ("written", "dry_run"):
                    n_seg += res.get("segments", 0)
                    if res["status"] == "written":
                        n_written += 1
                    log.info("  %s -> %s (%d segs)%s", d, res["status"],
                             res.get("segments", 0),
                             "  sample=" + json.dumps(res["sample"]) if res.get("sample") else "")
                else:
                    log.warning("  %s -> %s %s", d, res["status"],
                                {k: v for k, v in res.items() if k not in ("doc", "status")})
            if args.dry_run:
                conn.rollback()
                log.info("DRY RUN -- rolled back. docs=%d segments=%d", len(docs), n_seg)
            else:
                cur.execute(
                    "UPDATE extraction_runs SET status='completed', "
                    "document_count=%s, documents_processed=%s, completed_at=NOW() "
                    "WHERE id=%s::uuid",
                    (len(docs), n_written, run_id),
                )
                conn.commit()
                log.info("COMMIT -- docs_written=%d segments=%d run_id=%s",
                         n_written, n_seg, run_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
