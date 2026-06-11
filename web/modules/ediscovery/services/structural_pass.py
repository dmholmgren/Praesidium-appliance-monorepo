#!/usr/bin/env python3
"""
modules/ediscovery/services/structural_pass.py  ---  structural extraction (step 4)

The 'structural' pass handler. For each section the router tags 'structural'
(not skipped), runs the deterministic citation + PII extractors over the
section's canonical slice and writes section-keyed rows:
  - citations -> document_citations
  - PII        -> document_entities (is_pii=true)

Patterns/validators are IMPORTED from jobs.extraction_template_engine (one source
of truth). This module re-implements only the matching *loop*: per-section,
global offsets (section.char_start + local), section_id-keyed rows. Dates are NOT
handled here (step 5).

Geometry-grounded: text is doc_geometry.canonical_text sliced by [char_start,
char_end), so every match offset is §0-exact -- never stored section.content.

Deploy (with section_router.py) into modules/ediscovery/services/, then:
  docker exec -i -w /app praesidium-web \
      python -m modules.ediscovery.services.structural_pass <doc_id> [<id>...] [commit]

Patent Pending --- Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import os
import sys
import uuid

import psycopg2
import psycopg2.extras

from jobs.extraction_template_engine import (
    CITATION_PATTERNS, _normalize_citation,
    PII_PATTERNS, _PII_NEG_CTX, _PII_LOOKBACK, _pii_normalize,
    _luhn_ok, _aba_ok, _norm_digits,
)
from modules.ediscovery.services.section_router import route_section

HJMM = "986c0fee-1390-43bb-ad28-8cd1db6de53f"


# ── pure finders (local offsets into the text passed) ──────────────────────────
def find_citations(text):
    out = []
    for regex, ctype, conf in CITATION_PATTERNS:
        for m in regex.finditer(text):
            raw = m.group(0).strip()
            norm = _normalize_citation(raw)
            if not norm or len(norm) > 300:
                continue
            out.append({"raw": raw[:500], "norm": norm[:500], "ctype": ctype,
                        "conf": conf, "start": m.start(), "end": m.end()})
    return out


def find_pii(text):
    out = []
    for regex, ptype, conf, validator, require_ctx, check_neg in PII_PATTERNS:
        for m in regex.finditer(text):
            raw = m.group(0).strip()
            if not raw:
                continue
            window = text[max(0, m.start() - _PII_LOOKBACK):m.start()]
            if check_neg and _PII_NEG_CTX.search(window):
                continue
            if require_ctx is not None and not require_ctx.search(window):
                continue
            if callable(validator):
                if not validator(raw, window):
                    continue
            elif validator == "luhn" and not _luhn_ok(_norm_digits(raw)):
                continue
            elif validator == "aba" and not _aba_ok(_norm_digits(raw)):
                continue
            norm = _pii_normalize(ptype, raw)
            out.append({"raw": raw[:500], "norm": norm, "ptype": ptype,
                        "conf": conf, "start": m.start(), "end": m.end()})
    return out


# ── DB ──────────────────────────────────────────────────────────────────────
def _get_db_conn():
    raw = os.environ.get("DATABASE_URL", "")
    url = raw.replace("postgresql+asyncpg://", "postgresql://")
    at = url.rfind("@")
    rest = url[at + 1:]
    userinfo = url[len("postgresql://"):at]
    colon = userinfo.rfind(":")
    user, password = userinfo[:colon], userinfo[colon + 1:]
    slash = rest.find("/")
    hostport, dbname = rest[:slash], rest[slash + 1:].split("?")[0]
    host, port = (hostport.rsplit(":", 1) if ":" in hostport else (hostport, "5432"))
    return psycopg2.connect(host=host, port=int(port), dbname=dbname,
                            user=user, password=password,
                            cursor_factory=psycopg2.extras.RealDictCursor)


# ── the pass ──────────────────────────────────────────────────────────────────
def run_structural_pass(conn, tenant, corpus, doc_id, run_id):
    doc_col = "dms_document_id" if corpus == "dms" else "ediscovery_document_id"
    cur = conn.cursor()

    cur.execute(
        "SELECT canonical_text FROM doc_geometry "
        "WHERE corpus=%s AND doc_id=%s AND rendition='native_pdf'",
        (corpus, doc_id),
    )
    g = cur.fetchone()
    if not g or not g["canonical_text"]:
        return {"error": "no geometry/canonical for doc", "citations": 0, "pii": 0}
    canonical = g["canonical_text"]

    cur.execute(
        f"""SELECT s.id AS section_id, s.section_type, s.char_start, s.char_end,
                   s.page_start, ld.parse_type
            FROM document_sections s
            JOIN logical_documents ld ON ld.id = s.logical_document_id
            WHERE s.{doc_col} = %s
              AND s.superseded_by_run_id IS NULL
              AND s.logical_document_id IS NOT NULL
            ORDER BY s.char_start, s.section_index""",
        (doc_id,),
    )
    sections = cur.fetchall()

    cite_hits, pii_hits = [], []
    sections_scanned = 0
    for s in sections:
        clen = (s["char_end"] or 0) - (s["char_start"] or 0)
        rt = route_section(s["section_type"], s["parse_type"], char_len=clen)
        if rt.skip or "structural" not in rt.passes:
            continue
        sections_scanned += 1
        cs, ce = s["char_start"], s["char_end"]
        sec_text = canonical[cs:ce]
        base_page = s["page_start"] or 1
        for c in find_citations(sec_text):
            pg = base_page + sec_text[:c["start"]].count("\f")
            cite_hits.append((s["section_id"], cs + c["start"], cs + c["end"], pg, c))
        for p in find_pii(sec_text):
            pg = base_page + sec_text[:p["start"]].count("\f")
            pii_hits.append((s["section_id"], cs + p["start"], cs + p["end"], pg, p))

    # per-document dedup (earliest occurrence wins)
    cite_hits.sort(key=lambda h: h[1])
    pii_hits.sort(key=lambda h: h[1])
    seen_c, cites = set(), []
    for sec_id, g0, g1, pg, c in cite_hits:
        key = c["norm"].upper()
        if key in seen_c:
            continue
        seen_c.add(key)
        cites.append((sec_id, g0, g1, pg, c))
    seen_p, piis = set(), []
    for sec_id, g0, g1, pg, p in pii_hits:
        key = (p["ptype"], (p["norm"] or p["raw"]).upper())
        if key in seen_p:
            continue
        seen_p.add(key)
        piis.append((sec_id, g0, g1, pg, p))

    # supersede prior rows for this doc (provenance preserved, not deleted)
    cur.execute(
        f"""UPDATE document_citations SET superseded_by_run_id=%s
            WHERE TRIM(tenant_id)=%s AND {doc_col}=%s
              AND superseded_by_run_id IS NULL AND extraction_run_id != %s""",
        (run_id, tenant, doc_id, run_id),
    )
    cur.execute(
        f"""UPDATE document_entities SET superseded_by_run_id=%s
            WHERE TRIM(tenant_id)=%s AND {doc_col}=%s AND is_pii
              AND superseded_by_run_id IS NULL AND extraction_run_id != %s""",
        (run_id, tenant, doc_id, run_id),
    )

    for sec_id, g0, g1, pg, c in cites:
        cur.execute(
            f"""INSERT INTO document_citations
                (id, tenant_id, {doc_col}, extraction_run_id, extraction_method,
                 citation_text, citation_type, normalized_citation,
                 section_id, page_number, char_start, char_end, confidence, extracted_at)
                VALUES (%s,%s,%s,%s,'structural',%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
            (str(uuid.uuid4()), tenant, doc_id, run_id,
             c["raw"], c["ctype"], c["norm"], sec_id, pg, g0, g1, c["conf"]),
        )
    for sec_id, g0, g1, pg, p in piis:
        cur.execute(
            f"""INSERT INTO document_entities
                (id, tenant_id, {doc_col}, extraction_run_id, extraction_method,
                 entity_text, entity_type, normalized_value,
                 section_id, page_number, char_start, char_end,
                 confidence, is_pii, pii_type, extracted_at)
                VALUES (%s,%s,%s,%s,'structural',%s,'pii',%s,%s,%s,%s,%s,%s,true,%s,NOW())""",
            (str(uuid.uuid4()), tenant, doc_id, run_id,
             p["raw"], p["norm"], sec_id, pg, g0, g1, p["conf"], p["ptype"]),
        )

    return {"sections_scanned": sections_scanned,
            "citations": len(cites), "pii": len(piis),
            "cite_by_type": _tally(cites, lambda h: h[4]["ctype"]),
            "pii_by_type": _tally(piis, lambda h: h[4]["ptype"])}


def _tally(rows, keyfn):
    out = {}
    for r in rows:
        k = keyfn(r)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# ── CLI (validate on a doc; dry-run unless 'commit') ──────────────────────────
def main():
    args = list(sys.argv[1:])
    commit = "commit" in args
    docs = [a for a in args if a != "commit"]
    if not docs:
        print("usage: structural_pass.py <dms_document_id> [<id> ...] [commit]")
        sys.exit(1)

    conn = _get_db_conn()
    conn.autocommit = False
    cur = conn.cursor()
    for doc_id in docs:
        run_id = str(uuid.uuid4())
        cur.execute(
            """INSERT INTO extraction_runs
               (id, tenant_id, run_type, source_type, status, started_at)
               VALUES (%s,%s,'structural_pass','dms','processing',NOW())""",
            (run_id, HJMM),
        )
        res = run_structural_pass(conn, HJMM, "dms", doc_id, run_id)
        cur.execute(
            "UPDATE extraction_runs SET status='completed', completed_at=NOW() WHERE id=%s",
            (run_id,),
        )
        print(f"\n=== {doc_id}  run={run_id} ===")
        print(f"  sections scanned : {res.get('sections_scanned')}")
        print(f"  citations        : {res.get('citations')}  {res.get('cite_by_type')}")
        print(f"  PII              : {res.get('pii')}  {res.get('pii_by_type')}")
        if res.get("error"):
            print(f"  ERROR: {res['error']}")

    if commit:
        conn.commit()
        print("\nCOMMITTED.")
    else:
        conn.rollback()
        print("\nDRY RUN -- rolled back. Add 'commit' to apply.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
