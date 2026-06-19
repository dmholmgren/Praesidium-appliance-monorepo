"""appellate_framelock.py -- Record Fact-Element Classifier / U9.1: frame-lock.

On appeal the frame is FROZEN: the claim set is fixed and the judgment is known. This
job fixes the element spine for the appeal from that frozen frame:

  1. CLAIMS  -- detect the causes of action asserted in the operative pleadings
     (Petition / Counterclaim / Petition in Intervention), by cosine of each
     cause_of_action_library template (ModernBERT-768) against the pleading's CR
     paragraph fact units (U9.0), plus a lexical name signal. Seed `causes_of_action`
     + instantiate `coa_elements` from the library's elements_json (embedded for T1).
  2. JUDGMENT -- parse the signed Final Judgment: per claim, adjudicated
     {granted|denied|alternative_not_reached|undetermined}, relief, prevailing party,
     against whom -> causes_of_action.attributes. Deterministic where the judgment is
     explicit; attorney can override (coa_elements.attorney_override_status).

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.appellate_framelock --appeal UUID [--tenant T] [--debug]
  python -m modules.depositions.jobs.appellate_framelock --list --appeal UUID
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import uuid

logger = logging.getLogger(__name__)

CLAIM_COS_FLOOR = 0.50          # library<->pleading max cosine to count a COA asserted
                                # (a lexically-named COA is asserted even below the floor)
TOP_K_PER_PLEADING = 6

# library code -> lexical surface words (a strong literal hit asserts the COA even
# if the embedding is middling; appellate pleadings name their causes of action).
_COA_WORDS = {
    "breach_of_guaranty": ["guaranty", "guarantee", "guarantor"],
    "breach_of_contract": ["breach of contract", "breached the contract", "material breach"],
    "promissory_estoppel": ["promissory estoppel", "estoppel"],
    "quantum_meruit": ["quantum meruit"],
    "suit_on_sworn_account": ["sworn account"],
    "money_had_and_received": ["money had and received"],
    "unjust_enrichment": ["unjust enrichment"],
    "fraud_by_nondisclosure": ["nondisclosure"],
    "common_law_fraud": ["fraud"],
    "fraudulent_inducement": ["fraudulent inducement"],
    "negligent_misrepresentation": ["negligent misrepresentation"],
    "declaratory_judgment": ["declaratory judgment", "declaration"],
    "conversion": ["conversion"],
    "breach_of_fiduciary_duty": ["fiduciary"],
    "tortious_interference_existing_contract": ["tortious interference"],
}

_AMOUNT = re.compile(r"\$\s?[\d,]+(?:\.\d{2})?")
_GRANT_PL = re.compile(r"\b(?:entitled to recover|recover on its|prevailed on|have and recover|"
                       r"is awarded|granted judgment)\b", re.I)
_DENY_DEF = re.compile(r"\bfailed to meet (?:its|their) burden\b|\btake nothing\b|\bdenied\b|"
                       r"\bdismiss(?:ed|al)\b|\bdeny all relief\b", re.I)
_PREVAIL = re.compile(r"ORDERED that\s+([A-Z][\w&.,'\- ]+?)\s+have and recover", re.I)


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _claim_pleadings(cur, appellate_case_id, tenant):
    """Operative claim-bearing pleadings (Petition / Counterclaim / Intervention),
    excluding ANSWERs. Returns [{filing_id, title, cr_type, cite, role, para_ids, text}]."""
    cur.execute(
        "SELECT id::text, section_label, attributes->>'cr_type', attributes->>'cite', "
        "       logical_document_id::text FROM document_sections "
        "WHERE section_type='cr_filing' AND attributes->>'appellate_case_id'=%s "
        "  AND TRIM(tenant_id)=%s AND attributes->>'cr_type' IN "
        "      ('petition','counterclaim','intervention') "
        "  AND upper(section_label) NOT LIKE '%%ANSWER%%' "
        "ORDER BY section_index", (str(appellate_case_id), tenant))
    out = []
    for fid, title, ctype, cite, _ld in cur.fetchall():
        cur.execute("SELECT id::text, content FROM document_sections "
                    "WHERE parent_section_id=CAST(%s AS uuid) AND section_type='cr_para' "
                    "ORDER BY section_index", (fid,))
        paras = cur.fetchall()
        role = "plaintiff" if "PETITION" in title.upper() and "INTERVENTION" not in title.upper() \
            and "COUNTERCLAIM" not in title.upper() else "defendant_intervenor"
        out.append({"filing_id": fid, "title": title, "cr_type": ctype, "cite": cite,
                    "role": role, "para_ids": [p[0] for p in paras],
                    "text": "\n".join(p[1] for p in paras)})
    return out


def _detect_claims(cur, pleading):
    """Per library COA: max cosine vs this pleading's paragraph fact units + lexical hit.
    Returns asserted [{code, library_id, display_name, elements, sali_iri, sim, lexical}]."""
    if not pleading["para_ids"]:
        return []
    cur.execute(
        "SELECT lib.id::text, lib.code, lib.display_name, lib.elements_json, lib.sali_iri, "
        "       max(1 - (ds.embedding <=> lib.embedding)) AS sim "
        "FROM cause_of_action_library lib, document_sections ds "
        "WHERE lib.is_active AND lib.embedding IS NOT NULL "
        "  AND ds.id = ANY(CAST(%s AS uuid[])) AND ds.embedding IS NOT NULL "
        "GROUP BY lib.id, lib.code, lib.display_name, lib.elements_json, lib.sali_iri "
        "ORDER BY sim DESC", (pleading["para_ids"],))
    tl = pleading["text"].lower()
    rows = cur.fetchall()
    asserted = []
    for (lid, code, dn, ej, sali, sim) in rows:
        lexical = any(w in tl for w in _COA_WORDS.get(code, []))
        if sim >= CLAIM_COS_FLOOR or lexical:
            elements = ej if isinstance(ej, list) else json.loads(ej)
            asserted.append({"code": code, "library_id": lid, "display_name": dn,
                             "elements": elements, "sali_iri": sali,
                             "sim": round(float(sim), 4), "lexical": lexical})
    # keep top-K by sim but always keep lexical hits
    asserted.sort(key=lambda a: (a["lexical"], a["sim"]), reverse=True)
    return asserted[:TOP_K_PER_PLEADING]


def _judgment_text(cur, appellate_case_id, tenant):
    """The operative (signed, latest) Final Judgment text + cite. Prefer a judgment whose
    title lacks 'PROPOSED'; else any judgment filing."""
    cur.execute(
        "SELECT f.section_label, f.attributes->>'cite', f.id::text, "
        "       (upper(f.section_label) LIKE '%%PROPOSED%%') is_proposed "
        "FROM document_sections f WHERE f.section_type='cr_filing' "
        "  AND f.attributes->>'appellate_case_id'=%s AND TRIM(f.tenant_id)=%s "
        "  AND f.attributes->>'cr_type'='judgment' "
        "ORDER BY is_proposed ASC, f.extracted_at DESC", (str(appellate_case_id), tenant))
    rows = cur.fetchall()
    if not rows:
        return None, None
    label, cite, fid, _prop = rows[0]
    cur.execute("SELECT string_agg(content, ' ' ORDER BY section_index) FROM document_sections "
                "WHERE parent_section_id=CAST(%s AS uuid) AND section_type='cr_para'", (fid,))
    body = cur.fetchone()[0] or ""
    return body, (cite or "CR")


def _adjudicate(coa, role, jtext):
    """Deterministic disposition for one claim from the judgment -> attributes dict."""
    if not jtext:
        return {"adjudicated": "undetermined"}
    words = _COA_WORDS.get(coa["code"], [coa["display_name"].split("(")[0].strip().lower()])
    # window of judgment text near any mention of this COA's surface words
    near = ""
    low = jtext.lower()
    for w in words:
        i = low.find(w.lower())
        if i >= 0:
            near += " " + jtext[max(0, i - 160):i + 220]
    mention = bool(near.strip())
    pm = _PREVAIL.search(jtext)
    prevailing = pm.group(1).strip().rstrip(".,") if pm else None
    amounts = _AMOUNT.findall(near) or _AMOUNT.findall(jtext)

    if role == "plaintiff":
        if mention and _GRANT_PL.search(near):
            return {"adjudicated": "granted", "relief": amounts[:3] or None,
                    "prevailing_party": prevailing, "basis": "judgment recites recovery on this claim"}
        if not mention:
            return {"adjudicated": "alternative_not_reached",
                    "basis": "claim not named in the judgment (pleaded in the alternative)"}
        return {"adjudicated": "undetermined", "prevailing_party": prevailing}
    else:
        if _DENY_DEF.search(jtext):
            return {"adjudicated": "denied", "prevailing_party": prevailing,
                    "basis": "judgment: party failed to meet burden / take-nothing"}
        return {"adjudicated": "undetermined", "prevailing_party": prevailing}


def frame_lock(tenant_id, appellate_case_id, debug=False) -> dict:
    from modules.depositions.jobs.embed_qa import _embed, _vec_literal, EMBED_URL_DEFAULT
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

        pleadings = _claim_pleadings(cur, appellate_case_id, tenant)
        if not pleadings:
            return {"error": "no operative pleadings parsed (run U9.0 cr_parser first)"}

        jtext, jcite = _judgment_text(cur, appellate_case_id, tenant)

        # detect + assemble claims (dedupe a COA to the strongest pleading)
        seeded = {}     # code -> record
        debug_scores = {}
        for pl in pleadings:
            claims = _detect_claims(cur, pl)
            debug_scores[pl["title"][:40]] = [(c["code"], c["sim"], c["lexical"]) for c in claims]
            for c in claims:
                prev = seeded.get(c["code"])
                if prev and prev["sim"] >= c["sim"]:
                    continue
                rec = dict(c)
                rec.update({"role": pl["role"], "pleading_cite": pl["cite"],
                            "pleading_title": pl["title"]})
                seeded[c["code"]] = rec

        if debug:
            return {"appellate_case_id": str(appellate_case_id), "pleadings": len(pleadings),
                    "claim_scores": debug_scores,
                    "judgment_found": bool(jtext), "judgment_cite": jcite}

        # idempotent: clear prior framelock spine for this matter
        cur.execute("DELETE FROM coa_elements WHERE cause_of_action_id IN "
                    "(SELECT id FROM causes_of_action WHERE matter_id=CAST(%s AS uuid) "
                    "  AND attributes->>'source'='cr_framelock')", (matter_id,))
        cur.execute("DELETE FROM causes_of_action WHERE matter_id=CAST(%s AS uuid) "
                    "AND attributes->>'source'='cr_framelock'", (matter_id,))

        run_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO extraction_runs (id, tenant_id, run_type, source_type, "
            " extraction_model, status, started_at) VALUES (CAST(%s AS uuid), %s, 'framelock', "
            " 'record', 'framelock_v1', 'running', NOW())", (run_id, tenant))

        results = []
        cnt = 0
        for code, rec in sorted(seeded.items(), key=lambda kv: -kv[1]["sim"]):
            cnt += 1
            cid = str(uuid.uuid4())
            adj = _adjudicate(rec, rec["role"], jtext)
            attrs = {"source": "cr_framelock", "appellate_case_id": str(appellate_case_id),
                     "library_code": code, "role": rec["role"],
                     "pleading_cite": rec["pleading_cite"], "pleading": rec["pleading_title"],
                     "claim_similarity": rec["sim"], "lexical_hit": rec["lexical"],
                     "judgment_cite": jcite, **adj}
            cur.execute(
                "INSERT INTO causes_of_action (id, tenant_id, matter_id, cause_library_id, "
                "  title, count_number, status, confidence, attribution, extraction_run_id, "
                "  attributes) VALUES (CAST(%s AS uuid), %s, CAST(%s AS uuid), CAST(%s AS uuid), "
                "  %s, %s, 'seeded', %s, 'framelock', CAST(%s AS uuid), CAST(%s AS jsonb))",
                (cid, tenant, matter_id, rec["library_id"], rec["display_name"], cnt,
                 rec["sim"], run_id, json.dumps(attrs)))

            # elements from the library template, embedded for T1 classify
            el_texts = [e.get("text", "") for e in rec["elements"]]
            vecs = []
            if el_texts:
                vecs, _m, _r = _embed(EMBED_URL_DEFAULT, el_texts)
            for i, e in enumerate(rec["elements"]):
                eid = str(uuid.uuid4())
                e_attrs = {"n": e.get("n"), "sali_iri": rec["sali_iri"],
                           "source": "cr_framelock"}
                cur.execute(
                    "INSERT INTO coa_elements (id, tenant_id, cause_of_action_id, element_name, "
                    "  status, attribution, extraction_run_id, attributes, embedding) "
                    "VALUES (CAST(%s AS uuid), %s, CAST(%s AS uuid), %s, 'seeded', 'framelock', "
                    "  CAST(%s AS uuid), CAST(%s AS jsonb), %s)",
                    (eid, tenant, cid, (e.get("text") or "")[:500], run_id,
                     json.dumps(e_attrs), _vec_literal(vecs[i]) if vecs else None))
            results.append({"claim": rec["display_name"], "code": code, "role": rec["role"],
                            "elements": len(rec["elements"]), "similarity": rec["sim"],
                            "lexical": rec["lexical"], "adjudicated": adj.get("adjudicated"),
                            "relief": adj.get("relief"), "prevailing": adj.get("prevailing_party")})

        cur.execute("UPDATE extraction_runs SET status='completed' WHERE id=CAST(%s AS uuid)",
                    (run_id,))
        conn.commit()
        return {"appellate_case_id": str(appellate_case_id), "matter_id": matter_id,
                "pleadings_scanned": len(pleadings), "claims_seeded": len(results),
                "judgment_cite": jcite, "claims": results}
    finally:
        conn.close()


def list_spine(tenant_id, appellate_case_id) -> list:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT matter_id::text FROM appellate_cases WHERE id=CAST(%s AS uuid)",
                    (str(appellate_case_id),))
        r = cur.fetchone()
        if not r:
            return []
        cur.execute(
            "SELECT c.title, c.attributes->>'role', c.attributes->>'adjudicated', "
            "       c.attributes->>'relief', c.attributes->>'prevailing_party', "
            "       (SELECT count(*) FROM coa_elements e WHERE e.cause_of_action_id=c.id) "
            "FROM causes_of_action c WHERE c.matter_id=CAST(%s AS uuid) "
            "  AND c.attributes->>'source'='cr_framelock' ORDER BY c.count_number", (r[0],))
        cols = ["claim", "role", "adjudicated", "relief", "prevailing_party", "elements"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Appellate frame-lock (U9.1)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--appeal", required=True)
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        out = list_spine(args.tenant, args.appeal)
    else:
        out = frame_lock(args.tenant, args.appeal, debug=args.debug)
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
