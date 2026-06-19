"""cr_parser.py -- Appellate Surface C-5: Clerk's Record document segmentation.

The CR is a clerk-compiled, sequentially-paginated PDF of the trial-court file. U4
already page-addressed it (record_pages, from geometry). This adds the second job:
DOCUMENT-BOUNDARY segmentation driven by the clerk's INDEX (each filing begins at a
stated CR page) -> segment the CR into its constituent filings (petition, answer,
counterclaim, FFCL, judgment, notice of appeal, ...) as addressable sub-objects, and
within the substantive filings cut geometry-aware paragraph FACT UNITS (embedded
ModernBERT-768) -- the CR side of the closed record the fact-element classifier (U9)
consumes (the RR side is transcript_qa_units).

Two granularities, both in document_sections:
  cr_filing  (nesting_depth 0)  -- one per clerk-index entry; navigation + provenance
  cr_para    (nesting_depth 1)  -- paragraph fact units inside substantive filings; embedded

Page mapping is anchored on record_pages.char_start/char_end (canonical-space, the
authoritative partition) -> each filing/paragraph gets its clerk page (CR cite).

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.cr_parser --parse --appeal UUID [--doc RECORD_DOC_UUID] [--tenant T]
  python -m modules.depositions.jobs.cr_parser --list --appeal UUID
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import os
import re
import uuid

logger = logging.getLogger(__name__)

# clerk-index entry marker: "5. PLAINTIFF'S ORIGINAL PETITION"
_ENTRY = re.compile(r"^\s*(\d{1,2})\.\s+(.*\S)\s*$")
_RANGE = re.compile(r"\b(\d{1,4})\s*[-–]\s*(\d{1,4})\b")
_DATE = re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}\s*$")
_PAGENO = re.compile(r"^\s*\d{1,4}\s*$")

# title -> normalized CR filing type; SUBSTANTIVE types get paragraph fact units.
_TYPE_RULES = [
    ("judgment",            re.compile(r"\bJUDGMENT\b", re.I)),
    ("findings_conclusions", re.compile(r"FINDINGS\s+OF\s+FACT|CONCLUSIONS\s+OF\s+LAW", re.I)),
    ("intervention",        re.compile(r"INTERVENTION", re.I)),
    ("counterclaim",        re.compile(r"COUNTERCLAIM", re.I)),
    ("answer",              re.compile(r"\bANSWER\b", re.I)),
    ("petition",            re.compile(r"\bPETITION\b", re.I)),
    ("notice_of_appeal",    re.compile(r"NOTICE\s+OF\s+APPEAL", re.I)),
    ("cost_bill",           re.compile(r"COST\s+BILL|BILL\s+OF\s+COST", re.I)),
    ("docket_sheet",        re.compile(r"DOCKET\s+SHEET", re.I)),
    ("clerk_certificate",   re.compile(r"CERTIFICATE", re.I)),
    ("caption",             re.compile(r"\bCAPTION\b", re.I)),
    ("index",               re.compile(r"\bINDEX\b", re.I)),
    ("cover_sheet",         re.compile(r"COVER\s+SHEET", re.I)),
    ("motion",              re.compile(r"\bMOTION\b", re.I)),
    ("order",               re.compile(r"\bORDER\b", re.I)),
]
SUBSTANTIVE = {"petition", "answer", "counterclaim", "intervention",
               "findings_conclusions", "judgment", "motion", "order"}

_MIN_PARA_CHARS = 40
_EMBED_BATCH = 64


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    # Hang-safety backstop: with ix_document_sections_parent the delete-before-write
    # is fast; these ensure a pathological case aborts instead of stalling an ingest.
    with conn.cursor() as _c:
        _c.execute("SET statement_timeout = '180s'")
        _c.execute("SET lock_timeout = '30s'")
    conn.commit()
    return conn


def _cr_type(title):
    for code, rx in _TYPE_RULES:
        if rx.search(title or ""):
            return code
    return "other"


_INDEX_HDR = re.compile(r"\bINDEX\b|\bFile\s+Date\b", re.I)
_DATE_INLINE = re.compile(
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+\d{4}\b",
    re.I)


def _index_region(page_rows, canonical):
    """The clerk's index, which may span several pages but carries its INDEX/File-Date
    header only on the first. Find that first page (header + >=3 ranges), then extend
    across contiguous range-dense pages (the index continuation) until the body starts.
    Handles both the numbered main-CR index and the unnumbered supplemental index."""
    texts = [canonical[pr["cs"]:pr["ce"]] for pr in page_rows[:10]]
    start = next((i for i, t in enumerate(texts)
                  if _INDEX_HDR.search(t) and len(_RANGE.findall(t)) >= 3), None)
    if start is None:
        return ""
    chunks = [texts[start]]
    for j in range(start + 1, len(texts)):
        if len(_RANGE.findall(texts[j])) >= 3:
            chunks.append(texts[j])
        else:
            break
    return "\n".join(chunks)


def _clean_title(blob):
    """Pull a filing title out of the index text preceding a page-range token:
    drop the caption preamble + 'Document File Date Page' header + the file date."""
    t = blob
    # cut everything up to and including the column header, if present
    m = re.search(r"(?:Document\s+)?File\s+Date\s+Page", t, re.I)
    if m:
        t = t[m.end():]
    t = _DATE_INLINE.sub(" ", t)
    t = re.sub(r"\bVS\.?\b|\bDALLAS COUNTY, TEXAS\b|\bIN THE DISTRICT COURT\b|\bOF\b\s*$", " ", t, flags=re.I)
    t = re.sub(r"[§\d]+\s*$", " ", t)          # trailing footer page-no / section marks
    t = re.sub(r"\s+", " ", t).strip(" -–—\t")
    return t


def _parse_index(index_text, page_count):
    """Parse clerk-index -> ordered filings [(title, type)] + page breakpoints.

    The 3-column index (Document | File Date | Page) zig-zags in reading order, so
    titles and ranges desync. We recover titles in order and tile the page span with
    breakpoints from BOTH range starts (P1) and range ends+1 (P2+1) -- a missing P1 is
    healed by the previous filing's P2+1. Then zip titles to the tiled segments."""
    lines = [l.rstrip() for l in index_text.splitlines()]
    titles, cur = [], None
    for ln in lines:
        m = _ENTRY.match(ln)
        if m:
            if cur:
                titles.append(cur)
            cur = m.group(2).strip()
        elif cur is not None:
            s = ln.strip()
            # continuation = ALL-CAPS words, not a date / page-no / page-range
            is_range = re.fullmatch(r"\d+\s*[-–]\s*\d+", s) is not None
            if (s and len(s) > 2 and s.upper() == s
                    and not _DATE.match(s) and not _PAGENO.match(s) and not is_range):
                cur = (cur + " " + s).strip()
    if cur:
        titles.append(cur)

    # ordered ranges (as they appear) + breakpoint tiling
    ranges = []
    for m in _RANGE.finditer(index_text):
        p1, p2 = int(m.group(1)), int(m.group(2))
        if 1 <= p1 <= p2 <= page_count:
            ranges.append((m.start(), m.end(), p1, p2))
    pts = {1, page_count + 1}
    for _s, _e, p1, p2 in ranges:
        pts.add(p1); pts.add(p2 + 1)
    bps = sorted(pts)
    segs = [(bps[k], bps[k + 1] - 1) for k in range(len(bps) - 1)]

    filings = []
    if len(titles) >= 2 and len(segs) == len(titles):
        # numbered main-CR index: titles reliable in order, tile pages, zip
        for (pf, pl), title in zip(segs, titles):
            filings.append({"title": title, "type": _cr_type(title),
                            "page_first": pf, "page_last": pl, "healed": False})
    elif ranges:
        # unnumbered (supplemental) index: title = cleaned text before each range
        prev = 0
        for (ms, me, p1, p2) in ranges:
            title = _clean_title(index_text[prev:ms]) or "(filing %d)" % (len(filings) + 1)
            filings.append({"title": title, "type": _cr_type(title),
                            "page_first": p1, "page_last": p2, "healed": False})
            prev = me
    else:
        n = max(len(segs), len(titles))
        for i in range(n):
            title = titles[i] if i < len(titles) else "(unlabeled filing %d)" % (i + 1)
            pf, pl = segs[i] if i < len(segs) else (None, None)
            filings.append({"title": title, "type": _cr_type(title),
                            "page_first": pf, "page_last": pl, "healed": True})
    return filings


def _clerk_page_lookup(page_rows):
    """Return (starts, mapper): char_start sorted + fn(pos)->(clerk_page, pdf_page)."""
    rows = sorted(page_rows, key=lambda r: r["cs"])
    starts = [r["cs"] for r in rows]

    def page_of(pos):
        i = bisect.bisect_right(starts, pos) - 1
        if i < 0:
            i = 0
        r = rows[i]
        return (r["page_number"] if r["page_number"] is not None else r["pdf_page"]), r["pdf_page"]
    return starts, page_of


def parse_cr(tenant_id, appellate_case_id, record_document_id=None, embed=True, fact_units=True) -> dict:
    from modules.ediscovery.services.geometry_io import load_geometry
    from jobs.canonical_segmenter import segment_by_geometry, _label
    from modules.depositions.jobs.embed_qa import _embed, _vec_literal, EMBED_URL_DEFAULT
    tenant = (tenant_id or "").strip()
    conn = _connect()
    out_docs = []
    try:
        cur = conn.cursor()
        q = ("SELECT id::text, document_id::text, geometry_doc_id::text, label, page_count, "
             "       matter_id::text, record_kind FROM record_documents "
             "WHERE appellate_case_id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s "
             "  AND record_kind IN ('CR','SUPP_CR')")
        params = [str(appellate_case_id), tenant]
        if record_document_id:
            q += " AND id=CAST(%s AS uuid)"; params.append(record_document_id)
        q += " ORDER BY sort_order, label"
        cur.execute(q, params)
        cr_docs = cur.fetchall()
        if not cr_docs:
            return {"error": "no CR/SUPP_CR record documents for this appeal"}

        for (rdid, doc_id, geo_doc, label, page_count, matter_id, kind) in cr_docs:
            geo_id = geo_doc or doc_id
            g = load_geometry("record", geo_id)
            if not g:
                out_docs.append({"record_document": label, "error": "no geometry"}); continue
            canonical, tokens, _meta = g

            cur.execute(
                "SELECT pdf_page, page_number, char_start, char_end FROM record_pages "
                "WHERE record_document_id=CAST(%s AS uuid) ORDER BY char_start", (rdid,))
            page_rows = [{"pdf_page": p, "page_number": n, "cs": cs, "ce": ce}
                         for (p, n, cs, ce) in cur.fetchall()]
            if not page_rows:
                out_docs.append({"record_document": label, "error": "no record_pages"}); continue
            _starts, page_of = _clerk_page_lookup(page_rows)
            n_pages = page_count or len(page_rows)

            index_text = _index_region(page_rows, canonical)
            filings = _parse_index(index_text, n_pages) if index_text else []
            if not filings:                       # supplements may have no index -> 1 filing
                filings = [{"title": label, "type": "other", "page_first": 1,
                            "page_last": n_pages, "healed": True}]

            # clerk-page -> filing index
            def filing_for_clerk(cp):
                for fi, f in enumerate(filings):
                    if f["page_first"] is not None and f["page_first"] <= cp <= (f["page_last"] or cp):
                        return fi
                return None

            # paragraph fact units (geometry-aware) over the whole CR
            paras = segment_by_geometry(canonical, tokens)

            run_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO extraction_runs (id, tenant_id, run_type, source_type, "
                " extraction_model, status, started_at) VALUES (CAST(%s AS uuid), %s, 'cr_parse', "
                " 'record', 'cr_parser_v1', 'running', NOW())", (run_id, tenant))
            # idempotent: drop prior auto sections for this CR doc
            cur.execute("DELETE FROM document_sections WHERE logical_document_id=CAST(%s AS uuid) "
                        "AND section_type IN ('cr_filing','cr_para')", (doc_id,))

            # --- filing-level parent rows ---
            filing_ids = {}
            for fi, f in enumerate(filings):
                # char span of the filing = covering its clerk pages
                pr_in = [pr for pr in page_rows
                         if f["page_first"] is not None
                         and f["page_first"] <= (pr["page_number"] or pr["pdf_page"]) <= (f["page_last"] or 10**9)]
                if pr_in:
                    cstart = min(pr["cs"] for pr in pr_in); cend = max(pr["ce"] for pr in pr_in)
                else:
                    cstart = cend = 0
                fid = str(uuid.uuid4())
                filing_ids[fi] = fid
                attrs = {"record_document_id": rdid, "appellate_case_id": str(appellate_case_id),
                         "record_kind": "CR", "cr_type": f["type"], "filing_index": fi + 1,
                         "clerk_page_first": f["page_first"], "clerk_page_last": f["page_last"],
                         "cite": ("CR %d" % f["page_first"]) if f["page_first"] else None,
                         "healed": f["healed"]}
                cur.execute(
                    "INSERT INTO document_sections (id, tenant_id, logical_document_id, "
                    "  extraction_run_id, section_index, section_type, section_label, content, "
                    "  char_start, char_end, page_start, page_end, nesting_depth, attributes, extracted_at) "
                    "VALUES (CAST(%s AS uuid),%s,CAST(%s AS uuid),CAST(%s AS uuid),%s,'cr_filing',%s,%s,"
                    "        %s,%s,%s,%s,0,CAST(%s AS jsonb),now())",
                    (fid, tenant, doc_id, run_id, fi, f["title"][:200],
                     (canonical[cstart:cend][:8000] or f["title"]), cstart, cend,
                     f["page_first"], f["page_last"], json.dumps(attrs)))

            # --- paragraph fact units inside substantive filings (embedded) ---
            unit_rows = []          # (id, content) pending embed
            pidx = 0
            for p in (paras if fact_units else []):
                content = p["content"].strip()
                if len(content) < _MIN_PARA_CHARS:
                    continue
                cp, pdfp = page_of(p["char_start"])
                fi = filing_for_clerk(cp)
                if fi is None or filings[fi]["type"] not in SUBSTANTIVE:
                    continue
                sid = str(uuid.uuid4())
                attrs = {"record_document_id": rdid, "appellate_case_id": str(appellate_case_id),
                         "record_kind": "CR", "cr_type": filings[fi]["type"],
                         "filing_index": fi + 1, "clerk_page": cp, "pdf_page": pdfp,
                         "cite": "CR %d" % cp}
                cur.execute(
                    "INSERT INTO document_sections (id, tenant_id, logical_document_id, "
                    "  extraction_run_id, section_index, section_type, section_label, content, "
                    "  char_start, char_end, page_start, page_end, nesting_depth, parent_section_id, "
                    "  attributes, extracted_at) VALUES (CAST(%s AS uuid),%s,CAST(%s AS uuid),"
                    "  CAST(%s AS uuid),%s,'cr_para',%s,%s,%s,%s,%s,%s,1,CAST(%s AS uuid),CAST(%s AS jsonb),now())",
                    (sid, tenant, doc_id, run_id, pidx, (_label(content) or "")[:200], content,
                     p["char_start"], p["char_end"], cp, cp, filing_ids[fi], json.dumps(attrs)))
                unit_rows.append((sid, content))
                pidx += 1

            # embed the paragraph fact units (ModernBERT-768) into document_sections.embedding.
            # Optional + best-effort: deterministic segmentation must land regardless; if the
            # embed lane is slow/unavailable, cr_para vectors are backfilled there later.
            embedded = 0
            if embed:
                for i in range(0, len(unit_rows), _EMBED_BATCH):
                    batch = unit_rows[i:i + _EMBED_BATCH]
                    try:
                        vecs, _m, _r = _embed(EMBED_URL_DEFAULT, [c[:4000] for _id, c in batch])
                    except Exception:
                        logger.warning("cr_para embed batch failed; deferring to embed lane", exc_info=True)
                        break
                    for (sid, _c), v in zip(batch, vecs):
                        cur.execute("UPDATE document_sections SET embedding=%s::vector, embedded_at=now() "
                                    "WHERE id=CAST(%s AS uuid)", (_vec_literal(v), sid))
                        embedded += 1
            cur.execute("UPDATE extraction_runs SET status='completed' WHERE id=CAST(%s AS uuid)",
                        (run_id,))
            conn.commit()
            out_docs.append({"record_document": label, "record_kind": kind, "pages": n_pages,
                             "filings": len(filings), "fact_units": len(unit_rows),
                             "embedded": embedded,
                             "filing_types": _counts([f["type"] for f in filings]),
                             "any_healed": any(f["healed"] for f in filings)})
        return {"appellate_case_id": str(appellate_case_id), "documents": out_docs}
    finally:
        conn.close()


def _counts(xs):
    from collections import Counter
    return dict(Counter(xs))


def list_cr(tenant_id, appellate_case_id) -> list:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT ds.section_label, ds.attributes->>'cr_type' t, ds.page_start, ds.page_end, "
            "       (SELECT count(*) FROM document_sections c WHERE c.parent_section_id=ds.id) units "
            "FROM document_sections ds WHERE ds.section_type='cr_filing' "
            "  AND ds.attributes->>'appellate_case_id'=%s AND TRIM(ds.tenant_id)=%s "
            "ORDER BY ds.page_start NULLS LAST, ds.section_index",
            (str(appellate_case_id), (tenant_id or "").strip()))
        cols = ["filing", "type", "page_first", "page_last", "fact_units"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Clerk's Record parser (Appellate C-5 / U9.0)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--appeal", required=True)
    ap.add_argument("--doc", default=None, help="single record_document id")
    ap.add_argument("--parse", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.parse:
        out = parse_cr(args.tenant, args.appeal, args.doc)
    elif args.list:
        out = list_cr(args.tenant, args.appeal)
    else:
        ap.error("one of --parse/--list required")
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
