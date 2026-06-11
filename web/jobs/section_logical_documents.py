# -*- coding: utf-8 -*-
"""
section_logical_documents.py — per-logical-document sectioner (v2).

v2 grammar fixes (general litigation/contract patterns, not doc-specific):
  - caption furniture excluded from sections (court header / case no / § / court
    division like "CIVIL DEPARTMENT")
  - title finder requires a document-type keyword (matches the boundary detector),
    fixing "CIVIL DEPARTMENT" being grabbed as title
  - COUNT detection handles Roman numerals + en/em dashes (COUNT I – NEGLIGENCE)
  - named pleading sections typed: prayer / certificate / verification / jury_demand
  - signature/firm blocks typed signature_block (LLP/PLLC/P.C./ATTORNEYS FOR/...)
  - contract grammar handles numbered-clause style "1. Heading." (non-bold),
    not just "Article N" / "Section N.N"

Everything else (span-bounded sectioning, absolute offsets, own-text content,
soft-supersede commit, logical_document_id stamping) is unchanged from v1.

Usage:
  dry:    docker exec -i -w /app praesidium-web python - <doc_id> < this_file
  commit: docker exec -i -w /app praesidium-web python - <doc_id> commit < this_file
"""
import sys, re
sys.path.insert(0, "/app")
import psycopg2, psycopg2.extras
from collections import defaultdict
from modules.ediscovery.services.geometry_service import _conn_kwargs
from modules.ediscovery.services.geometry_io import load_geometry

TEN = "986c0fee-1390-43bb-ad28-8cd1db6de53f"

def _norm(s): return re.sub(r"[^A-Za-z]", "", s).upper()
_CONNECT = {"AND", "OF", "OR", "THE", "A", "AN", "TO", "FOR", "IN", "ON",
            "WITH", "BY", "UNDER", "AS", "AT", "AND/OR"}

def reconstruct_lines(tokens):
    lm = defaultdict(list)
    for t in tokens:
        lm[(t.page_number, round(t.y, 3))].append(t)
    out = []
    for (pg, y), ts in lm.items():
        ts.sort(key=lambda t: t.x)
        txt = " ".join(t.text for t in ts)
        alpha = [c for c in txt if c.isalpha()]
        xmin = min(t.x for t in ts); xmax = max(t.x + t.w for t in ts)
        out.append(dict(pg=pg, y=y, text=txt,
                        bold=sum(t.is_bold for t in ts) / len(ts) >= 0.5,
                        caps=(sum(c.isupper() for c in alpha) / len(alpha)) if alpha else 0.0,
                        centered=abs((xmin + xmax) / 2 - 0.5) < 0.08 and xmin > 0.15,
                        cs=min(t.char_start for t in ts)))
    out.sort(key=lambda l: (l["pg"], l["y"]))
    return out

def filter_furniture(lines, npages):
    pof = defaultdict(set)
    for l in lines: pof[_norm(l["text"])].add(l["pg"])
    rc = max(3, int(npages * 0.4))
    return [l for l in lines
            if not (l["y"] > 0.90 or l["y"] < 0.07 or len(pof[_norm(l["text"])]) >= rc)]

# --- markers ---
_ROMAN   = re.compile(r"^\s*([IVXLCDM]+)\.\s")
_COUNT   = re.compile(r"(?i)^\s*COUNT\s+(?:\d+|[IVXLCDM]+)\b")          # digit OR roman
_NUMPARA = re.compile(r"^\s*\d+\.\s")
_NUMSEC  = re.compile(r"^\s*(\d+(?:\.\d+)*)\.\s")                       # contract "1." / "1.1."
_ARTICLE = re.compile(r"(?i)^\s*Article\s+[0-9IVXLC]+\b")
_SECTION = re.compile(r"(?i)^\s*Section\s+\d+(?:\.\d+)?\b")
_RECIT   = re.compile(r"(?i)^\s*RECITALS\b")
_PREAMBLE = re.compile(r"(?i)^\s*THIS\s+[A-Z]")
# caption furniture
COURT_RE = re.compile(
    r"(?i)\b(?:IN\s+THE\b.{0,80}?\b(?:COURT|DISTRICT|DIVISION)\b"
    r"|(?:DISTRICT|SUPERIOR|CIRCUIT|SUPREME|COUNTY|MUNICIPAL|CHANCERY|PROBATE|BANKRUPTCY)\s+COURT\b"
    r"|JUDICIAL\s+DISTRICT\b|COURT\s+OF\s+(?:COMMON\s+PLEAS|APPEALS?|CHANCERY))")
CASENO_RE = re.compile(r"(?i)\b(?:CAUSE|CASE|CIVIL\s+ACTION|DOCKET)\s+NO\b|\bCASE\s+NUMBER\b")
CIVDIV_RE = re.compile(r"(?i)^\s*(?:CIVIL|PROBATE|CHANCERY|CRIMINAL|FAMILY|LAW)\s+(?:DEPARTMENT|DIVISION)\s*$"
                       r"|^\s*AT\s+LAW\s*$")
def is_caption_line(t):
    return bool(COURT_RE.search(t) or CASENO_RE.search(t) or "\u00a7" in t or CIVDIV_RE.match(t))
# titles
PLEAD_KEYS = (r"PETITION|COMPLAINT|ANSWER|MOTION|COUNTERCLAIM|CROSS-?CLAIM|PLEA|BRIEF"
              r"|RESPONSE|REPLY|ORDER|NOTICE|AFFIDAVIT|DECLARATION|STIPULATION|SUBPOENA")
CONTRACT_KEYS = (r"AGREEMENT|LEASE|DEED|PROMISSORY\s+NOTE|\bNOTE\b|GUARANTY|GUARANTEE"
                 r"|ASSIGNMENT|AMENDMENT|ADDENDUM|CONTRACT|MEMORANDUM\s+OF")
TITLE_KEYS = re.compile(r"(?i)\b(?:%s|%s)\b" % (PLEAD_KEYS, CONTRACT_KEYS))
# named pleading sections + signature
NAMED = [
    (re.compile(r"(?i)^\s*PRAYER\b"), "prayer"),
    (re.compile(r"(?i)^\s*CERTIFICATE\s+OF\s+SERVICE\b"), "certificate"),
    (re.compile(r"(?i)^\s*VERIFICATION\b"), "verification"),
    (re.compile(r"(?i)^\s*(?:DEMAND\s+FOR\s+JURY|JURY\s+(?:TRIAL\s+)?DEMAND)\b"), "jury_demand"),
]
SIG_RE = re.compile(r"(?i)\bLLP\b|\bPLLC\b|\bL\.L\.P\.|\bP\.C\.|\bATTORNEYS\s+FOR\b"
                    r"|\bRESPECTFULLY\s+SUBMITTED\b|^\s*By:\s")

def pleading_grammar(l):
    t = l["text"]
    if is_caption_line(t):
        return None
    if l["bold"] and l["caps"] >= 0.6 and len(re.sub(r"[^A-Za-z]", "", t)) >= 3:
        if _ROMAN.match(t): return ("division", 0)
        if _COUNT.match(t): return ("cause_of_action", 1)
        for rx, typ in NAMED:
            if rx.match(t): return (typ, 1)
        if SIG_RE.search(t): return ("signature_block", 1)
        return ("subsection", 1)
    if not l["bold"] and _NUMPARA.match(t):
        return ("numbered_paragraph", 2)
    return None

def contract_grammar(l):
    t = l["text"]
    if is_caption_line(t):
        return None
    if l["bold"] and _ARTICLE.match(t): return ("article", 0)
    if l["bold"] and _SECTION.match(t): return ("section", 1)
    if l["bold"] and _RECIT.match(t):   return ("recitals", 0)
    m = _NUMSEC.match(t)
    if m:
        return ("section", m.group(1).count("."))     # "1"->0, "1.1"->1, "1.1.1"->2
    return None

GRAMMARS = {"pleading": pleading_grammar, "motion": pleading_grammar, "brief": pleading_grammar,
            "contract": contract_grammar, "real_estate": contract_grammar, "loi": contract_grammar}

def _starts_new(t, pt):
    if pt in ("contract", "real_estate", "loi"):
        return bool(_ARTICLE.match(t) or _SECTION.match(t) or _RECIT.match(t) or _NUMSEC.match(t))
    return bool(_ROMAN.match(t) or _COUNT.match(t) or _NUMPARA.match(t))

def detect_title(content, pt, first_pg):
    title = []
    for l in content:
        if l["pg"] != first_pg:
            break
        t = l["text"]
        if is_caption_line(t):
            continue
        if _PREAMBLE.match(t) or _starts_new(t, pt):
            break
        if l["centered"] and l["caps"] >= 0.8 and l["bold"]:
            if not title and not TITLE_KEYS.search(t):
                continue                       # only START the title on a doc-type keyword line
            title.append(l)
        elif title:
            break
    return title

def section_span(canonical, tokens, pt, span_start, span_end, first_pg, npages_c):
    toks = [t for t in tokens if span_start <= t.char_start < span_end]
    content = filter_furniture(reconstruct_lines(toks), npages_c)
    grammar = GRAMMARS.get(pt)
    if grammar is None:
        return []
    title_lines = detect_title(content, pt, first_pg)
    tset = set(id(t) for t in title_lines)
    markers = []
    if title_lines:
        markers.append(dict(type="title", level=0, pg=title_lines[0]["pg"],
                            label=" ".join(t["text"] for t in title_lines), cs=title_lines[0]["cs"]))
    prev_h = None; prev_line_h = False
    for l in content:
        if id(l) in tset:
            prev_line_h = False; continue
        m = grammar(l)
        if m is None:
            prev_line_h = False; prev_h = None; continue
        typ, lvl = m
        if (typ != "numbered_paragraph" and prev_line_h and prev_h is not None
                and not _starts_new(l["text"], pt)):
            words = prev_h["label"].split()
            lw = _norm(words[-1]) if words else ""
            if lw in _CONNECT:
                prev_h["label"] = (prev_h["label"] + " " + l["text"]).strip()
                continue
        node = dict(type=typ, level=lvl, pg=l["pg"], label=l["text"].strip(), cs=l["cs"])
        markers.append(node)
        if typ != "numbered_paragraph":
            prev_h = node; prev_line_h = True
        else:
            prev_h = None; prev_line_h = False
    markers.sort(key=lambda m: m["cs"])

    secs = []; stack = []
    for i, m in enumerate(markers):
        ce = markers[i + 1]["cs"] if i + 1 < len(markers) else span_end
        while stack and stack[-1][0] >= m["level"]:
            stack.pop()
        parent = stack[-1][1] if stack else None
        secs.append(dict(local=i, type=m["type"], depth=m["level"], pg=m["pg"],
                         label=m["label"][:120], cs=m["cs"], ce=ce, parent_local=parent))
        stack.append((m["level"], i))
    child_min = {}
    for s in secs:
        p = s["parent_local"]
        if p is not None:
            child_min[p] = min(child_min.get(p, 1 << 60), s["cs"])
    for s in secs:
        own_end = child_min.get(s["local"], s["ce"])
        s["content"] = (canonical[s["cs"]:own_end].strip() or s["label"])
    return secs

# ---------- load ----------
DOC = sys.argv[1]
MODE = sys.argv[2] if len(sys.argv) > 2 else "dry"

conn = psycopg2.connect(**_conn_kwargs()); conn.autocommit = True
cur = conn.cursor()
cur.execute("SELECT file_path FROM dms_documents WHERE id=%s::uuid AND TRIM(tenant_id)=%s", (DOC, TEN))
row = cur.fetchone()
if not row:
    print("no such dms_document for tenant"); sys.exit(1)
path = row[0]
cur.execute("""SELECT id, char_start, char_end, page_start, page_end, doc_role, parse_type, title
               FROM logical_documents
               WHERE TRIM(tenant_id)=%s AND source_corpus='dms' AND source_file_id=%s::uuid
               ORDER BY char_start""", (TEN, DOC))
ldocs = cur.fetchall()
conn.close()
if not ldocs:
    print("no logical_documents for this file — run write_logical_documents.py first"); sys.exit(2)

_g = load_geometry("dms", DOC)
if _g is None:
    print("no persisted geometry - run write_logical_documents.py (boundary) first"); sys.exit(5)
canonical, _tokens, _gmeta = _g

plan = []
for (lid, cs, ce, pg0, pg1, role, ptype, title) in ldocs:
    npages_c = max(1, (pg1 or pg0) - pg0 + 1)
    secs = section_span(canonical, _tokens, ptype, cs, ce, pg0, npages_c)
    plan.append(dict(lid=lid, cs=cs, ce=ce, pg0=pg0, pg1=pg1, role=role,
                     ptype=ptype, title=title, secs=secs))

print("file:", path.rsplit("/", 1)[-1], "| logical docs:", len(ldocs))
total = 0
for d in plan:
    by = defaultdict(int)
    for s in d["secs"]: by[s["type"]] += 1
    total += len(d["secs"])
    print("=" * 96)
    print("[%s] %s  pp%d-%s  type=%s  sections=%d  %s"
          % (d["role"], (d["title"] or "(no title)")[:48], d["pg0"], d["pg1"], d["ptype"],
             len(d["secs"]), dict(by)))
    shown = 0
    for s in d["secs"]:
        if s["type"] == "numbered_paragraph":
            continue
        par = "" if s["parent_local"] is None else " <-%d" % s["parent_local"]
        print("   %3d d%d %s%-16s %-50s [%d:%d]%s"
              % (s["local"], s["depth"], "  " * s["depth"], s["type"], s["label"][:48], s["cs"], s["ce"], par))
        shown += 1
        if shown > 36:
            print("   ... (truncated)"); break
    leaves = [s for s in d["secs"] if s["type"] == "numbered_paragraph"]
    if leaves:
        print("   ... numbered_paragraph leaves: %d" % len(leaves))
print("=" * 96)
print("total sections across file:", total)
bad = 0
for d in plan:
    ss = d["secs"]
    for i in range(len(ss) - 1):
        if ss[i]["cs"] > ss[i + 1]["cs"]: bad += 1
    for s in ss:
        if s["ce"] < s["cs"] or s["cs"] < d["cs"] or s["ce"] > d["ce"]: bad += 1
print("integrity violations (order / in-bounds):", bad)

if MODE != "commit":
    print("\nDRY RUN — no writes. Re-run with 'commit' to persist.")
    sys.exit(0)
if bad:
    print("\nABORT: integrity violations; refusing to commit."); sys.exit(3)

conn = psycopg2.connect(**_conn_kwargs()); conn.autocommit = False
try:
    with conn.cursor() as c:
        c.execute("""
            INSERT INTO extraction_runs
              (tenant_id, run_type, source_type, source_path, status,
               document_count, documents_processed, extraction_model, started_at, completed_at)
            VALUES (%s,'sectioning','dms',%s,'completed',%s,%s,'layout_sectioner_v2',now(),now())
            RETURNING id
        """, (TEN, path, len(ldocs), len(ldocs)))
        run_id = c.fetchone()[0]
        c.execute("""UPDATE document_sections SET superseded_by_run_id=%s
                     WHERE dms_document_id=%s::uuid AND superseded_by_run_id IS NULL""", (run_id, DOC))
        superseded = c.rowcount
        gidx = 0; ninserted = 0
        for d in plan:
            local_to_id = {}
            for s in d["secs"]:
                parent_id = local_to_id.get(s["parent_local"]) if s["parent_local"] is not None else None
                attrs = {"method": "layout", "parse_type": d["ptype"]}
                c.execute("""
                    INSERT INTO document_sections
                      (tenant_id, dms_document_id, logical_document_id, extraction_run_id,
                       section_index, section_type, section_label, content,
                       char_start, char_end, page_start, page_end,
                       nesting_depth, parent_section_id, attributes)
                    VALUES (%s,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (TEN, DOC, d["lid"], run_id, gidx, s["type"], s["label"], s["content"],
                      s["cs"], s["ce"], s["pg"], d["pg1"], s["depth"], parent_id,
                      psycopg2.extras.Json(attrs)))
                local_to_id[s["local"]] = c.fetchone()[0]
                gidx += 1; ninserted += 1
    conn.commit()
    print("\nCOMMITTED.")
    print("  sectioning run:", run_id)
    print("  superseded prior sections:", superseded)
    print("  sections inserted:", ninserted, "across", len(ldocs), "logical documents")
    with conn.cursor() as c:
        c.execute("""
            SELECT ld.doc_role, ld.parse_type, count(s.*) AS nsec,
                   left(coalesce(ld.title,'(no title)'),40) AS title
            FROM logical_documents ld
            LEFT JOIN document_sections s
              ON s.logical_document_id = ld.id AND s.superseded_by_run_id IS NULL
            WHERE TRIM(ld.tenant_id)=%s AND ld.source_corpus='dms' AND ld.source_file_id=%s::uuid
            GROUP BY ld.id, ld.doc_role, ld.parse_type, ld.title, ld.char_start
            ORDER BY ld.char_start
        """, (TEN, DOC))
        print("\n  per logical document (current sections):")
        for r in c.fetchall():
            print("    %-10s %-8s %4d sections  %s" % (r[0], r[1], r[2], r[3]))
except Exception:
    conn.rollback()
    raise
finally:
    conn.close()
