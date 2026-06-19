"""appellate_brief.py -- Module B / Unit 5: auto-Table-of-Authorities + compliance.

Two deterministic engines over the brief draft, the appellate analog of the
deposition designation-report (the manual grind, gone):

  build_toa()        Run the existing citation extractor (find_citations /
                     CITATION_PATTERNS -- Texas reporters, Tex. codes, Tex./Fed. R.,
                     Const.) over the brief's rendered text, recover case names,
                     map each cite to its brief page (via the geometry \\f page
                     breaks), dedupe, alphabetize within Cases/Statutes/Rules/
                     Constitutional/Other, mark passim (>=5 occurrences) -> the
                     Table of Authorities, persisted to brief_authorities.

  check_compliance() Load appellate_rules (DATA, jurisdiction-swappable) and grade
                     the brief red/green: live word count vs the TRAP 9.4(i) limit,
                     14-pt body font (TRAP 9.4(e), best-effort from the .docx),
                     required-section presence (38.1), appendix presence (38.1(k)).

The brief is rendered docx->pdf (soffice, the depo-report lane) and tokenized
through the same geometry pipeline as the record (corpus='record'), so cite page
mapping and U6/U7 ride the same substrate.

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.appellate_brief --toa --appeal UUID [--brief-doc UUID]
  python -m modules.depositions.jobs.appellate_brief --compliance --appeal UUID [--doctype brief_appellant]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import tempfile

logger = logging.getLogger(__name__)

GEOM_CORPUS = "record"
RENDERED_RENDITION = "rendered_pdf"

# ctype (find_citations) -> TOA group
_TOA_GROUP = {"case": "Cases", "statute": "Statutes", "rule": "Rules",
              "constitution": "Constitutional", "regulation": "Other"}

# 'X v. Y' immediately preceding a reporter cite -> the case name for the TOA line.
# The word char class includes the curly apostrophe (U+2019) so "Nat'l" isn't cut.
_NWORD = r"[A-Z][A-Za-z.&'’\-]+"
_CASE_NAME_RE = re.compile(
    r"(%s(?:,?\s+(?:%s|of|the|and|&|[A-Z]\.))*?)\s+v\.?\s+"
    r"(%s(?:,?\s+(?:%s|of|the|and|&|[A-Z]\.))*)" % (_NWORD, _NWORD, _NWORD, _NWORD))
# leading citation signals to strip off a recovered case name
_SIGNAL_RE = re.compile(
    r"^(?:see also|see, e\.g\.,|see|but see|but cf\.|cf\.|accord|compare|contra|e\.g\.,?|"
    r"quoting|citing|in)\s+", re.I)
# trailing parenthetical with a court/year: (Tex. 2003) / (Tex. App.--Dallas 2018, pet. denied)
_PAREN_RE = re.compile(r"\(([^)]{0,60}\d{4}[^)]*)\)")

# required-section heading detectors (38.1 keys -> heading regex)
_SECTION_HEAD = {
    "identity_of_parties": r"identity of (?:the )?parties",
    "table_of_contents": r"table of contents",
    "index_of_authorities": r"(?:index|table) of authorities",
    "statement_of_the_case": r"statement of the case",
    "statement_on_oral_argument": r"(?:statement (?:regarding|on|concerning) oral argument|oral argument)",
    "issues_presented": r"issues?\s+presented|issues?\s+for review|points? of error",
    "statement_of_facts": r"statement of (?:the )?facts",
    "summary_of_the_argument": r"summary of (?:the )?argument",
    "argument": r"\bargument\b",
    "prayer": r"\bprayer\b|conclusion and prayer",
    "certifications": r"certificate of (?:compliance|service)",
    "appendix": r"\bappendix\b",
}


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


# ---------------------------------------------------------------------------
# brief text / geometry
# ---------------------------------------------------------------------------

def _render_to_pdf(src_path: str) -> str:
    outdir = tempfile.mkdtemp(prefix="brief_render_")
    r = subprocess.run(["soffice", "--headless", "--convert-to", "pdf",
                        "--outdir", outdir, src_path],
                       capture_output=True, text=True, timeout=240)
    pdfs = [f for f in os.listdir(outdir) if f.lower().endswith(".pdf")]
    if not pdfs:
        raise RuntimeError("docx->pdf render failed: %s" % (r.stderr[:300]))
    return os.path.join(outdir, pdfs[0])


def _brief_canonical(tenant, brief_document_id, storage_path):
    """Render (if docx) + tokenize through the geometry pipeline (corpus='record').
    Returns the canonical text (with \\f page breaks for page mapping)."""
    from modules.ediscovery.services import geometry_io
    path = storage_path
    rendition = RENDERED_RENDITION
    if path.lower().endswith(".docx") or path.lower().endswith(".doc"):
        path = _render_to_pdf(storage_path)
    elif path.lower().endswith(".pdf"):
        rendition = "native_pdf"
    out = geometry_io.build_geometry(tenant, GEOM_CORPUS, brief_document_id, path,
                                     rendition=rendition)
    if out is None:
        raise RuntimeError("no text layer in rendered brief")
    canonical, _tokens, _meta = out
    return canonical


def _find_brief(cur, appellate_case_id, brief_document_id=None):
    if brief_document_id:
        cur.execute("SELECT document_id::text, storage_path, label FROM record_documents "
                    "WHERE appellate_case_id=CAST(%s AS uuid) AND document_id=CAST(%s AS uuid)",
                    (str(appellate_case_id), str(brief_document_id)))
        r = cur.fetchone()
        if r:
            return r
    # prefer 'Brief of Appellant', else any BRIEF, newest first
    cur.execute(
        "SELECT document_id::text, storage_path, label FROM record_documents "
        "WHERE appellate_case_id=CAST(%s AS uuid) AND record_kind='BRIEF' "
        "ORDER BY (label ILIKE '%%brief of appellant%%') DESC, "
        "         (storage_path ILIKE '%%.docx') DESC, created_at DESC LIMIT 1",
        (str(appellate_case_id),))
    return cur.fetchone()


# ---------------------------------------------------------------------------
# auto-TOA
# ---------------------------------------------------------------------------

def _case_name_before(text, start):
    seg = text[max(0, start - 160):start]
    m = None
    for m in _CASE_NAME_RE.finditer(seg):
        pass
    if not m:
        return None
    name = re.sub(r"\s+", " ", m.group(0)).strip().strip(",")
    name = _SIGNAL_RE.sub("", name).strip()
    return name[:200]


def _paren_after(text, end):
    seg = text[end:end + 80]
    m = _PAREN_RE.search(seg)
    return ("(" + re.sub(r"\s+", " ", m.group(1)).strip() + ")") if m else None


def build_toa(tenant_id, appellate_case_id, brief_document_id=None) -> dict:
    from modules.ediscovery.services.structural_pass import find_citations
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        brief = _find_brief(cur, appellate_case_id, brief_document_id)
        if not brief:
            return {"error": "no BRIEF document attached to this appeal"}
        bdoc_id, storage_path, label = brief
        canonical = _brief_canonical(tenant, bdoc_id, storage_path)
        cites = find_citations(canonical)

        agg = {}   # norm -> record
        for c in cites:
            norm = c["norm"]
            page = canonical[:c["start"]].count("\f") + 1
            rec = agg.get(norm)
            if rec is None:
                ctype = c["ctype"]
                name = _case_name_before(canonical, c["start"]) if ctype == "case" else None
                paren = _paren_after(canonical, c["end"]) if ctype == "case" else None
                label_line = norm
                if name:
                    label_line = "%s, %s" % (name, norm)
                if paren:
                    label_line += " " + paren
                sort_key = (name or norm).lower().lstrip("the ")
                rec = {"ctype": ctype, "group": _TOA_GROUP.get(ctype, "Other"),
                       "norm": norm, "name": name, "label": label_line,
                       "pages": set(), "sort_key": sort_key}
                agg[norm] = rec
            rec["pages"].add(page)

        # persist
        cur.execute("DELETE FROM brief_authorities WHERE appellate_case_id=CAST(%s AS uuid) "
                    "AND brief_document_id=CAST(%s AS uuid)", (str(appellate_case_id), bdoc_id))
        from psycopg2.extras import execute_values
        rows = []
        for rec in agg.values():
            pages = sorted(rec["pages"])
            occ = len(pages)
            rows.append((tenant, str(appellate_case_id), bdoc_id, rec["group"], rec["ctype"],
                         rec["norm"], rec["name"], rec["label"], pages[0], pages, occ,
                         occ >= 5, rec["sort_key"]))
        if rows:
            execute_values(
                cur,
                "INSERT INTO brief_authorities (tenant_id, appellate_case_id, brief_document_id, "
                "  toa_group, citation_type, citation_text, case_name, toa_label, first_brief_page, "
                "  brief_pages, occurrences, passim, sort_key) VALUES %s",
                rows,
                template="(%s,CAST(%s AS uuid),CAST(%s AS uuid),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                page_size=500)
        conn.commit()

        # build the grouped TOA for return
        GROUP_ORDER = ["Cases", "Statutes", "Rules", "Constitutional", "Other"]
        toa = {}
        for rec in sorted(agg.values(), key=lambda r: (r["group"], r["sort_key"])):
            pages = sorted(rec["pages"])
            entry = {"authority": rec["label"],
                     "pages": "passim" if len(pages) >= 5 else ", ".join(map(str, pages))}
            toa.setdefault(rec["group"], []).append(entry)
        ordered = {g: toa[g] for g in GROUP_ORDER if g in toa}
        return {"brief": label, "brief_document_id": bdoc_id,
                "authorities": len(agg), "table_of_authorities": ordered}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# compliance engine
# ---------------------------------------------------------------------------

def _para_pt(paragraph):
    """Effective body font size (pt) for a paragraph: an explicit run size wins, else
    walk the paragraph-style inheritance chain. None if unresolved."""
    for run in paragraph.runs:
        if run.font.size is not None:
            return round(run.font.size.pt)
    st = paragraph.style
    seen = 0
    while st is not None and seen < 6:
        if getattr(st, "font", None) is not None and st.font.size is not None:
            return round(st.font.size.pt)
        st = getattr(st, "base_style", None)
        seen += 1
    return None


def _dominant_font_pt(path):
    """Best-effort dominant BODY font size (pt) for a .docx: char-weighted over
    non-heading paragraphs, resolving style inheritance (body runs usually carry no
    explicit size). None if unreadable."""
    try:
        import docx
        from collections import Counter
        d = docx.Document(path)
        c = Counter()
        for p in d.paragraphs:
            txt = (p.text or "").strip()
            if not txt:
                continue
            sname = (p.style.name or "").lower() if p.style else ""
            if "heading" in sname or "title" in sname or "toc" in sname:
                continue
            pt = _para_pt(p)
            if pt is not None:
                c[pt] += len(txt)
        if c:
            return c.most_common(1)[0][0]
        st = d.styles["Normal"].font.size
        return round(st.pt) if st is not None else None
    except Exception:
        return None


def check_compliance(tenant_id, appellate_case_id, doc_type="brief_appellant") -> dict:
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT jurisdiction FROM appellate_cases WHERE id=CAST(%s AS uuid)",
                    (str(appellate_case_id),))
        r = cur.fetchone()
        jurisdiction = (r[0] if r else "TX-TRAP") or "TX-TRAP"
        cur.execute("SELECT rule_key, rule_value, rule_cite, verified FROM appellate_rules "
                    "WHERE jurisdiction=%s AND doc_type IN (%s,'*')", (jurisdiction, doc_type))
        rules = {row[0]: {"value": row[1], "cite": row[2], "verified": row[3]}
                 for row in cur.fetchall()}

        brief = _find_brief(cur, appellate_case_id)
        if not brief:
            return {"error": "no BRIEF document attached"}
        bdoc_id, storage_path, label = brief
        canonical = _brief_canonical(tenant, bdoc_id, storage_path)
        words = len(re.findall(r"\S+", canonical))
        low = canonical.lower()

        checks = []

        # word limit
        if "word_limit" in rules:
            lim = int(rules["word_limit"]["value"])
            checks.append({
                "requirement": "Word count <= %d" % lim, "rule": rules["word_limit"]["cite"],
                "status": "pass" if words <= lim else "fail",
                "detail": "%d words (limit %d; note: TRAP 9.4(i) excludes caption/ToC/ToA/"
                          "signature/certificate/appendix, so the certified count is <= this)"
                          % (words, lim),
                "verified": rules["word_limit"]["verified"]})

        # body font (best-effort, docx only)
        if "body_font_pt" in rules:
            want = int(rules["body_font_pt"]["value"])
            got = _dominant_font_pt(storage_path) if storage_path.lower().endswith(".docx") else None
            if got is None:
                st = "manual"
                det = "could not read font automatically -- verify %d-pt body manually" % want
            else:
                st = "pass" if got == want else "warn"
                det = "dominant body font %d-pt (need %d-pt)" % (got, want)
            checks.append({"requirement": "%d-pt body font" % want,
                           "rule": rules["body_font_pt"]["cite"], "status": st, "detail": det,
                           "verified": rules["body_font_pt"]["verified"]})

        # required sections present
        if "required_sections" in rules:
            req = rules["required_sections"]["value"]
            if isinstance(req, str):
                req = json.loads(req)
            present, missing = [], []
            for key in req:
                rx = _SECTION_HEAD.get(key)
                (present if (rx and re.search(rx, low)) else missing).append(key)
            checks.append({
                "requirement": "Required sections present (TRAP 38.1)",
                "rule": rules["required_sections"]["cite"],
                "status": "pass" if not missing else "warn",
                "detail": {"present": present, "missing": missing},
                "verified": rules["required_sections"]["verified"]})

        # appendix presence + 38.1(k)(1) checklist
        if "appendix_contents" in rules:
            cur.execute("SELECT count(*) FROM record_documents WHERE appellate_case_id=CAST(%s AS uuid) "
                        "AND record_kind='APPENDIX'", (str(appellate_case_id),))
            n_appx = cur.fetchone()[0]
            req = rules["appendix_contents"]["value"]
            if isinstance(req, str):
                req = json.loads(req)
            checks.append({
                "requirement": "Appendix present + required contents (TRAP 38.1(k)(1))",
                "rule": rules["appendix_contents"]["cite"],
                "status": "pass" if n_appx > 0 else "fail",
                "detail": {"appendix_documents_attached": n_appx,
                           "required_contents_checklist": req,
                           "note": "presence checked; per-item contents verification is manual"},
                "verified": rules["appendix_contents"]["verified"]})

        summary = {"pass": sum(1 for c in checks if c["status"] == "pass"),
                   "fail": sum(1 for c in checks if c["status"] == "fail"),
                   "warn": sum(1 for c in checks if c["status"] == "warn"),
                   "manual": sum(1 for c in checks if c["status"] == "manual")}
        return {"brief": label, "jurisdiction": jurisdiction, "doc_type": doc_type,
                "word_count": words, "summary": summary, "checks": checks,
                "note": "rule values are seed data (verified flag); confirm current text "
                        "vs txcourts.gov before filing"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Appellate brief TOA + compliance (Module B / U5)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--toa", action="store_true")
    ap.add_argument("--compliance", action="store_true")
    ap.add_argument("--appeal", required=True)
    ap.add_argument("--brief-doc", dest="brief_doc", default=None)
    ap.add_argument("--doctype", default="brief_appellant")
    args = ap.parse_args()
    if args.toa:
        out = build_toa(args.tenant, args.appeal, args.brief_doc)
    elif args.compliance:
        out = check_compliance(args.tenant, args.appeal, args.doctype)
    else:
        ap.error("one of --toa/--compliance required")
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
