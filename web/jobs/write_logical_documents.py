# -*- coding: utf-8 -*-
"""
write_logical_documents.py — boundary WRITER (v1).

Runs the validated v2 boundary detector, then on --commit persists, in ONE atomic
transaction:
  1. doc_layout_tokens for the file  (the geometry spine; mirrors geometry_service
     persist: DELETE by (corpus,doc_id,rendition) + execute_values + verify_offsets
     §0 gate) — so the spine and the spans come from the SAME extraction and cannot
     drift apart.
  2. an extraction_runs row           (run_type='boundary_detection', provenance)
  3. logical_documents rows           (base + exhibits, or one standalone)
  4. document_relationships edges      (rel_type='exhibit', the nesting tree)

Re-run safe: deletes prior logical_documents for this (tenant,corpus,source_file_id)
first; the ON DELETE CASCADE on document_relationships clears their edges. Each
commit records a fresh extraction_run (provenance history).

matter_id is left NULL — dms_documents is path-keyed and carries no matter_id;
it backfills from the folder->matter mapping later. Provenance holds via
source_file_id regardless.

NOTE: detection functions are copied verbatim from the validated /tmp/boundary_detector.py
(v2). When this is promoted into modules/, the two collapse into one import.

Usage:
  dry-run (default): docker exec -i -w /app praesidium-web python - <doc_id> [base_pt] < this_file
  commit:            docker exec -i -w /app praesidium-web python - <doc_id> <base_pt> commit < this_file
"""
import sys, re, json
sys.path.insert(0, "/app")
import psycopg2, psycopg2.extras
from collections import defaultdict
from modules.ediscovery.services.geometry_service import _conn_kwargs
from modules.ediscovery.services.geometry_extraction import extract_pdf_with_geometry, verify_offsets

TEN = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
RENDITION = "native_pdf"

# ====================== detection (verbatim from boundary_detector v2) =====================
def _norm(s): return re.sub(r"[^A-Za-z]", "", s).upper()

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

COURT_RE = re.compile(
    r"(?i)\b(?:IN\s+THE\b.{0,80}?\b(?:COURT|DISTRICT|DIVISION)\b"
    r"|(?:DISTRICT|SUPERIOR|CIRCUIT|SUPREME|COUNTY|MUNICIPAL|CHANCERY|PROBATE|BANKRUPTCY)\s+COURT\b"
    r"|JUDICIAL\s+DISTRICT\b"
    r"|COURT\s+OF\s+(?:COMMON\s+PLEAS|APPEALS?|CHANCERY))")
CASENO_RE  = re.compile(r"(?i)\b(?:CAUSE|CASE|CIVIL\s+ACTION|DOCKET)\s+NO\b|\bNO\.\s*[0-9][0-9A-Za-z\-]*"
                        r"|\bCASE\s+NUMBER\b")
EXHIBIT_RE = re.compile(r"(?i)^\s*EXHIBIT\s+([A-Z0-9]{1,4})\b")
PLEAD_KEYS = (r"PETITION|COMPLAINT|ANSWER|MOTION|COUNTERCLAIM|CROSS-?CLAIM|PLEA|BRIEF"
              r"|RESPONSE|REPLY|ORDER|NOTICE|AFFIDAVIT|DECLARATION|STIPULATION|SUBPOENA")
CONTRACT_KEYS = (r"AGREEMENT|LEASE|DEED|PROMISSORY\s+NOTE|\bNOTE\b|GUARANTY|GUARANTEE"
                 r"|ASSIGNMENT|AMENDMENT|ADDENDUM|CONTRACT|MEMORANDUM\s+OF")
TITLE_KEYS = re.compile(r"(?i)\b(?:%s|%s)\b" % (PLEAD_KEYS, CONTRACT_KEYS))

def repeats(lines, npages):
    pof = defaultdict(set)
    for l in lines: pof[_norm(l["text"])].add(l["pg"])
    rc = max(4, int(npages * 0.4))
    return {k for k, v in pof.items() if len(v) >= rc and k}

def page_index(lines, rep):
    pg = defaultdict(list)
    for l in lines:
        if _norm(l["text"]) in rep: continue
        pg[l["pg"]].append(l)
    for p in pg: pg[p].sort(key=lambda l: l["y"])
    return pg

def _has_title(top):
    for l in top:
        if COURT_RE.search(l["text"]) or CASENO_RE.search(l["text"]): continue
        if l["centered"] and l["caps"] >= 0.7 and TITLE_KEYS.search(l["text"]): return True
    return False

def is_caption(plines):
    top = [l for l in plines if l["y"] <= 0.45][:20] or plines[:8]
    court = any(COURT_RE.search(l["text"]) and (l["caps"] >= 0.5 or l["centered"]) for l in top)
    caseno = any(CASENO_RE.search(l["text"]) for l in top)
    sect = sum(1 for l in top if "\u00a7" in l["text"]) >= 3
    return court and (caseno or sect or _has_title(top))

def stamp_letter(plines):
    top = sorted([l for l in plines if l["y"] < 0.12], key=lambda l: l["y"])
    saw = False
    for l in top:
        s = l["text"].strip().upper()
        if s == "EXHIBIT": saw = True; continue
        if saw and re.fullmatch(r"[A-Z0-9]{1,3}", s): return s
    return None

def slip_label(plines):
    if len(plines) > 6: return None
    for l in plines:
        m = EXHIBIT_RE.match(l["text"])
        if m and l["centered"]: return m.group(1)
    return None

def find_title(start_pg, end_pg, pg):
    hits = []
    for p in range(start_pg, min(end_pg, start_pg + 2) + 1):
        for l in pg.get(p, []):
            if l["y"] > 0.66: continue
            t = l["text"]
            if COURT_RE.search(t) or CASENO_RE.search(t) or "\u00a7" in t: continue
            if l["centered"] and l["caps"] >= 0.85 and TITLE_KEYS.search(t):
                hits.append((p, l["y"], t.strip()))
    if not hits: return None
    p0, y0, _ = hits[0]
    merged = [t for (p, y, t) in hits if p == p0 and y0 - 0.001 <= y <= y0 + 0.05]
    return " ".join(merged).strip()

def parse_type_of(title, role, base_pt):
    if role in ("base", "standalone"): return base_pt
    if title:
        if re.search(r"(?i)\b(?:%s)\b" % CONTRACT_KEYS, title): return "contract"
        if re.search(r"(?i)\b(?:MOTION|RESPONSE|REPLY|BRIEF|PLEA)\b", title): return "motion"
        if re.search(r"(?i)\b(?:%s)\b" % PLEAD_KEYS, title): return "pleading"
    return "pleading"

def detect(canonical, tokens, npages):
    lines = reconstruct_lines(tokens)
    rep = repeats(lines, npages)
    pg = page_index(lines, rep)
    page_cs = {}
    for t in tokens:
        if t.page_number not in page_cs or t.char_start < page_cs[t.page_number]:
            page_cs[t.page_number] = t.char_start
    pages = sorted(pg.keys())
    raw = []
    for p in pages:
        pl = pg[p]
        sl = slip_label(pl)
        if sl:
            raw.append(dict(pg=p, kind="slip", stamp=None, slip=sl))
        elif is_caption(pl):
            raw.append(dict(pg=p, kind="caption", stamp=stamp_letter(pl), slip=None))
    if not raw or raw[0]["pg"] != pages[0]:
        raw.insert(0, dict(pg=pages[0], kind="caption", stamp=stamp_letter(pg[pages[0]]), slip=None))
    raw.sort(key=lambda b: b["pg"])
    cons = []
    for i, b in enumerate(raw):
        nxt = raw[i + 1]["pg"] if i + 1 < len(raw) else None
        cons.append(dict(ord=i, start_pg=b["pg"], end_pg=(nxt - 1) if nxt else pages[-1],
                         cs=page_cs[b["pg"]], ce=(page_cs[nxt] if nxt else len(canonical)),
                         kind=b["kind"], stamp=b["stamp"], slip=b["slip"]))
    return cons, pg

def enrich(cons, pg, base_pt):
    single = len(cons) == 1
    caption_seq = 0
    last_caption_ord = 0
    sib = defaultdict(int)
    for c in cons:
        c["role"] = "standalone" if single else ("base" if c["ord"] == 0 else "exhibit")
        c["title"] = find_title(c["start_pg"], c["end_pg"], pg)
        c["ptype"] = parse_type_of(c["title"], c["role"], base_pt)
        if c["role"] == "exhibit" and c["kind"] == "caption":
            lbl = c["stamp"] or chr(ord("A") + caption_seq)
            c["parent"] = 0; c["label"] = "Exhibit %s" % lbl
            c["ordinal"] = sib[0]; sib[0] += 1
            caption_seq += 1; last_caption_ord = c["ord"]
        elif c["role"] == "exhibit" and c["kind"] == "slip":
            c["parent"] = last_caption_ord
            c["label"] = "Exhibit %s" % (c["slip"] or "?")
            c["ordinal"] = sib[last_caption_ord]; sib[last_caption_ord] += 1
        else:
            c["parent"] = None; c["label"] = "-"; c["ordinal"] = None
    return cons

def print_plan(path, ncanon, npages, cons):
    print("file:", path.rsplit("/", 1)[-1], "| pages:", npages, "| canonical:", ncanon, "| constituents:", len(cons))
    print("-" * 104)
    for c in cons:
        par = "-" if c["parent"] is None else ("[%d]" % c["parent"])
        print("  [%d] %-10s pp %3d-%-3d chars[%7d:%-7d] type=%-8s parent=%-4s %-11s %s"
              % (c["ord"], c["role"], c["start_pg"], c["end_pg"], c["cs"], c["ce"],
                 c["ptype"], par, c["label"], (c["title"] or "(no title)")[:54]))
    print("-" * 104)
    mono = all(cons[i]["cs"] < cons[i + 1]["cs"] for i in range(len(cons) - 1))
    cover = cons[0]["cs"] == 0 and cons[-1]["ce"] == ncanon
    gaps = sum(1 for i in range(len(cons) - 1) if cons[i]["ce"] != cons[i + 1]["cs"])
    print("spans strictly increasing:", mono, "| full coverage:", cover, "| gaps:", gaps)
    return mono and cover and gaps == 0

# ============================== main ==============================
DOC = sys.argv[1]
BASE_PT = sys.argv[2] if len(sys.argv) > 2 else "motion"
MODE = sys.argv[3] if len(sys.argv) > 3 else "dry"

conn = psycopg2.connect(**_conn_kwargs()); conn.autocommit = True
cur = conn.cursor()
cur.execute("SELECT file_path FROM dms_documents WHERE id=%s::uuid AND TRIM(tenant_id)=%s", (DOC, TEN))
row = cur.fetchone()
if not row:
    print("no such dms_document for tenant"); sys.exit(1)
path = row[0]
conn.close()

res = extract_pdf_with_geometry(path)
if res is None or not res.has_text_layer:
    print("no text layer — needs OCR; aborting"); sys.exit(2)
cons, pg = detect(res.canonical_text, res.tokens, res.page_count)
cons = enrich(cons, pg, BASE_PT)
ok = print_plan(path, len(res.canonical_text), res.page_count, cons)

if MODE != "commit":
    print("\nDRY RUN — no writes. Re-run with 'commit' to persist.")
    sys.exit(0)

if not ok:
    print("\nABORT: span integrity check failed; refusing to commit."); sys.exit(3)

ok_off, bad_off = verify_offsets(res)
if bad_off:
    print("\nABORT: §0 offset self-check failed (%d bad); refusing to persist geometry." % bad_off); sys.exit(4)

# ---- atomic write ----
conn = psycopg2.connect(**_conn_kwargs()); conn.autocommit = False
try:
    with conn.cursor() as c:
        # 1+1b. geometry tokens (incl. block/line) + canonical header --
        #        single source of truth (geometry_io.persist_geometry).
        from modules.ediscovery.services import geometry_io
        geometry_io.persist_geometry(c.connection, TEN, "dms", DOC, RENDITION, res)
        trows = res.tokens  # for the post-commit count print

        # 2. supersede prior logical documents for this file (cascade clears their edges)
        c.execute("""DELETE FROM logical_documents
                     WHERE TRIM(tenant_id)=%s AND source_corpus='dms' AND source_file_id=%s::uuid""",
                  (TEN, DOC))
        superseded = c.rowcount

        # 3. extraction run (provenance)
        c.execute("""
            INSERT INTO extraction_runs
              (tenant_id, run_type, source_type, source_path, status,
               document_count, documents_processed, extraction_model, started_at, completed_at)
            VALUES (%s,'boundary_detection','dms',%s,'completed',%s,%s,'layout_boundary_v2',now(),now())
            RETURNING id
        """, (TEN, path, len(cons), len(cons)))
        run_id = c.fetchone()[0]

        # 4. logical_documents (uniform anchor: every constituent is a row)
        id_of = {}
        for cc in cons:
            attrs = {"detector": "layout_boundary_v2", "kind": cc["kind"],
                     "exhibit_stamp": cc["stamp"], "slip_label": cc["slip"]}
            c.execute("""
                INSERT INTO logical_documents
                  (tenant_id, matter_id, source_corpus, source_file_id, char_start, char_end,
                   page_start, page_end, doc_role, parse_type, title, extraction_run_id, attributes)
                VALUES (%s, NULL, 'dms', %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (TEN, DOC, cc["cs"], cc["ce"], cc["start_pg"], cc["end_pg"],
                  cc["role"], cc["ptype"], cc["title"], run_id, psycopg2.extras.Json(attrs)))
            id_of[cc["ord"]] = c.fetchone()[0]

        # 5. exhibit relationship edges (the nesting tree)
        nrel = 0
        for cc in cons:
            if cc["role"] != "exhibit":
                continue
            c.execute("""
                INSERT INTO document_relationships
                  (tenant_id, from_doc_id, to_doc_id, rel_type, exhibit_label, ordinal, attributes)
                VALUES (%s, %s, %s, 'exhibit', %s, %s, %s)
            """, (TEN, id_of[cc["parent"]], id_of[cc["ord"]], cc["label"], cc["ordinal"],
                  psycopg2.extras.Json({"nested": cc["kind"] == "slip"})))
            nrel += 1

    conn.commit()
    print("\nCOMMITTED.")
    print("  tokens persisted:", len(trows), "| superseded prior logical_documents:", superseded)
    print("  extraction_run:", run_id)
    print("  logical_documents inserted:", len(cons), "| exhibit edges:", nrel)
    # verify
    with conn.cursor() as c:
        c.execute("""SELECT doc_role, parse_type, char_start, char_end, page_start, page_end, title
                     FROM logical_documents
                     WHERE TRIM(tenant_id)=%s AND source_corpus='dms' AND source_file_id=%s::uuid
                     ORDER BY char_start""", (TEN, DOC))
        print("\n  persisted logical_documents:")
        for r in c.fetchall():
            print("    %-10s %-8s [%7d:%-7d] pp%3d-%-3d %s"
                  % (r[0], r[1], r[2], r[3], r[4], r[5], (r[6] or "(no title)")[:50]))
        c.execute("""SELECT count(*) FROM document_relationships
                     WHERE TRIM(tenant_id)=%s AND from_doc_id IN
                       (SELECT id FROM logical_documents
                        WHERE TRIM(tenant_id)=%s AND source_file_id=%s::uuid)""",
                  (TEN, TEN, DOC))
        print("  relationship edges from this file's docs:", c.fetchone()[0])
except Exception:
    conn.rollback()
    raise
finally:
    conn.close()
