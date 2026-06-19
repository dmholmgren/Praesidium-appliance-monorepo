"""appellate_draft.py -- Record Fact-Element Classifier / U9.5: auto Statement of Facts
+ argument scaffold (record-cited).

The deliverable end of the classifier. From the accepted record_fact_element_links
(U9.2/U9.3), assemble two drafts whose every sentence is born with its record cite:

  STATEMENT OF FACTS  -- supporting facts grouped by cause -> element, each rendered
                         with its (CR p) / ([vol] RR p:l) cite; the manual record-cited
                         narrative, pre-assembled. Attorney edits; cites stay anchored.
  ARGUMENT SCAFFOLD   -- per issue (cause): adjudication + standard of review, then each
                         element -> its record support -> the matrix sufficiency read,
                         so the appellant sees which elements are vulnerable (gaps) vs
                         well grounded. element -> record support -> [authorities].

On --write the drafts populate the workspace brief_sections (statement_of_facts /
argument) so they render in the U6 brief workspace immediately.

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.appellate_draft --appeal UUID [--tenant T] [--write]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# standard of review by the appellant's posture against an adjudicated claim.
_SOR = {
    "granted": "Legal sufficiency (no-evidence): the reviewing court credits evidence "
               "favorable to the finding if a reasonable factfinder could, and disregards "
               "contrary evidence unless a reasonable factfinder could not. A no-evidence "
               "point is sustained only if the record shows no more than a scintilla.",
    "denied": "Legal & factual sufficiency: a party attacking an adverse finding on an "
              "issue on which it bore the burden must show the evidence establishes the "
              "matter as a matter of law (legal) or that the finding is against the great "
              "weight and preponderance of the evidence (factual).",
}
_MAX_FACTS_PER_EL = 6


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _gather(cur, appellate_case_id, matter_id):
    cur.execute(
        "SELECT c.id::text, c.title, c.count_number, c.attributes->>'adjudicated', "
        "       c.attributes->>'relief' FROM causes_of_action c "
        "WHERE c.matter_id=CAST(%s AS uuid) AND c.attributes->>'source'='cr_framelock' "
        "ORDER BY c.count_number", (matter_id,))
    causes = cur.fetchall()

    cur.execute(
        "SELECT l.cause_of_action_id::text, l.coa_element_id::text, e.element_name, "
        "       e.attributes->>'n', l.fact_kind, l.fact_ref_id::text, l.record_cite, "
        "       l.confidence, l.relation, l.tier FROM record_fact_element_links l "
        "JOIN coa_elements e ON e.id=l.coa_element_id "
        "WHERE l.appellate_case_id=CAST(%s AS uuid) AND l.status<>'rejected'",
        (str(appellate_case_id),))
    links = cur.fetchall()

    cr_ids = [r[5] for r in links if r[4] == "cr_section"]
    rr_ids = [r[5] for r in links if r[4] == "rr_qa"]
    snip = {}
    if cr_ids:
        cur.execute("SELECT id::text, content FROM document_sections "
                    "WHERE id = ANY(CAST(%s AS uuid[]))", (cr_ids,))
        snip.update({i: re.sub(r"\s+", " ", (t or "")).strip()[:280] for i, t in cur.fetchall()})
    if rr_ids:
        cur.execute("SELECT id::text, coalesce(question_text,'')||' '||coalesce(answer_text,'') "
                    "FROM transcript_qa_units WHERE id = ANY(CAST(%s AS uuid[]))", (rr_ids,))
        snip.update({i: re.sub(r"\s+", " ", (t or "")).strip()[:280] for i, t in cur.fetchall()})
    return causes, links, snip


def _is_boilerplate(text):
    t = (text or "").strip()
    if len(t) < 30:
        return True
    # caption / letterhead / filing furniture
    return bool(re.search(r"IN THE \d+|DISTRICT COURT|RECEIVED By|FIRM WIRING|"
                          r"CAUSE NO|§+|CLIENT TRUST", t, re.I)) and len(t) < 120


def build_drafts(tenant_id, appellate_case_id, write=False) -> dict:
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT matter_id::text, appellant, appellee, style FROM appellate_cases "
                    "WHERE id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s",
                    (str(appellate_case_id), tenant))
        meta = cur.fetchone()
        if not meta:
            return {"error": "appellate case not found"}
        matter_id, appellant, appellee, style = meta

        causes, links, snip = _gather(cur, appellate_case_id, matter_id)
        if not causes:
            return {"error": "no frame-locked spine -- run U9.1/U9.2 first"}

        # index links: cause -> element_n -> [facts]
        by = {}
        meta_el = {}
        for (caid, eid, ename, en, kind, fref, cite, conf, rel, tier) in links:
            txt = snip.get(fref, "")
            if rel != "supports" or _is_boilerplate(txt):
                continue
            by.setdefault(caid, {}).setdefault(en, []).append(
                {"cite": cite, "conf": float(conf or 0), "text": txt, "tier": tier})
            meta_el[(caid, en)] = ename

        sof, arg = [], []
        sof.append("STATEMENT OF FACTS")
        sof.append("")
        arg.append("ARGUMENT")
        arg.append("")
        n_facts = 0
        for (caid, title, num, adj, relief) in causes:
            els = by.get(caid, {})
            # ---- Statement of Facts (only causes with record facts) ----
            if els:
                sof.append("%s." % title)
                for en in sorted(els, key=lambda x: int(x) if x and x.isdigit() else 99):
                    facts = sorted(els[en], key=lambda f: -f["conf"])[:_MAX_FACTS_PER_EL]
                    if not facts:
                        continue
                    sof.append("  %s:" % meta_el.get((caid, en), "Element %s" % en))
                    for f in facts:
                        sof.append("    %s (%s)." % (f["text"].rstrip(". "), f["cite"]))
                        n_facts += 1
                sof.append("")
            # ---- Argument scaffold (every cause) ----
            arg.append("Issue: Whether the evidence is legally sufficient as to %s [%s%s]."
                       % (title, adj or "undetermined",
                          ", relief " + relief if relief and relief != "null" else ""))
            arg.append("  Standard of Review: %s" % _SOR.get(adj, _SOR["granted"]))
            cur.execute("SELECT attributes->>'n', element_name FROM coa_elements "
                        "WHERE cause_of_action_id=CAST(%s AS uuid) "
                        "ORDER BY (attributes->>'n')::int", (caid,))
            for en, ename in cur.fetchall():
                facts = sorted(els.get(en, []), key=lambda f: -f["conf"])
                cites = ", ".join(dict.fromkeys(f["cite"] for f in facts[:5]))
                if facts:
                    arg.append("  Element %s (%s): record support -> %s. [authorities: TBD]"
                               % (en, ename, cites))
                else:
                    arg.append("  Element %s (%s): NO RECORD SUPPORT FOUND -- legal-sufficiency "
                               "vulnerability; argue no-evidence / develop or concede. [authorities: TBD]"
                               % (en, ename))
            arg.append("")

        sof_text = "\n".join(sof)
        arg_text = "\n".join(arg)

        written = []
        if write:
            for key, body in (("statement_of_facts", sof_text), ("argument", arg_text)):
                cur.execute(
                    "UPDATE brief_sections SET body=%s, word_count=%s, updated_at=now() "
                    "WHERE appellate_case_id=CAST(%s AS uuid) AND section_key=%s",
                    (body, len(re.findall(r"\S+", body)), str(appellate_case_id), key))
                if cur.rowcount:
                    written.append(key)
            conn.commit()

        return {"appellate_case_id": str(appellate_case_id), "appellant": appellant,
                "facts_rendered": n_facts, "causes": len(causes),
                "written_sections": written,
                "statement_of_facts": sof_text, "argument_scaffold": arg_text}
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Auto Statement of Facts + argument scaffold (U9.5)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--appeal", required=True)
    ap.add_argument("--write", action="store_true", help="write into workspace brief_sections")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    out = build_drafts(args.tenant, args.appeal, write=args.write)
    if args.show and "statement_of_facts" in out:
        print(out["statement_of_facts"]); print(); print(out["argument_scaffold"])
        out = {k: v for k, v in out.items() if k not in ("statement_of_facts", "argument_scaffold")}
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
