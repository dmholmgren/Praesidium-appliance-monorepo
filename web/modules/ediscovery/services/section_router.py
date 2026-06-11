#!/usr/bin/env python3
"""
section_router.py  ---  pure section-type router (step 3, v2: size guard)

Decides, per document_sections row, WHICH extraction passes apply and the default
date bucket. No model, no I/O in the core function -- a pure mapping of
(section_type, parse_type, char_len) -> SectionRoute. See
Praesidium_Extraction_Architecture_v1.0.md sec.1 & sec.3.

v2 SIZE GUARD: never skip a section large enough to be substance. An unstructured
motion/brief whose entire body is captured as one oversized 'title' (or other
nominally-boilerplate) section must not be discarded -- size betrays that it is
the document body, not a caption. Any would-skip section over MAX_SKIP_CHARS is
re-routed to structural+dates. (Found via the Marcus motion: its 10,811-char
argument sat in a single 'title' section and was being dropped, losing 17 case
cites + 2 rule cites.)

Division of labor:
  - The ROUTER sets the doc-level default (skip vs structural/dates/allegations,
    and a fact/operating date *default* by doc role).
  - The dates HANDLER (step 5) refines residue per individual date.

Conservative defaults: an UNKNOWN section_type routes to 'substantive'
(structural+dates) -- never skipped, never an allegation anchor. An UNKNOWN
parse_type is treated as non-pleading (fact dates, no frontier).

CLI (audit a doc's sections; zero cost):
  docker exec -i -w /app praesidium-web python -m \
      modules.ediscovery.services.section_router \
      d19cd919-4a6e-4d47-9219-9341f5c21e90

Patent Pending --- Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

import psycopg2
import psycopg2.extras

HJMM = "986c0fee-1390-43bb-ad28-8cd1db6de53f"

# ── section_type buckets ────────────────────────────────────────────────────
CAPTION_TYPES = {"title"}
BOILERPLATE_TYPES = {"signature_block", "certificate", "verification",
                     "jury_demand", "prayer"}
ALLEGATION_TYPES = {"cause_of_action"}
CONTRACT_CLAUSE_TYPES = {"section"}
SUBSTANTIVE_TYPES = {"numbered_paragraph", "subsection", "division"}

# ── parse_type families ─────────────────────────────────────────────────────
ALLEGATION_PARSE_TYPES = {"pleading", "discovery_response"}
OPERATING_PARSE_TYPES = {"scheduling_order", "docket_control_order",
                         "case_management_order"}

# Any would-skip section larger than this is treated as body, not boilerplate.
MAX_SKIP_CHARS = 800


@dataclass(frozen=True)
class SectionRoute:
    section_class: str
    passes: tuple = field(default_factory=tuple)
    date_default: str = None
    skip: bool = False
    note: str = ""


def route_section(section_type, parse_type, doc_role=None, char_len=None) -> SectionRoute:
    st = (section_type or "").strip().lower()
    pt = (parse_type or "").strip().lower()
    date_default = "operating" if pt in OPERATING_PARSE_TYPES else "fact"

    if st in CAPTION_TYPES:
        r = SectionRoute("caption", (), None, True, "caption -> clerk pass")
    elif st in BOILERPLATE_TYPES:
        r = SectionRoute("boilerplate", (), None, True, f"{st} (boilerplate)")
    elif st in ALLEGATION_TYPES:
        passes = ["structural", "dates"]
        if pt in ALLEGATION_PARSE_TYPES:
            passes.append("allegations")
            note = "cause_of_action -> frontier eligible"
        else:
            note = f"cause_of_action (no frontier: parse_type={pt!r})"
        r = SectionRoute("allegation_anchor", tuple(passes), date_default, False, note)
    elif st in CONTRACT_CLAUSE_TYPES:
        r = SectionRoute("contract_clause", ("structural", "dates"), "fact", False,
                         "contract clause")
    else:
        note = "substantive" if st in SUBSTANTIVE_TYPES \
            else f"substantive (default for unknown type {st!r})"
        r = SectionRoute("substantive", ("structural", "dates"), date_default, False, note)

    # size guard: a large would-skip section is a mislabeled body -> keep it
    if r.skip and char_len is not None and char_len > MAX_SKIP_CHARS:
        return SectionRoute("substantive", ("structural", "dates"), date_default, False,
                            f"{st} oversized ({char_len}c) -> treated as body")
    return r


# ── CLI audit (reads sections, prints routes; no model, no writes) ──────────
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


def audit(doc_ids):
    conn = _get_db_conn()
    cur = conn.cursor()
    for doc_id in doc_ids:
        cur.execute(
            """
            SELECT s.section_index, s.section_type, s.section_label,
                   s.char_start, s.char_end, ld.parse_type, ld.doc_role, ld.char_start AS ld_start
            FROM document_sections s
            JOIN logical_documents ld ON ld.id = s.logical_document_id
            WHERE s.dms_document_id = %s
              AND s.superseded_by_run_id IS NULL
              AND s.logical_document_id IS NOT NULL
            ORDER BY ld.char_start, s.section_index
            """,
            (doc_id,),
        )
        rows = cur.fetchall()
        print(f"\n=== {doc_id}  ({len(rows)} sections) ===")
        tally, pass_tally, date_tally, guarded = {}, {"structural": 0, "dates": 0, "allegations": 0}, {"fact": 0, "operating": 0}, 0
        for r in rows:
            clen = (r["char_end"] or 0) - (r["char_start"] or 0)
            rt = route_section(r["section_type"], r["parse_type"], r["doc_role"], clen)
            tally[rt.section_class] = tally.get(rt.section_class, 0) + 1
            for p in rt.passes:
                pass_tally[p] += 1
            if rt.date_default in date_tally:
                date_tally[rt.date_default] += 1
            if "oversized" in rt.note:
                guarded += 1
            flag = "SKIP" if rt.skip else ",".join(rt.passes)
            label = (r["section_label"] or "")[:30]
            mark = " *GUARD*" if "oversized" in rt.note else ""
            print(f"  [{r['parse_type']:<9}] {r['section_type']:<19} {flag:<26} "
                  f"{clen:>6}c date={rt.date_default or '-':<9} {label}{mark}")
        print("  --")
        print(f"  classes: {dict(sorted(tally.items()))}")
        print(f"  pass load: {pass_tally}   date default: {date_tally}   size-guarded: {guarded}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: section_router.py <dms_document_id> [<dms_document_id> ...]")
        sys.exit(1)
    audit(sys.argv[1:])
