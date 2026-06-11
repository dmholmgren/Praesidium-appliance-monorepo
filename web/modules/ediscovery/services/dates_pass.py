#!/usr/bin/env python3
"""
modules/ediscovery/services/dates_pass.py  ---  date extraction (step 5)

The 'dates' pass handler. For each section the router tags 'dates' (not skipped),
finds dates over the section's canonical slice and routes each by the section's
date_default with a deterministic operating-residue flip:
  - fact     -> document_propositions (event_date + event_date_raw, offsets)
  - operating -> document_deadlines    (deadline_date + deadline_text)

Routing (per Praesidium_Extraction_Architecture_v1.0.md sec.3):
  * date_default='operating' (scheduling/docket orders): every date -> operating.
  * date_default='fact' (the common case): a date is fact UNLESS its immediate
    context carries deadline language (no later than / due / hearing / cutoff...)
    and not narrative language (filed on / dated / certificate of service...),
    in which case it flips to operating. This is the only refinement; no model.
  * "N days before/after X" (CALC) is inherently procedural -> operating
    (is_calculated=true) regardless of section default.

Patterns/parser are IMPORTED from jobs.extraction_template_engine (one source of
truth). The deadline/narrative keyword regexes there live *inside* a function and
can't be imported, so compact mirrors are defined here -- converging them is a
follow-up. Geometry-grounded: text is doc_geometry.canonical_text sliced by the
section span, so fact-date offsets are §0-exact.

Deploy into modules/ediscovery/services/ (alongside section_router.py), then:
  docker exec -i -w /app praesidium-web \
      python -m modules.ediscovery.services.dates_pass <doc_id> [<id>...] [commit]

Patent Pending --- Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import os
import re
import sys
import uuid

import psycopg2
import psycopg2.extras

from jobs.extraction_template_engine import (
    DATE_PATTERNS, _parse_date_str, DEADLINE_TYPE_KEYWORDS, CALC_PATTERN,
)
from modules.ediscovery.services.section_router import route_section

HJMM = "986c0fee-1390-43bb-ad28-8cd1db6de53f"

# Compact mirrors of the engine's (function-local, unimportable) deadline /
# narrative keyword gates. Converge with the engine in a later cleanup.
_DEADLINE_KW = re.compile(
    r"(?i)(deadline|cut[\-\s]?off|due\s+(?:date|by|on|before)|no\s+later\s+than|"
    r"on\s+or\s+before|shall\s+(?:be\s+)?(?:filed|served|completed|exchanged|designated)|"
    r"must\s+(?:be\s+)?(?:filed|completed)|hearing|trial\b|conference|mediation|"
    r"arbitration|discovery\s+cut|deposition|expert\s+(?:design|report)|designation|"
    r"rebuttal|pretrial|pre[\-\s]?trial|docket\s+call|jury\s+fee|"
    r"response\s+(?:due|deadline)|objection\s+(?:due|deadline)|comply\s+by|"
    r"compliance\s+date|expires?\s+on|terminat\w*\s+(?:date|on))"
)
_NARRATIVE_KW = re.compile(
    r"(?i)(filed\s+(?:on|this)|was\s+filed|dated\b|signed\s+this|entered\s+(?:on|this)|"
    r"e[\-]?filed|certificate\s+of\s+service|hereby\s+certif|notary|"
    r"commission\s+expires|reporter|csr\s+no)"
)


# ── pure finder (local offsets) ─────────────────────────────────────────────
def find_dates(text):
    """Every date-shaped match, deduped by position (a single physical date can
    match multiple patterns). Fact-date capture is intentionally ungated -- every
    date in a factual section is a candidate event date."""
    out, seen = [], set()
    for pat, _ in DATE_PATTERNS:
        for m in pat.finditer(text):
            bucket = m.start() // 5
            if bucket in seen:
                continue
            seen.add(bucket)
            raw = (m.group(1) if m.lastindex else m.group(0)).strip()
            out.append({"raw": raw[:120], "iso": _parse_date_str(raw),
                        "start": m.start(), "end": m.end()})
    return out


def _bucket(date_default, close_ctx):
    if date_default == "operating":
        return "operating"
    if _DEADLINE_KW.search(close_ctx) and not _NARRATIVE_KW.search(close_ctx):
        return "operating"
    return "fact"


def _deadline_type(close_ctx):
    cl = close_ctx.lower()
    for kw, dt in DEADLINE_TYPE_KEYWORDS.items():
        if kw in cl:
            return dt
    return None


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
def run_dates_pass(conn, tenant, corpus, doc_id, run_id):
    doc_col = "dms_document_id" if corpus == "dms" else "ediscovery_document_id"
    cur = conn.cursor()

    cur.execute(
        "SELECT canonical_text FROM doc_geometry "
        "WHERE corpus=%s AND doc_id=%s AND rendition='native_pdf'",
        (corpus, doc_id),
    )
    g = cur.fetchone()
    if not g or not g["canonical_text"]:
        return {"error": "no geometry/canonical", "fact": 0, "operating": 0}
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

    fact_rows, op_rows = [], []   # propositions, deadlines
    scanned = 0
    for s in sections:
        clen = (s["char_end"] or 0) - (s["char_start"] or 0)
        rt = route_section(s["section_type"], s["parse_type"], char_len=clen)
        if rt.skip or "dates" not in rt.passes:
            continue
        scanned += 1
        cs, ce = s["char_start"], s["char_end"]
        sec_text = canonical[cs:ce]
        base_page = s["page_start"] or 1

        for d in find_dates(sec_text):
            lo = d["start"]
            close = sec_text[max(0, lo - 60):min(len(sec_text), d["end"] + 60)]
            ctx = re.sub(r"\s+", " ",
                         sec_text[max(0, lo - 100):min(len(sec_text), d["end"] + 100)]).strip()
            pg = base_page + sec_text[:lo].count("\f")
            g0, g1 = cs + lo, cs + d["end"]
            conf = 0.85 if d["iso"] else 0.60
            if _bucket(rt.date_default, close) == "fact":
                fact_rows.append((s["section_id"], g0, g1, pg, d, ctx[:1000], conf))
            else:
                op_rows.append((s["section_id"], ctx[:500], d["iso"],
                                _deadline_type(close), False, None, conf))

        # calculated deadlines ("30 days before trial") -> always operating
        for m in CALC_PATTERN.finditer(sec_text):
            basis = m.group(0).strip()[:500]
            ctx = re.sub(r"\s+", " ", sec_text[max(0, m.start() - 40):m.end() + 10]).strip()
            op_rows.append((s["section_id"], ctx[:500], None,
                            _deadline_type(basis), True, basis, 0.70))

    # supersede prior rows from THIS pass (our extraction_method), idempotent
    cur.execute(
        f"""UPDATE document_propositions SET superseded_by_run_id=%s
            WHERE TRIM(tenant_id)=%s AND {doc_col}=%s AND extraction_method='structural'
              AND superseded_by_run_id IS NULL AND extraction_run_id != %s""",
        (run_id, tenant, doc_id, run_id),
    )
    cur.execute(
        f"""UPDATE document_deadlines SET superseded_by_run_id=%s
            WHERE TRIM(tenant_id)=%s AND {doc_col}=%s AND extraction_method='structural'
              AND superseded_by_run_id IS NULL AND extraction_run_id != %s""",
        (run_id, tenant, doc_id, run_id),
    )

    parsed = 0
    for sec_id, g0, g1, pg, d, ctx, conf in fact_rows:
        if d["iso"]:
            parsed += 1
        cur.execute(
            f"""INSERT INTO document_propositions
                (id, tenant_id, {doc_col}, extraction_run_id, extraction_method,
                 proposition_type, proposition_text, section_id, page_number,
                 char_start, char_end, confidence, event_date, event_date_raw, extracted_at)
                VALUES (%s,%s,%s,%s,'structural','date',%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
            (str(uuid.uuid4()), tenant, doc_id, run_id, ctx, sec_id, pg,
             g0, g1, conf, d["iso"], d["raw"]),
        )
    for sec_id, text_ctx, iso, dtype, is_calc, basis, conf in op_rows:
        cur.execute(
            f"""INSERT INTO document_deadlines
                (id, tenant_id, {doc_col}, extraction_run_id, extraction_method,
                 deadline_text, deadline_date, is_calculated, calculation_basis,
                 section_id, confidence, extracted_at)
                VALUES (%s,%s,%s,%s,'structural',%s,%s,%s,%s,%s,%s,NOW())""",
            (str(uuid.uuid4()), tenant, doc_id, run_id, text_ctx, iso,
             is_calc, basis, sec_id, conf),
        )

    return {"sections_scanned": scanned,
            "fact": len(fact_rows), "fact_parsed": parsed,
            "operating": len(op_rows)}


# ── CLI ──────────────────────────────────────────────────────────────────────
def main():
    args = list(sys.argv[1:])
    commit = "commit" in args
    docs = [a for a in args if a != "commit"]
    if not docs:
        print("usage: dates_pass.py <dms_document_id> [<id> ...] [commit]")
        sys.exit(1)

    conn = _get_db_conn()
    conn.autocommit = False
    cur = conn.cursor()
    for doc_id in docs:
        run_id = str(uuid.uuid4())
        cur.execute(
            """INSERT INTO extraction_runs
               (id, tenant_id, run_type, source_type, status, started_at)
               VALUES (%s,%s,'dates_pass','dms','processing',NOW())""",
            (run_id, HJMM),
        )
        res = run_dates_pass(conn, HJMM, "dms", doc_id, run_id)
        cur.execute(
            "UPDATE extraction_runs SET status='completed', completed_at=NOW() WHERE id=%s",
            (run_id,),
        )
        print(f"\n=== {doc_id}  run={run_id} ===")
        print(f"  sections scanned : {res.get('sections_scanned')}")
        print(f"  fact dates       : {res.get('fact')}  (parsed to ISO: {res.get('fact_parsed')})")
        print(f"  operating dates  : {res.get('operating')}")
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
