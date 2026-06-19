"""appellate_pipeline.py -- Module B / Unit 4: wire a matter's record into the
appellate workspace, ingest it as page-addressed geometry, resolve record cites.

The record on appeal = Clerk's Record (CR) + Reporter's Record (RR). Both are
ingested through the EXISTING geometry pipeline (geometry_io.build_geometry) under
corpus='record' -- the same doc_layout_tokens (word bbox <-> char-span) + doc_geometry
canonical that drive DMS/eDiscovery sectioning and viewer overlays. Geometry is what
makes exact-span click-through + page-number detection deterministic, so the record
reuses it instead of re-extracting flat text. On top of geometry:

  CR  -> record_pages: a citable-page index DERIVED FROM the geometry tokens (clerk's
         stamped page detected from a footer integer token by bbox). Cite 'CR [page]'
         resolves to the page's char span in doc_geometry.canonical_text + its tokens.
  RR  -> Module A trial pipeline (page:line transcript_lines + Q&A embeddings) for the
         testimony, PLUS geometry on the same PDF for click-through bbox. Cite
         '[vol] RR [page]:[line]' resolves via transcript_lines.

Record vs appendix (a real TRAP trap): CR/RR are THE RECORD (is_record=true, citable
as fact); appendix (38.1(k)) and briefs are NOT -- attached for reference, never
resolved as a record cite.

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.appellate_pipeline --new-appeal \
        --matter UUID --coa "Fifth Court of Appeals (Dallas)" --appno 05-25-00300-CV \
        [--trial-court "..."] [--trial-cause DC-23-06524] [--style "X v. Y"] \
        [--appellant "..."] [--appellee "..."] [--deadline YYYY-MM-DD] [--tenant T]
  python -m modules.depositions.jobs.appellate_pipeline --attach --appeal UUID [--matter UUID]
  python -m modules.depositions.jobs.appellate_pipeline --geometry --appeal UUID [--limit N]
  python -m modules.depositions.jobs.appellate_pipeline --ingest-rr --appeal UUID
  python -m modules.depositions.jobs.appellate_pipeline --resolve --appeal UUID --cite "CR 123"
  python -m modules.depositions.jobs.appellate_pipeline --status --appeal UUID
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

GEOM_CORPUS = "record"            # geometry corpus tag for the record (vs 'dms'/'ediscovery')
GEOM_RENDITION = "native_pdf"


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def create_appeal(tenant_id, matter_id, court_of_appeals=None, appellate_cause_number=None,
                  trial_court=None, trial_cause_number=None, style=None, appellant=None,
                  appellee=None, brief_deadline=None, jurisdiction="TX-TRAP") -> dict:
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO appellate_cases "
            "  (tenant_id, matter_id, court_of_appeals, appellate_cause_number, "
            "   trial_court, trial_cause_number, style, appellant, appellee, "
            "   jurisdiction, brief_deadline) "
            "VALUES (%s, CAST(%s AS uuid), %s, %s, %s, %s, %s, %s, %s, %s, "
            "        CAST(%s AS date)) RETURNING id::text",
            (tenant, (str(matter_id) if matter_id else None), court_of_appeals,
             appellate_cause_number, trial_court, trial_cause_number, style,
             appellant, appellee, jurisdiction, brief_deadline))
        aid = cur.fetchone()[0]
        conn.commit()
        logger.info("created appellate_case %s (%s)", aid, appellate_cause_number or "")
        return {"appellate_case_id": aid, "appellate_cause_number": appellate_cause_number}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# attach: classify the matter's dropped docs into the record register
# ---------------------------------------------------------------------------

_CR_DIR = re.compile(r"^0?1[\s\-_]*clerk")
_RR_DIR = re.compile(r"^0?2[\s\-_]*reporter")
_BR_DIR = re.compile(r"^0?3[\s\-_]*brief")


def _classify(storage_path: str):
    """(record_kind, is_record) from the SUBFOLDER the doc sits in -- the reliable
    signal -- not phrase-matching the whole path (correspondence in 03-Briefs that
    merely mentions 'Clerk's Record' must NOT be classed as the record). Seeded tree:
    01-Clerk's Record / 02-Reporter's Record / 03-Briefs and Motions."""
    parts = [seg.lower() for seg in (storage_path or "").split("/") if seg]
    name = parts[-1] if parts else ""
    folder = ""
    for seg in parts[:-1]:
        if _CR_DIR.match(seg):
            folder = "CR"; break
        if _RR_DIR.match(seg):
            folder = "RR"; break
        if _BR_DIR.match(seg):
            folder = "BRIEF"; break
    supp = "supplement" in name or "supp " in name or "_supp" in name
    if folder == "CR":
        return ("SUPP_CR" if supp else "CR"), True
    if folder == "RR":
        return ("SUPP_RR" if supp else "RR"), True
    # 03-Briefs and Motions (or unfiled): advocacy/correspondence -- NOT the record
    if "appendix" in name:
        return "APPENDIX", False
    if name.endswith(".docx") or "brief" in name:
        return "BRIEF", False
    return ("MOTION" if folder == "BRIEF" else "OTHER"), False


def attach_records(tenant_id, appellate_case_id, matter_id=None) -> dict:
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        if matter_id is None:
            cur.execute("SELECT matter_id::text FROM appellate_cases WHERE id=CAST(%s AS uuid)",
                        (str(appellate_case_id),))
            r = cur.fetchone()
            matter_id = r[0] if r else None
        cur.execute(
            "SELECT id::text, COALESCE(file_name, original_filename, "
            "       split_part(storage_path,'/',-1)) AS fn, storage_path, mime_type "
            "FROM documents WHERE matter_id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s "
            "ORDER BY storage_path", (str(matter_id), tenant))
        rows = cur.fetchall()
        counts = {}
        n = 0
        for doc_id, fn, path, mime in rows:
            kind, is_rec = _classify(path)
            label = (fn or (path or "").rsplit("/", 1)[-1])
            cur.execute(
                "INSERT INTO record_documents "
                "  (tenant_id, appellate_case_id, matter_id, record_kind, is_record, "
                "   document_id, label, storage_path, status) "
                "VALUES (%s, CAST(%s AS uuid), CAST(%s AS uuid), %s, %s, CAST(%s AS uuid), %s, %s, 'attached') "
                "ON CONFLICT (appellate_case_id, document_id) WHERE document_id IS NOT NULL "
                "DO UPDATE SET record_kind=EXCLUDED.record_kind, is_record=EXCLUDED.is_record, "
                "  label=EXCLUDED.label, storage_path=EXCLUDED.storage_path, updated_at=now()",
                (tenant, str(appellate_case_id), (str(matter_id) if matter_id else None),
                 kind, is_rec, doc_id, label, path))
            counts[kind] = counts.get(kind, 0) + 1
            n += 1
        conn.commit()
        logger.info("attached %d record doc(s): %s", n, counts)
        return {"attached": n, "by_kind": counts}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# geometry: reuse build_geometry under corpus='record'; CR -> record_pages
# ---------------------------------------------------------------------------

_FOOTER_Y = 0.88   # bbox coords are normalized 0..1; footer band is the bottom ~12%
_RIGHT_X = 0.55    # the clerk's CR page stamp sits bottom-RIGHT (Bates position)


def _page_spans_and_numbers(cur, doc_id):
    """From the persisted geometry tokens, return per-pdf-page:
       {pdf_page: (char_start, char_end, page_label, page_number_int, bbox)}.
    doc_layout_tokens x/y/w/h are normalized (0..1). The clerk's stamped CR page is
    the integer word token in the footer band (y>=0.88), bottom-RIGHT (the Bates
    position) -- the rightmost such integer wins, beating centered 'PAGE x OF y'
    furniture. Falls back to the pdf page when no stamp is found (calibration via
    pageno_bbox: NULL = fell back)."""
    cur.execute(
        "SELECT page_number, x, y, w, h, char_start, char_end, text "
        "FROM doc_layout_tokens WHERE corpus=%s AND doc_id=CAST(%s AS uuid) "
        "  AND rendition=%s AND unit='word' ORDER BY page_number, char_start",
        (GEOM_CORPUS, str(doc_id), GEOM_RENDITION))
    pages = {}
    for (pno, x, y, w, h, cs, ce, text) in cur.fetchall():
        p = pages.setdefault(pno, {"cs": cs, "ce": ce, "cands": []})
        p["cs"] = min(p["cs"], cs)
        p["ce"] = max(p["ce"], ce)
        t = (text or "").strip()
        if t.isdigit() and len(t) <= 5:
            yb = float(y or 0) + float(h or 0)
            xl = float(x or 0)
            p["cands"].append((yb, xl, int(t),
                               [xl, float(y or 0), float(w or 0), float(h or 0)]))
    out = {}
    for pno, p in pages.items():
        label, num, bbox = None, None, None
        footer = [c for c in p["cands"] if c[0] >= _FOOTER_Y and c[1] >= _RIGHT_X]
        if footer:
            footer.sort(key=lambda c: -c[1])   # rightmost = the Bates stamp
            _, _, num, bbox = footer[0]
            label = str(num)
        out[pno] = (p["cs"], p["ce"], label, num, bbox)
    return out


def capture_geometry(tenant_id, appellate_case_id, limit=0, redo=False) -> dict:
    """Build geometry (corpus='record') for each PDF record doc; for CR docs, derive
    the record_pages citable-page index from the tokens."""
    from modules.ediscovery.services import geometry_io
    tenant = (tenant_id or "").strip()
    conn = _connect()
    s = {"geometry_built": 0, "no_text": 0, "cr_pages": 0, "skipped": 0, "errors": 0}
    try:
        cur = conn.cursor()
        done = "" if redo else " AND geometry_status NOT IN ('done','skipped')"
        sql = ("SELECT id::text, document_id::text, storage_path, record_kind "
               "FROM record_documents WHERE appellate_case_id=CAST(%s AS uuid) "
               "  AND storage_path ILIKE '%%.pdf'" + done + " ORDER BY sort_order, id")
        if limit:
            sql += " LIMIT %d" % int(limit)
        cur.execute(sql, (str(appellate_case_id),))
        units = cur.fetchall()
        for rdoc_id, doc_id, path, kind in units:
            try:
                if not path or not os.path.exists(path):
                    raise FileNotFoundError(str(path))
                # geometry_doc_id = the DMS documents.id (stable key)
                out = geometry_io.build_geometry(tenant, GEOM_CORPUS, doc_id, path,
                                                 rendition=GEOM_RENDITION)
                if out is None:
                    cur.execute("UPDATE record_documents SET geometry_status='no_text', "
                                "updated_at=now() WHERE id=CAST(%s AS uuid)", (rdoc_id,))
                    conn.commit()
                    s["no_text"] += 1
                    continue
                _canon, _toks, meta = out
                cur.execute(
                    "UPDATE record_documents SET geometry_corpus=%s, geometry_doc_id=CAST(%s AS uuid), "
                    "  page_count=%s, geometry_status='done', updated_at=now() "
                    "WHERE id=CAST(%s AS uuid)",
                    (GEOM_CORPUS, doc_id, meta.get("page_count"), rdoc_id))
                conn.commit()
                s["geometry_built"] += 1

                if kind in ("CR", "SUPP_CR"):
                    spans = _page_spans_and_numbers(cur, doc_id)
                    cur.execute("DELETE FROM record_pages WHERE record_document_id=CAST(%s AS uuid)",
                                (rdoc_id,))
                    from psycopg2.extras import execute_values
                    rows = []
                    for pno in sorted(spans):
                        cs, ce, label, num, bbox = spans[pno]
                        rows.append((tenant, rdoc_id, str(appellate_case_id), pno,
                                     (label or str(pno)), (num if num is not None else pno),
                                     cs, ce, (json.dumps(bbox) if bbox else None)))
                    if rows:
                        execute_values(
                            cur,
                            "INSERT INTO record_pages "
                            "  (tenant_id, record_document_id, appellate_case_id, pdf_page, "
                            "   page_label, page_number, char_start, char_end, pageno_bbox) "
                            "VALUES %s",
                            rows,
                            template="(%s,CAST(%s AS uuid),CAST(%s AS uuid),%s,%s,%s,%s,%s,%s::jsonb)",
                            page_size=500)
                    pf = min(spans); pl = max(spans)
                    cur.execute("UPDATE record_documents SET page_first=%s, page_last=%s, "
                                "updated_at=now() WHERE id=CAST(%s AS uuid)",
                                (rows[0][5] if rows else pf, rows[-1][5] if rows else pl, rdoc_id))
                    conn.commit()
                    s["cr_pages"] += len(rows)
            except Exception as e:
                conn.rollback()
                cur.execute("UPDATE record_documents SET geometry_status='failed', updated_at=now() "
                            "WHERE id=CAST(%s AS uuid)", (rdoc_id,))
                conn.commit()
                s["errors"] += 1
                logger.exception("geometry failed for record_doc %s", rdoc_id)
        try:
            from modules.depositions.jobs.appellate_ocr import ocr_record_geometry
            s["ocr"] = ocr_record_geometry(tenant, appellate_case_id, redo=redo)
        except Exception:
            logger.exception("appellate_ocr step failed (non-fatal) for appeal %s", appellate_case_id)
        try:
            from modules.depositions.jobs.cr_parser import parse_cr
            s["cr"] = parse_cr(tenant, appellate_case_id, embed=False, fact_units=False)
        except Exception:
            logger.exception("cr_parser step failed (non-fatal) for appeal %s", appellate_case_id)
        return s
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# RR ingest via Module A (page:line testimony) + geometry already captured above
# ---------------------------------------------------------------------------

def ingest_rr(tenant_id, appellate_case_id) -> dict:
    from modules.depositions.jobs import trial_pipeline, depo_dag
    tenant = (tenant_id or "").strip()
    conn = _connect()
    out = {"volumes": []}
    try:
        cur = conn.cursor()
        cur.execute("SELECT matter_id::text, trial_id::text, court_of_appeals, "
                    "       appellate_cause_number, trial_court, trial_cause_number, style "
                    "FROM appellate_cases WHERE id=CAST(%s AS uuid)", (str(appellate_case_id),))
        mid, trial_id, coa, appno, tcourt, tcause, style = cur.fetchone()
        # one trial_proceedings = the proceedings below (holds the RR)
        if not trial_id:
            tp = trial_pipeline.create_trial(
                tenant, matter_id=mid, caption=style, cause_number=tcause,
                trial_court=tcourt, court_of_appeals=coa, appellate_cause_number=appno)
            trial_id = tp["trial_id"]
            cur.execute("UPDATE appellate_cases SET trial_id=CAST(%s AS uuid), updated_at=now() "
                        "WHERE id=CAST(%s AS uuid)", (trial_id, str(appellate_case_id)))
            conn.commit()
        cur.execute("SELECT id::text, storage_path, volume, label FROM record_documents "
                    "WHERE appellate_case_id=CAST(%s AS uuid) AND record_kind IN ('RR','SUPP_RR') "
                    "ORDER BY volume NULLS FIRST, sort_order", (str(appellate_case_id),))
        rr_docs = cur.fetchall()
    finally:
        conn.close()

    for rdoc_id, path, volume, label in rr_docs:
        vol = volume or 1
        res = trial_pipeline.run_volume(tenant, trial_id, path, vol, matter_id=mid,
                                        title=label)
        tid = res["register"]["transcript_id"]
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT page_first, page_last FROM deposition_transcripts "
                        "WHERE id=CAST(%s AS uuid)", (tid,))
            pf, pl = cur.fetchone()
            cur.execute("UPDATE record_documents SET rr_transcript_id=CAST(%s AS uuid), "
                        "  page_first=%s, page_last=%s, status='ingested', updated_at=now() "
                        "WHERE id=CAST(%s AS uuid)", (tid, pf, pl, rdoc_id))
            conn.commit()
        finally:
            conn.close()
        out["volumes"].append({"record_document_id": rdoc_id, "transcript_id": tid,
                               "volume": vol, "pages": [pf, pl]})
        try:
            from modules.depositions.jobs.trial_exhibits import extract_exhibits
            extract_exhibits(tenant, tid)
        except Exception:
            logger.exception("extract_exhibits failed (non-fatal) for transcript %s", tid)
    out["trial_id"] = trial_id
    try:
        from modules.depositions.jobs.exhibit_segmenter import segment_exhibits
        out["exhibits"] = segment_exhibits(tenant, appellate_case_id)
    except Exception:
        logger.exception("exhibit_segmenter failed (non-fatal) for appeal %s", appellate_case_id)
    return out


# ---------------------------------------------------------------------------
# record-cite resolver
# ---------------------------------------------------------------------------

_RR_RE = re.compile(
    r"(?:(?P<vol>\d+)\s+)?RR(?:\s+vol\.?\s*(?P<vol2>\d+))?[\s,]+(?:p\.?\s*)?"
    r"(?P<page>\d+)(?:\s*[:.]\s*(?:ll?\.?\s*)?(?P<l1>\d+)(?:\s*[-–]\s*(?P<l2>\d+))?|\s*[-–]\s*(?P<page2>\d+))?",
    re.I)
_CR_RE = re.compile(
    r"(?:(?P<supp>\d+)(?:st|nd|rd|th)?\s+supp(?:lemental)?\.?\s+)?CR[\s,]+(?:p\.?\s*)?(?P<page>\d+)",
    re.I)


def resolve_cite(tenant_id, appellate_case_id, cite: str) -> dict:
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        m = _RR_RE.search(cite or "")
        if m and re.search(r"\bRR\b", cite, re.I):
            vol = m.group("vol") or m.group("vol2")
            page = int(m.group("page"))
            l1 = int(m.group("l1")) if m.group("l1") else None
            l2 = int(m.group("l2")) if m.group("l2") else l1
            page2 = int(m.group("page2")) if m.group("page2") else None
            q = ("SELECT id::text, rr_transcript_id::text, volume FROM record_documents "
                 "WHERE appellate_case_id=CAST(%s AS uuid) AND record_kind IN ('RR','SUPP_RR') "
                 "  AND rr_transcript_id IS NOT NULL")
            params = [str(appellate_case_id)]
            if vol:
                q += " AND volume=%s"; params.append(int(vol))
            q += " ORDER BY volume NULLS FIRST LIMIT 1"
            cur.execute(q, params)
            r = cur.fetchone()
            if not r:
                return {"cite": cite, "kind": "RR", "resolved": False,
                        "reason": "no ingested RR volume%s" % (" "+vol if vol else "")}
            rdoc_id, tid, rvol = r
            lq = ("SELECT page, line, text FROM transcript_lines "
                  "WHERE transcript_id=CAST(%s AS uuid) AND ")
            lp = [tid]
            if page2:
                lq += "page BETWEEN %s AND %s"; lp += [page, page2]
            else:
                lq += "page=%s"; lp += [page]
                if l1 is not None:
                    lq += " AND line BETWEEN %s AND %s"; lp += [l1, l2]
            lq += " ORDER BY page, line"
            cur.execute(lq, lp)
            lines = cur.fetchall()
            text = "\n".join("%2d  %s" % (ln[1], ln[2] or "") for ln in lines)
            locus = "%sRR %d" % ((str(rvol)+" " if rvol else ""), page)
            if page2:
                locus += "-%d" % page2
            elif l1:
                locus += ":%d%s" % (l1, ("-%d" % l2 if l2 and l2 != l1 else ""))
            return {"cite": cite, "kind": "RR", "resolved": bool(lines), "is_record": True,
                    "volume": rvol, "page": page, "lines": [l1, l2] if l1 else None,
                    "locus": locus, "transcript_id": tid, "record_document_id": rdoc_id, "text": text}

        m = _CR_RE.search(cite or "")
        if m:
            page = int(m.group("page"))
            supp = m.group("supp")
            kinds = ("SUPP_CR",) if supp else ("CR",)
            cur.execute(
                "SELECT rp.record_document_id::text, rd.geometry_corpus, rd.geometry_doc_id::text, "
                "       rp.pdf_page, rp.char_start, rp.char_end, rp.page_label, rp.pageno_bbox "
                "FROM record_pages rp JOIN record_documents rd ON rd.id=rp.record_document_id "
                "WHERE rp.appellate_case_id=CAST(%s AS uuid) AND rp.page_number=%s "
                "  AND rd.record_kind = ANY(%s) ORDER BY rd.sort_order LIMIT 1",
                (str(appellate_case_id), page, list(kinds)))
            r = cur.fetchone()
            if not r:
                return {"cite": cite, "kind": "CR", "resolved": False,
                        "reason": "CR page %d not indexed" % page}
            rdoc_id, corpus, gdoc, pdf_page, cs, ce, label, bbox = r
            cur.execute("SELECT substr(canonical_text, %s+1, %s) FROM doc_geometry "
                        "WHERE corpus=%s AND doc_id=CAST(%s AS uuid) AND rendition=%s",
                        (cs, max(ce - cs, 0), corpus, gdoc, GEOM_RENDITION))
            tr = cur.fetchone()
            snippet = (tr[0] if tr else "") or ""
            return {"cite": cite, "kind": "CR", "resolved": True, "is_record": True,
                    "page": page, "locus": "%sCR %d" % (("Supp. " if supp else ""), page),
                    "record_document_id": rdoc_id, "geometry": {"corpus": corpus, "doc_id": gdoc,
                    "pdf_page": pdf_page, "pageno_bbox": bbox}, "page_label": label,
                    "text": snippet[:1200]}
        return {"cite": cite, "resolved": False, "reason": "unrecognized cite format"}
    finally:
        conn.close()


def status(tenant_id, appellate_case_id) -> dict:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT record_kind, is_record, geometry_status, status, page_first, "
                    "       page_last, page_count, label FROM record_documents "
                    "WHERE appellate_case_id=CAST(%s AS uuid) ORDER BY is_record DESC, record_kind, label",
                    (str(appellate_case_id),))
        docs = [{"kind": r[0], "is_record": r[1], "geometry": r[2], "status": r[3],
                 "pages": [r[4], r[5]], "page_count": r[6], "label": r[7]}
                for r in cur.fetchall()]
        cur.execute("SELECT count(*) FROM record_pages WHERE appellate_case_id=CAST(%s AS uuid)",
                    (str(appellate_case_id),))
        return {"record_documents": docs, "record_pages": cur.fetchone()[0]}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Appellate record pipeline (Module B / U4)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--new-appeal", action="store_true")
    ap.add_argument("--attach", action="store_true")
    ap.add_argument("--geometry", action="store_true")
    ap.add_argument("--ingest-rr", action="store_true")
    ap.add_argument("--resolve", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--matter", default=None)
    ap.add_argument("--appeal", default=None)
    ap.add_argument("--coa", default=None)
    ap.add_argument("--appno", default=None)
    ap.add_argument("--trial-court", dest="trial_court", default=None)
    ap.add_argument("--trial-cause", dest="trial_cause", default=None)
    ap.add_argument("--style", default=None)
    ap.add_argument("--appellant", default=None)
    ap.add_argument("--appellee", default=None)
    ap.add_argument("--deadline", default=None)
    ap.add_argument("--cite", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--redo", action="store_true")
    args = ap.parse_args()

    if args.new_appeal:
        out = create_appeal(args.tenant, args.matter, court_of_appeals=args.coa,
                            appellate_cause_number=args.appno, trial_court=args.trial_court,
                            trial_cause_number=args.trial_cause, style=args.style,
                            appellant=args.appellant, appellee=args.appellee,
                            brief_deadline=args.deadline)
    elif args.attach:
        out = attach_records(args.tenant, args.appeal, args.matter)
    elif args.geometry:
        out = capture_geometry(args.tenant, args.appeal, limit=args.limit, redo=args.redo)
    elif args.ingest_rr:
        out = ingest_rr(args.tenant, args.appeal)
    elif args.resolve:
        out = resolve_cite(args.tenant, args.appeal, args.cite)
    elif args.status:
        out = status(args.tenant, args.appeal)
    else:
        ap.error("one of --new-appeal/--attach/--geometry/--ingest-rr/--resolve/--status required")
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
