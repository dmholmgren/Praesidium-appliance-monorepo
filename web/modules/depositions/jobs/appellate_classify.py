"""appellate_classify.py -- Record Fact-Element Classifier / U9.2: cascade classify.

Classify every fact unit of the CLOSED record against the frame-locked element spine
(U9.1), cheapest tier that resolves it, writing record_fact_element_links:

  T0 deterministic -- the fact unit literally carries the element's distinctive terms
                      (high precision, small yield).
  T1 embedding     -- top coa_element by ModernBERT-768 cosine (pgvector), assigned
                      when the top similarity clears the floor; low-margin / low-score
                      links are flagged needs_escalation for the T2 frontier lane (U9.3).

Fact units = CR paragraph sections (U9.0 cr_para) + RR Q&A units (Module A). Both are
already embedded in the SAME 768-space as the elements, so the whole pass is cosine +
rules -- the wide CPU lane. Idempotent per appeal (classify_run_id supersedes prior).

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.appellate_classify --appeal UUID [--tenant T]
        [--assign-at 0.42] [--debug]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import uuid

logger = logging.getLogger(__name__)

ASSIGN_AT = 0.42        # top-1 element cosine to assign a fact unit
ESCALATE_HI = 0.50      # assigned but below this -> flag for T2 frontier
MARGIN_MIN = 0.03       # top1-top2 below this -> ambiguous -> flag for T2
T0_MIN_HITS = 2         # distinctive element terms present -> deterministic tier 0

_STOP = set("the a an of to in on for and or with from that this its their his her by "
            "is are was were be been being as at occurrence existence terms failure "
            "refusal promise reliance damages plaintiff defendant party which whose "
            "upon based held made was not non".split())


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _keywords(element_name):
    """Distinctive content words of an element statement (for the T0 literal layer)."""
    words = re.findall(r"[A-Za-z][A-Za-z'\-]{4,}", (element_name or "").lower())
    return {w for w in words if w not in _STOP}


def _percentiles(xs):
    if not xs:
        return {}
    s = sorted(xs)
    def pct(p):
        return round(s[min(len(s) - 1, int(p * len(s)))], 3)
    return {"min": round(s[0], 3), "p25": pct(.25), "p50": pct(.5),
            "p75": pct(.75), "p90": pct(.9), "max": round(s[-1], 3)}


def _top2_rows(cur, sql, params):
    """Run a top-2-per-fact-unit candidate query -> {fid: {top1,top2,meta}}."""
    cur.execute(sql, params)
    by = {}
    for row in cur.fetchall():
        fid = row[0]
        by.setdefault(fid, {}).setdefault("rows", []).append(row)
    out = {}
    for fid, d in by.items():
        rows = sorted(d["rows"], key=lambda r: r[-1])      # by rk
        out[fid] = {"top1": rows[0], "top2": rows[1] if len(rows) > 1 else None}
    return out


_CR_SQL = """
WITH cand AS (
  SELECT ds.id AS fid, ds.attributes->>'cite' AS cite, ds.char_start, ds.char_end,
         ds.content AS txt, e.id AS eid, e.cause_of_action_id AS caid,
         e.element_name AS ename, e.attributes->>'sali_iri' AS sali,
         1 - (ds.embedding <=> e.embedding) AS sim,
         row_number() OVER (PARTITION BY ds.id ORDER BY ds.embedding <=> e.embedding) AS rk
  FROM document_sections ds CROSS JOIN coa_elements e
  WHERE ds.section_type='cr_para' AND ds.logical_document_id = ANY(CAST(%s AS uuid[]))
    AND ds.embedding IS NOT NULL
    AND e.cause_of_action_id = ANY(CAST(%s AS uuid[])) AND e.embedding IS NOT NULL)
SELECT fid, cite, char_start, char_end, txt, eid, caid, ename, sali, sim, rk
FROM cand WHERE rk <= 2
"""

_RR_SQL = """
WITH cand AS (
  SELECT q.id AS fid, q.q_start_page AS pg, q.q_start_line AS ln, q.char_start, q.char_end,
         coalesce(q.question_text,'')||' '||coalesce(q.answer_text,'') AS txt,
         e.id AS eid, e.cause_of_action_id AS caid, e.element_name AS ename,
         e.attributes->>'sali_iri' AS sali,
         1 - (em.embedding <=> e.embedding) AS sim,
         row_number() OVER (PARTITION BY q.id ORDER BY em.embedding <=> e.embedding) AS rk
  FROM transcript_qa_units q
  JOIN transcript_qa_embeddings em ON em.qa_unit_id=q.id AND em.chunk_number=0
  CROSS JOIN coa_elements e
  WHERE q.transcript_id = ANY(CAST(%s AS uuid[])) AND coalesce(q.is_colloquy,false)=false
    AND e.cause_of_action_id = ANY(CAST(%s AS uuid[])) AND e.embedding IS NOT NULL)
SELECT fid, pg, ln, char_start, char_end, txt, eid, caid, ename, sali, sim, rk
FROM cand WHERE rk <= 2
"""


def classify(tenant_id, appellate_case_id, assign_at=ASSIGN_AT, debug=False) -> dict:
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT matter_id::text, trial_id::text FROM appellate_cases "
                    "WHERE id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s",
                    (str(appellate_case_id), tenant))
        meta = cur.fetchone()
        if not meta:
            return {"error": "appellate case not found"}
        matter_id, trial_id = meta

        cur.execute("SELECT id FROM causes_of_action WHERE matter_id=CAST(%s AS uuid) "
                    "AND attributes->>'source'='cr_framelock'", (matter_id,))
        cause_ids = [r[0] for r in cur.fetchall()]
        if not cause_ids:
            return {"error": "no frame-locked spine -- run U9.1 first"}

        cur.execute("SELECT document_id::text FROM record_documents "
                    "WHERE appellate_case_id=CAST(%s AS uuid) AND record_kind IN ('CR','SUPP_CR')",
                    (str(appellate_case_id),))
        cr_doc_ids = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT id::text FROM deposition_transcripts WHERE trial_id=CAST(%s AS uuid)",
                    (trial_id,))
        rr_tx_ids = [r[0] for r in cur.fetchall()]

        cr = _top2_rows(cur, _CR_SQL, (cr_doc_ids, cause_ids)) if cr_doc_ids else {}
        rr = _top2_rows(cur, _RR_SQL, (rr_tx_ids, cause_ids)) if rr_tx_ids else {}

        # column indices: ...,eid(-6),caid(-5),ename(-4),sali(-3),sim(-2),rk(-1)
        links = []
        sims = []
        for kind, units in (("cr_section", cr), ("rr_qa", rr)):
            for fid, d in units.items():
                t1 = d["top1"]
                sim = float(t1[-2])
                sims.append(sim)
                if sim < assign_at:
                    continue
                margin = sim - float(d["top2"][-2]) if d["top2"] else sim
                eid, caid, ename, sali = t1[-6], t1[-5], t1[-4], t1[-3]
                if kind == "cr_section":          # fid,cite,cs,ce,txt,...
                    cite, cs, ce, txt = t1[1], t1[2], t1[3], t1[4]
                else:                              # fid,pg,ln,cs,ce,txt,...
                    cite, cs, ce, txt = "1 RR %s:%s" % (t1[1], t1[2]), t1[3], t1[4], t1[5]
                # T0 literal layer: element's distinctive terms present in the fact text
                kw = _keywords(ename)
                hits = sum(1 for w in kw if w in (txt or "").lower())
                tier = 0 if hits >= T0_MIN_HITS else 1
                conf = max(sim, 0.85) if tier == 0 else sim
                needs = tier == 1 and (sim < ESCALATE_HI or margin < MARGIN_MIN)
                links.append({"kind": kind, "fid": fid, "eid": eid, "caid": caid,
                              "sali": sali, "cite": cite, "cs": cs, "ce": ce,
                              "sim": round(sim, 4), "margin": round(margin, 4),
                              "tier": tier, "conf": round(conf, 4), "needs": needs,
                              "snippet": re.sub(r"\s+", " ", (txt or "")[:160])})

        if debug:
            from collections import Counter
            return {"appellate_case_id": str(appellate_case_id),
                    "fact_units": {"cr": len(cr), "rr": len(rr)},
                    "sim_distribution": _percentiles(sims),
                    "assigned_at_%.2f" % assign_at: len(links),
                    "by_tier": dict(Counter(l["tier"] for l in links)),
                    "needs_escalation": sum(1 for l in links if l["needs"]),
                    "sample": sorted(links, key=lambda l: -l["sim"])[:8]}

        # persist (idempotent: supersede prior links for this appeal)
        run_id = str(uuid.uuid4())
        cur.execute("DELETE FROM record_fact_element_links "
                    "WHERE appellate_case_id=CAST(%s AS uuid)", (str(appellate_case_id),))
        from psycopg2.extras import execute_values
        rows = [(tenant, matter_id, str(appellate_case_id), l["kind"], l["fid"], l["eid"],
                 l["caid"], "supports", l["conf"], l["margin"], l["tier"], l["needs"],
                 l["sali"], l["cite"], l["cs"], l["ce"], "proposed", run_id)
                for l in links]
        if rows:
            execute_values(cur,
                "INSERT INTO record_fact_element_links (tenant_id, matter_id, appellate_case_id, "
                "  fact_kind, fact_ref_id, coa_element_id, cause_of_action_id, relation, "
                "  confidence, margin, tier, needs_escalation, sali_iri, record_cite, char_start, "
                "  char_end, status, classify_run_id) VALUES %s",
                rows,
                template="(%s,CAST(%s AS uuid),CAST(%s AS uuid),%s,CAST(%s AS uuid),"
                         "CAST(%s AS uuid),CAST(%s AS uuid),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                         "CAST(%s AS uuid))",
                page_size=500)
        conn.commit()

        from collections import Counter
        return {"appellate_case_id": str(appellate_case_id), "classify_run_id": run_id,
                "fact_units": {"cr": len(cr), "rr": len(rr)},
                "links": len(links), "by_tier": dict(Counter(l["tier"] for l in links)),
                "needs_escalation": sum(1 for l in links if l["needs"]),
                "by_kind": dict(Counter(l["kind"] for l in links))}
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Record fact-element classify T0/T1 (U9.2)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--appeal", required=True)
    ap.add_argument("--assign-at", type=float, default=ASSIGN_AT)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    out = classify(args.tenant, args.appeal, assign_at=args.assign_at, debug=args.debug)
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
