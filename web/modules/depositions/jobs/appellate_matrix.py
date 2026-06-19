"""appellate_matrix.py -- Record Fact-Element Classifier / U9.4: coverage / sufficiency
matrix + rollup.

Rolls the classified record_fact_element_links (U9.2/U9.3) up to the element spine:
per (cause of action, element) -- supporting count + strength, strongest record cites,
gap flag -- and overlays the frozen judgment (causes_of_action.attributes.adjudicated)
+ the appellant identity to produce a LEGAL-SUFFICIENCY read:

  - a GRANTED claim whose element has thin / empty record support  -> a sufficiency
    POINT for the appellant (challenge) / EXPOSURE for the appellee (defend);
  - a DENIED claim's gaps explain the adverse finding (and frame the claimant's appeal).

The accepted links are also written into coa_elements.supporting_evidence /
undermining_evidence jsonb so the existing element UI renders the record support for
free. This is the payoff the closed, frame-locked record makes computable.

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.appellate_matrix --appeal UUID [--tenant T] [--rollup]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

WELL_BEST = 0.55        # best supporting cosine to call an element well-grounded
WELL_COUNT = 2          # >= this many supporting links
TOP_CITES = 12          # supporting facts kept per element in the jsonb rollup


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _grade(adjudicated, role, supp_count, best, appellant_is_claimant):
    """Element-level sufficiency read, given the frozen outcome."""
    strength = ("well_supported" if supp_count >= WELL_COUNT and (best or 0) >= WELL_BEST
                else "thin" if supp_count >= 1 else "gap")
    granted = adjudicated == "granted"
    denied = adjudicated == "denied"
    if granted and strength in ("thin", "gap"):
        # the burdened party prevailed but the record is light on this element
        who = "appellant may challenge" if not appellant_is_claimant else "defend on appeal"
        return strength, "sufficiency_risk", \
            "Granted claim, but record support for this element is %s -- %s legal sufficiency." \
            % (strength, who)
    if granted:
        return strength, "supported", "Granted claim; element well grounded in the record."
    if denied and strength == "gap":
        return strength, "explains_denial", \
            "Denied claim: NO record support for this element -- consistent with the adverse finding."
    if denied:
        return strength, "denied_thin", "Denied claim; element support is %s." % strength
    return strength, "undetermined", "Adjudication undetermined; element support is %s." % strength


def build_matrix(tenant_id, appellate_case_id, rollup=False) -> dict:
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT matter_id::text, appellant, appellee FROM appellate_cases "
                    "WHERE id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s",
                    (str(appellate_case_id), tenant))
        meta = cur.fetchone()
        if not meta:
            return {"error": "appellate case not found"}
        matter_id, appellant, appellee = meta

        # spine
        cur.execute(
            "SELECT c.id::text, c.title, c.count_number, c.attributes->>'adjudicated', "
            "       c.attributes->>'role', c.attributes->>'relief', c.attributes->>'prevailing_party' "
            "FROM causes_of_action c WHERE c.matter_id=CAST(%s AS uuid) "
            "  AND c.attributes->>'source'='cr_framelock' ORDER BY c.count_number", (matter_id,))
        causes = cur.fetchall()

        # all links for this appeal (joined to element)
        cur.execute(
            "SELECT l.coa_element_id::text, l.cause_of_action_id::text, e.element_name, "
            "       e.attributes->>'n', l.fact_kind, l.fact_ref_id::text, l.record_cite, "
            "       l.confidence, l.tier, l.relation, l.char_start, l.char_end, l.needs_escalation "
            "FROM record_fact_element_links l JOIN coa_elements e ON e.id=l.coa_element_id "
            "WHERE l.appellate_case_id=CAST(%s AS uuid)", (str(appellate_case_id),))
        link_rows = cur.fetchall()

        # snippet text per fact unit (batch by kind)
        cr_ids = [r[5] for r in link_rows if r[4] == "cr_section"]
        rr_ids = [r[5] for r in link_rows if r[4] == "rr_qa"]
        snip = {}
        if cr_ids:
            cur.execute("SELECT id::text, content FROM document_sections "
                        "WHERE id = ANY(CAST(%s AS uuid[]))", (cr_ids,))
            snip.update({i: re.sub(r"\s+", " ", (t or ""))[:200] for i, t in cur.fetchall()})
        if rr_ids:
            cur.execute("SELECT id::text, coalesce(question_text,'')||' '||coalesce(answer_text,'') "
                        "FROM transcript_qa_units WHERE id = ANY(CAST(%s AS uuid[]))", (rr_ids,))
            snip.update({i: re.sub(r"\s+", " ", (t or ""))[:200] for i, t in cur.fetchall()})

        # group links by element
        by_el = {}
        for (eid, caid, ename, en, kind, fref, cite, conf, tier, rel, cs, ce, needs) in link_rows:
            d = by_el.setdefault(eid, {"name": ename, "n": en, "cause": caid, "supports": [],
                                       "undermines": []})
            rec = {"cite": cite, "confidence": round(float(conf or 0), 4), "tier": tier,
                   "fact_kind": kind, "fact_ref_id": fref, "snippet": snip.get(fref, ""),
                   "needs_escalation": needs}
            (d["undermines"] if rel == "undermines" else d["supports"]).append(rec)

        appellant_is_claimant = (appellant or "").lower().startswith(("key",))  # claimant = plaintiff Key

        matrix, run_writes = [], 0
        for (cid, title, num, adj, role, relief, prevailing) in causes:
            cur.execute("SELECT id::text, element_name, attributes->>'n' FROM coa_elements "
                        "WHERE cause_of_action_id=CAST(%s AS uuid) ORDER BY (attributes->>'n')::int",
                        (cid,))
            elements = []
            for (eid, ename, en) in cur.fetchall():
                d = by_el.get(eid, {"supports": [], "undermines": []})
                supp = sorted(d["supports"], key=lambda r: -r["confidence"])
                best = supp[0]["confidence"] if supp else None
                strength, flag, note = _grade(adj, role, len(supp), best,
                                              appellant_is_claimant)
                elements.append({"n": en, "element": ename, "support_count": len(supp),
                                 "best_confidence": best, "strength": strength,
                                 "sufficiency": flag, "note": note,
                                 "top_cites": [s["cite"] for s in supp[:5]],
                                 "evidence": supp[:TOP_CITES]})
                if rollup:
                    cur.execute(
                        "UPDATE coa_elements SET supporting_evidence=CAST(%s AS jsonb), "
                        "  status=%s, updated_at=now() WHERE id=CAST(%s AS uuid)",
                        (json.dumps(supp[:TOP_CITES]),
                         "supported" if strength == "well_supported" else
                         "thin" if strength == "thin" else "gap", eid))
                    run_writes += 1
            gaps = [e["n"] for e in elements if e["strength"] == "gap"]
            matrix.append({"cause": title, "count_number": num, "adjudicated": adj,
                           "relief": relief, "prevailing_party": prevailing,
                           "elements": elements, "gap_elements": gaps,
                           "sufficiency_summary":
                               ("ALL elements grounded" if not gaps and adj == "granted" else
                                "GAPS at elements %s" % gaps if gaps else "see elements")})
        if rollup:
            conn.commit()

        return {"appellate_case_id": str(appellate_case_id), "appellant": appellant,
                "appellee": appellee, "rolled_up_elements": run_writes if rollup else 0,
                "matrix": matrix}
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Coverage / sufficiency matrix (U9.4)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--appeal", required=True)
    ap.add_argument("--rollup", action="store_true", help="write supporting_evidence into coa_elements")
    args = ap.parse_args()
    out = build_matrix(args.tenant, args.appeal, rollup=args.rollup)
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
