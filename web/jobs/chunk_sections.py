# -*- coding: utf-8 -*-
"""
chunk_sections.py — section-aware chunker (Tier-1 feeder, v1).

Walks the live layout sections for a file and emits SECTION-BOUNDED chunks (a
chunk never crosses a section boundary): one chunk per section's own-text span,
windowed with overlap when that own-text is large. Each chunk is the shared unit
for (a) embedding [all chunks] and (b) extraction [substantive chunks] — the
frontier-LLM allegation breaker for pleadings, the proposition parser for the
eDiscovery bulk.

Carries the context those consumers need:
  - chunk_metadata: section_type, section_label, immediate parent, nearest
    claim ancestor (cause_of_action / division), parse_type, doc_role, doc title,
    window i/n, substantive flag
  - embedded_content: "<doc title> > <claim> > <section>\\n\\n<text>" prefix so the
    embedding (and the breaker) carry hierarchy
Anchoring stays on the spine: char_start/char_end absolute, section_id + section_ids,
logical_document_id. primitive_id/primitive_type left NULL (set when a chunk is
bound to an extracted allegation/proposition downstream).

Usage:
  dry:    docker exec -i -w /app praesidium-web python - <doc_id> < this_file
  commit: docker exec -i -w /app praesidium-web python - <doc_id> commit < this_file
"""
import sys, bisect
sys.path.insert(0, "/app")
import psycopg2, psycopg2.extras
from collections import defaultdict
from modules.ediscovery.services.geometry_service import _conn_kwargs
from modules.ediscovery.services.geometry_io import load_geometry

TEN = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
TARGET_CHARS = 2000          # ~500 tokens; retrieval granularity
OVERLAP_CHARS = 200
CHUNKING_MODEL = "section_aware_v1"
SUBSTANTIVE = {"numbered_paragraph", "cause_of_action", "division", "subsection",
               "section", "recitals", "prayer", "article"}
CLAIM_TYPES = {"cause_of_action", "division", "article", "section", "recitals"}

DOC = sys.argv[1]
MODE = sys.argv[2] if len(sys.argv) > 2 else "dry"

conn = psycopg2.connect(**_conn_kwargs()); conn.autocommit = True
cur = conn.cursor()
cur.execute("SELECT file_path FROM dms_documents WHERE id=%s::uuid AND TRIM(tenant_id)=%s", (DOC, TEN))
row = cur.fetchone()
if not row:
    print("no such dms_document for tenant"); sys.exit(1)
path = row[0]

cur.execute("""SELECT id, parse_type, doc_role, title, char_start
               FROM logical_documents
               WHERE TRIM(tenant_id)=%s AND source_corpus='dms' AND source_file_id=%s::uuid
               ORDER BY char_start""", (TEN, DOC))
ld = {r[0]: dict(parse_type=r[1], doc_role=r[2], title=r[3]) for r in cur.fetchall()}
if not ld:
    print("no logical_documents — run write_logical_documents.py first"); sys.exit(2)

cur.execute("""SELECT id, logical_document_id, section_type, section_label,
                      char_start, char_end, page_start, page_end, parent_section_id
               FROM document_sections
               WHERE dms_document_id=%s::uuid AND superseded_by_run_id IS NULL
                 AND logical_document_id IS NOT NULL
               ORDER BY char_start""", (DOC,))
secs = [dict(id=r[0], lid=r[1], stype=r[2], label=r[3], cs=r[4], ce=r[5],
             pg0=r[6], pg1=r[7], parent=r[8]) for r in cur.fetchall()]
conn.close()
if not secs:
    print("no live layout sections — run section_logical_documents.py commit first"); sys.exit(3)

_g = load_geometry("dms", DOC)
if _g is None:
    print("no persisted geometry - run write_logical_documents.py (boundary) first"); sys.exit(5)
canonical, _tokens, _gmeta = _g
# spine: token char_start -> page, sorted, for page resolution of any span
toks = sorted(_tokens, key=lambda t: t.char_start)
cs_arr = [t.char_start for t in toks]
pg_arr = [t.page_number for t in toks]
def pages_for(a, b, fallback):
    lo = bisect.bisect_left(cs_arr, a); hi = bisect.bisect_left(cs_arr, b)
    seg = pg_arr[lo:hi]
    return (min(seg), max(seg)) if seg else fallback

by_id = {s["id"]: s for s in secs}
child_min = {}
for s in secs:
    if s["parent"] is not None:
        child_min[s["parent"]] = min(child_min.get(s["parent"], 1 << 60), s["cs"])

def claim_ancestor(s):
    cur_id = s["parent"]
    while cur_id is not None and cur_id in by_id:
        p = by_id[cur_id]
        if p["stype"] in CLAIM_TYPES:
            return p["label"]
        cur_id = p["parent"]
    return None

def own_end(s):
    return min(child_min.get(s["id"], s["ce"]), s["ce"])

def windows(a, b):
    if b - a <= TARGET_CHARS:
        return [(a, b)]
    out = []; start = a
    while start < b:
        end = min(start + TARGET_CHARS, b)
        out.append((start, end))
        if end >= b:
            break
        start = end - OVERLAP_CHARS
    return out

# build chunk plan
plan = []   # per chunk dict
gidx = 0
for s in secs:
    oe = own_end(s)
    if oe <= s["cs"]:
        continue  # heading with an immediate child and no own text; skip (child carries it)
    wins = windows(s["cs"], oe)
    claim = claim_ancestor(s)
    parent_label = by_id[s["parent"]]["label"] if (s["parent"] in by_id) else None
    title = ld.get(s["lid"], {}).get("title")
    ptype = ld.get(s["lid"], {}).get("parse_type")
    for wi, (a, b) in enumerate(wins):
        content = canonical[a:b].strip()
        if not content:
            continue
        ctx = " > ".join(x for x in [title, claim, s["label"]] if x)
        embedded = (ctx + "\n\n" + content) if ctx else content
        p0, p1 = pages_for(a, b, (s["pg0"], s["pg1"]))
        meta = {"section_type": s["stype"], "section_label": s["label"],
                "parent_label": parent_label, "claim": claim,
                "parse_type": ptype, "doc_role": ld.get(s["lid"], {}).get("doc_role"),
                "doc_title": title, "window": [wi + 1, len(wins)],
                "substantive": s["stype"] in SUBSTANTIVE}
        plan.append(dict(idx=gidx, lid=s["lid"], section_id=s["id"], cs=a, ce=b,
                         pg0=p0, pg1=p1, content=content, embedded=embedded,
                         tokens=max(1, len(content) // 4), meta=meta,
                         substantive=s["stype"] in SUBSTANTIVE, stype=s["stype"]))
        gidx += 1

# ---- report ----
print("file:", path.rsplit("/", 1)[-1], "| sections:", len(secs), "| chunks:", len(plan))
per = defaultdict(lambda: dict(n=0, sub=0, chars=0, win=0))
for c in plan:
    d = per[c["lid"]]; d["n"] += 1; d["sub"] += 1 if c["substantive"] else 0
    d["chars"] += len(c["content"]); d["win"] += 1 if c["meta"]["window"][1] > 1 else 0
for lid, d in sorted(per.items(), key=lambda kv: ld.get(kv[0], {}).get("title") or ""):
    info = ld.get(lid, {})
    print("  [%-10s %-8s] chunks=%3d  substantive=%3d  windowed=%2d  avg_chars=%4d  %s"
          % (info.get("doc_role"), info.get("parse_type"), d["n"], d["sub"], d["win"],
             d["chars"] // max(1, d["n"]), (info.get("title") or "(no title)")[:36]))
sizes = sorted(len(c["content"]) for c in plan)
print("chunk char sizes: min=%d  p50=%d  p95=%d  max=%d"
      % (sizes[0], sizes[len(sizes)//2], sizes[int(len(sizes)*0.95)], sizes[-1]))
print("total est tokens:", sum(c["tokens"] for c in plan))
# integrity: every chunk inside its section span
bad = sum(1 for c in plan if c["cs"] < by_id[c["section_id"]]["cs"] or c["ce"] > by_id[c["section_id"]]["ce"])
print("chunks crossing a section boundary:", bad, "(must be 0)")
# sample
print("\nsample chunks:")
for c in plan[:2] + [c for c in plan if c["meta"]["window"][1] > 1][:1]:
    print("  [%d] %s | claim=%s | win %s | [%d:%d] p%d-%d"
          % (c["idx"], c["meta"]["section_type"], (c["meta"]["claim"] or "-"),
             c["meta"]["window"], c["cs"], c["ce"], c["pg0"], c["pg1"]))
    print("      embedded_content head: %r" % (c["embedded"][:120]))

if MODE != "commit":
    print("\nDRY RUN — no writes. Re-run with 'commit' to persist.")
    sys.exit(0)
if bad:
    print("\nABORT: section-boundary violations; refusing to commit."); sys.exit(4)

# ---- atomic write ----
conn = psycopg2.connect(**_conn_kwargs()); conn.autocommit = False
try:
    with conn.cursor() as c:
        c.execute("""
            INSERT INTO extraction_runs
              (tenant_id, run_type, source_type, source_path, status,
               document_count, documents_processed, extraction_model, started_at, completed_at)
            VALUES (%s,'chunking','dms',%s,'completed',%s,%s,%s,now(),now())
            RETURNING id
        """, (TEN, path, len(ld), len(ld), CHUNKING_MODEL))
        run_id = c.fetchone()[0]
        # replace prior chunks for this file (chunks are derived; delete-replace)
        c.execute("""DELETE FROM document_chunk_embeddings
                     WHERE chunk_id IN (SELECT id FROM document_chunks WHERE dms_document_id=%s::uuid)""", (DOC,))
        c.execute("DELETE FROM document_chunks WHERE dms_document_id=%s::uuid", (DOC,))
        deleted = c.rowcount
        for ch in plan:
            c.execute("""
                INSERT INTO document_chunks
                  (tenant_id, dms_document_id, logical_document_id, section_id, section_ids,
                   derived_from_run_id, chunking_model, chunk_index, content, embedded_content,
                   token_count, char_start, char_end, page_start, page_end, chunk_metadata, chunked_at)
                VALUES (%s,%s::uuid,%s,%s,%s::uuid[],%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
            """, (TEN, DOC, ch["lid"], ch["section_id"], [ch["section_id"]], run_id,
                  CHUNKING_MODEL, ch["idx"], ch["content"], ch["embedded"], ch["tokens"],
                  ch["cs"], ch["ce"], ch["pg0"], ch["pg1"], psycopg2.extras.Json(ch["meta"])))
    conn.commit()
    print("\nCOMMITTED.")
    print("  chunking run:", run_id)
    print("  prior chunks deleted:", deleted, "| chunks inserted:", len(plan))
    with conn.cursor() as c:
        c.execute("""SELECT ld.doc_role, ld.parse_type, count(dc.*),
                            count(*) FILTER (WHERE (dc.chunk_metadata->>'substantive')::bool)
                     FROM logical_documents ld
                     LEFT JOIN document_chunks dc ON dc.logical_document_id = ld.id
                     WHERE TRIM(ld.tenant_id)=%s AND ld.source_corpus='dms' AND ld.source_file_id=%s::uuid
                     GROUP BY ld.id, ld.doc_role, ld.parse_type, ld.char_start
                     ORDER BY ld.char_start""", (TEN, DOC))
        print("\n  per logical document (chunks / substantive):")
        for r in c.fetchall():
            print("    %-10s %-8s  %3d chunks  (%3d substantive)" % (r[0], r[1], r[2], r[3]))
except Exception:
    conn.rollback()
    raise
finally:
    conn.close()
