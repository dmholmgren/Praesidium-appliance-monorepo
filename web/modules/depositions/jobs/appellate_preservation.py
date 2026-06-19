"""appellate_preservation.py -- Module B / Unit 8: preservation check.

For each Issue Presented, classify the complaint (admission / exclusion of evidence,
legal- or factual-sufficiency, jury charge, other legal ruling), semantically match
it against the trial_preservation index (U3) in the ModernBERT-768 space the record
is already embedded in, and apply Tex. R. App. P. 33.1 (+ Tex. R. Evid. 103) to grade
preservation. Surfaces an unpreserved issue -- the single most expensive appellate
mistake -- before filing, with the matched RR objection/ruling loci.

Determinism: the bulk work is the cheap local embed; the rule logic is deterministic.
The attorney confirms; party attribution (who objected) is Tier-1 best-effort.

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.appellate_preservation --appeal UUID [--issues-from section|brief]
        [--show]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# Cross-register match (legal issue phrasing vs testimony context) sits lower than
# testimony<->testimony; calibrated on the Key RR. Tunable.
MATCH_FLOOR = 0.30
TOP_K = 3

# Complaint-type classifiers, in priority order (first hit wins).
_CTYPE = [
    ("factual_sufficiency",
     re.compile(r"\bfactual(?:ly)?\s+(?:in)?sufficien|\bgreat\s+weight\b|\bagainst\s+the\s+weight\b", re.I)),
    ("legal_sufficiency",
     re.compile(r"\blegal(?:ly)?\s+(?:in)?sufficien|\bno\s+evidence\b|\bas\s+a\s+matter\s+of\s+law\b"
                r"|\bscintilla\b", re.I)),
    ("charge",
     re.compile(r"\bjury\s+(?:charge|instruction|question|finding)\b|\bcharge\s+(?:error|to\s+the\s+jury|"
                r"conference)\b|\b(?:court'?s|its|the)\s+charge\b|\bbroad-?form\b|\bRule\s+27[34]\b"
                r"|\brefus(?:ed|al)\s+to\s+submit\b|\bsubmit(?:ted|ting)?\s+(?:an?\s+)?(?:instruction|question)\b", re.I)),
    ("exclusion",
     re.compile(r"\b(?:err\w*|abus\w*)\b[^.]{0,60}\b(?:exclud\w+|sustain\w+|refus\w+|disallow\w+|strik\w+|"
                r"exclusion)\b|\bexclud(?:ed|ing)\b|\brefus(?:ed|ing)\s+to\s+admit\b|\bexcluding\s+"
                r"(?:appellant|the)\b|\bstruck\b|\bexclusion\s+of\b", re.I)),
    ("admission",
     re.compile(r"\b(?:err\w*|abus\w*)\b[^.]{0,60}\b(?:admit\w+|overrul\w+|allow\w+|permit\w+|receiv\w+|"
                r"admission)\b|\badmit(?:ted|ting)\b|\bover\s+(?:appellant'?s?\s+)?objection\b|"
                r"\badmission\s+of\b|\boverrul\w+\b", re.I)),
    ("legal_ruling",
     re.compile(r"\b(?:err\w*|abus\w*)\b[^.]{0,40}\b(?:grant\w+|den\w+|rul\w+|order\w*)\b|"
                r"\bmotion\s+(?:for|to|in)\b|\babus(?:e|ed)\s+(?:its|his|her)\s+discretion\b", re.I)),
]


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _classify(issue_text):
    for code, rx in _CTYPE:
        if rx.search(issue_text or ""):
            return code
    return "other"


# Surface words for each grounds code -- when the issue names the ground, anchor
# the match to the entry carrying that ground (lexical re-rank over the semantics).
_GROUND_WORDS = {
    "hearsay": ["hearsay"], "foundation": ["foundation", "predicate"],
    "speculation": ["speculat"], "leading": ["leading", "lead the"],
    "relevance": ["relevan", "irrelevan", "immaterial"],
    "nonresponsive": ["nonresponsive", "non-responsive", "responsive"],
    "argumentative": ["argumentative"], "asked_and_answered": ["asked and answered"],
    "best_evidence": ["best evidence"], "privilege": ["privileg"],
    "cumulative": ["cumulative"], "prejudice": ["prejudic", "403"],
    "beyond_scope": ["scope"], "form": ["form of the question"], "narrative": ["narrative"],
    "compound": ["compound"], "vague": ["vague", "ambiguous"],
    "assumes_facts": ["assumes facts"], "legal_conclusion": ["legal conclusion"],
    "misstates": ["misstat", "mischaracteriz", "misquot"],
    "lacks_knowledge": ["personal knowledge"], "authentication": ["authenticat"],
}
_GROUND_BONUS = 0.12


def _ground_bonus(issue_lower, grounds):
    """+bonus per issue when it names a ground the entry was objected on."""
    if not grounds:
        return 0.0
    for code in grounds.split(","):
        for w in _GROUND_WORDS.get(code.strip(), []):
            if w in issue_lower:
                return _GROUND_BONUS
    return 0.0


def _split_issues(body):
    """Parse an Issues Presented section body into discrete issues.
    Handles numbered ('1.' 'Issue 1:'), lettered, and one-per-line layouts."""
    body = (body or "").strip()
    if not body:
        return []
    # numbered/lettered markers at line start
    parts = re.split(r"(?m)^\s*(?:Issue\s+)?(?:\d+|[A-Z])\s*[.):]\s+", body)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        return parts
    # else: split on blank lines, then on single newlines if needed
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if len(paras) >= 2:
        return paras
    lines = [l.strip() for l in body.split("\n") if l.strip() and len(l.strip()) > 25]
    return lines or [body]


def _load_lines(cur, transcript_id):
    cur.execute("SELECT page, line, text FROM transcript_lines "
                "WHERE transcript_id=CAST(%s AS uuid) ORDER BY page, line", (str(transcript_id),))
    rows = cur.fetchall()
    idx = {(p, l): i for i, (p, l, _t) in enumerate(rows)}
    return rows, idx


def _context(rows, idx, page, line, back=12, fwd=2):
    """The subject-matter window around an objection (the Q/A being objected to)."""
    i = idx.get((page, line))
    if i is None:
        return ""
    lo, hi = max(0, i - back), min(len(rows), i + fwd + 1)
    return " ".join((rows[j][2] or "").strip() for j in range(lo, hi))


def _decide(ctype, matches):
    """Apply TRAP 33.1 / TRE 103 to the ranked matches -> (status, needs_oop, rationale)."""
    def first(ruling):
        return next((m for m in matches if m["ruling"] == ruling), None)

    if ctype in ("legal_sufficiency", "factual_sufficiency"):
        kind = "Legal" if ctype == "legal_sufficiency" else "Factual"
        return ("no_objection_required", False,
                "%s-sufficiency complaints are preserved without a trial objection. In a "
                "civil bench trial no motion is required to challenge legal or factual "
                "sufficiency on appeal (Tex. R. App. P. 33.1(d)); confirm the bench-trial "
                "posture." % kind)
    if ctype == "charge":
        if not matches:
            return ("unpreserved", False,
                    "Charge complaints require a timely, specific objection to the charge "
                    "before it is read (Tex. R. Civ. P. 274 / TRAP 33.1). No charge objection "
                    "found in the record. (N.B. a bench trial has no jury charge.)")
        m = matches[0]
        return ("unclear", False,
                "A related ruling appears at %s (%s); charge error requires a Rule 274 "
                "objection -- verify on the charge record." % (m["ruling_locus"] or m["locus"], m["ruling"]))
    # anchor on the BEST semantic match; only reach past it to a same-polarity
    # entry that is essentially tied (within TIE of the top similarity).
    TIE = 0.05
    best = matches[0] if matches else None

    def tied(ruling):
        if not best:
            return None
        return next((m for m in matches if m["ruling"] == ruling
                     and m["similarity"] >= best["similarity"] - TIE), None)

    if ctype == "admission":
        # preserved if the evidence came in OVER an objection (overruled)
        if best and best["ruling"] == "overruled":
            ov = best
        else:
            ov = tied("overruled")
        if ov:
            return ("preserved", False,
                    "Preserved: Appellant's objection (%s) was OVERRULED at %s, so the "
                    "evidence came in over objection (Tex. R. App. P. 33.1). Grounds: %s."
                    % (ov["locus"], ov["ruling_locus"] or ov["locus"], ov["grounds"] or "n/a"))
        if best and best["ruling"] == "sustained":
            return ("unclear", False,
                    "The closest objection (%s, grounds: %s) was SUSTAINED -- the objecting "
                    "party prevailed below, so there is no adverse admission ruling to appeal. "
                    "Verify which party complains; you cannot appeal a ruling in your favor "
                    "(Tex. R. App. P. 33.1)." % (best["locus"], best["grounds"] or "n/a"))
        return ("unpreserved", False,
                "No trial objection in the record matches this admission complaint; a "
                "timely, specific objection and adverse ruling are required to preserve "
                "(Tex. R. App. P. 33.1(a)).")
    if ctype == "exclusion":
        # preserved if the evidence was kept OUT (sustained) + an offer of proof
        if best and best["ruling"] == "sustained":
            su = best
        else:
            su = tied("sustained")
        if su and su.get("oop"):
            return ("preserved", False,
                    "Preserved: the objection excluding the evidence was SUSTAINED at %s and "
                    "the record contains an offer of proof / bill of exception (Tex. R. Evid. "
                    "103(a)(2))." % (su["ruling_locus"] or su["locus"]))
        if su:
            return ("unpreserved", True,
                    "NOT preserved: the objection was SUSTAINED at %s, but the record shows NO "
                    "offer of proof. Excluded-evidence error is preserved only by an offer of "
                    "proof / bill of exception (Tex. R. Evid. 103(a)(2); TRAP 33.2). Make the "
                    "offer of proof before relying on this issue." % (su["ruling_locus"] or su["locus"]))
        if best and best["ruling"] == "overruled":
            return ("unclear", False,
                    "The closest objection (%s) was OVERRULED -- the evidence was admitted, not "
                    "excluded. Verify the exclusion complaint against the record." % best["locus"])
        return ("unpreserved", True,
                "No sustained objection or offer of proof found for this exclusion complaint "
                "(Tex. R. Evid. 103 / TRAP 33.1).")
    # legal_ruling / other
    ruled = next((m for m in matches if m["ruling"] and m["ruling"] not in ("withdrawn",)), None)
    if ruled:
        return ("preserved", False,
                "A trial-court ruling appears at %s (%s); confirm this is the complained-of "
                "ruling and that the request/objection was specific (TRAP 33.1)."
                % (ruled["ruling_locus"] or ruled["locus"], ruled["ruling"]))
    if matches:
        return ("unclear", False,
                "Related objection at %s but no clear ruling; review the record for a ruling "
                "or refusal to rule (TRAP 33.1(a)(2))." % matches[0]["locus"])
    return ("unpreserved", False,
            "No matching trial-court objection or ruling found in the record (TRAP 33.1).")


def _cosine(a, b):
    return float(sum(x * y for x, y in zip(a, b)))


def _get_issues(cur, tenant, appellate_case_id, source):
    """Issues from the workspace section model (default) or the attached brief."""
    if source == "brief":
        from modules.depositions.jobs.appellate_brief import _brief_canonical, _find_brief
        b = _find_brief(cur, appellate_case_id, None)
        if not b:
            return []
        canon = _brief_canonical(tenant, b[0], b[1])
        m = re.search(r"ISSUES?\s+PRESENTED\b", canon[2000:], re.I)  # skip the TOC entry
        if not m:
            return []
        seg = canon[2000 + m.end(): 2000 + m.end() + 2500]
        seg = re.split(r"\n\s*(?:INTRODUCTION|STATEMENT|SUMMARY|ARGUMENT)\b", seg, 1, re.I)[0]
        return _split_issues(seg)
    cur.execute("SELECT body FROM brief_sections WHERE appellate_case_id=CAST(%s AS uuid) "
                "AND section_key='issues_presented'", (str(appellate_case_id),))
    r = cur.fetchone()
    return _split_issues(r[0] if r else "")


def check_preservation(tenant_id, appellate_case_id, issues_source="section") -> dict:
    from modules.depositions.jobs.embed_qa import _embed, EMBED_URL_DEFAULT
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

        issues = _get_issues(cur, tenant, appellate_case_id, issues_source)
        if not issues:
            return {"error": "no Issues Presented found (section model empty / brief has none)"}

        # load the preservation index for this trial + per-entry subject-matter context
        cur.execute(
            "SELECT id::text, transcript_id::text, objection_page, objection_line, "
            "       objection_locus, grounds, objection_text, ruling, ruling_locus, "
            "       offer_of_proof FROM trial_preservation "
            "WHERE trial_id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s "
            "ORDER BY objection_page, objection_line", (str(trial_id), tenant))
        entries = []
        line_cache = {}
        for (pid, tid, pg, ln, locus, grounds, otext, ruling, rlocus, oop) in cur.fetchall():
            if tid not in line_cache:
                line_cache[tid] = _load_lines(cur, tid)
            rows, idx = line_cache[tid]
            ctx = _context(rows, idx, pg, ln)
            entries.append({"id": pid, "locus": locus, "grounds": grounds, "ruling": ruling,
                            "ruling_locus": rlocus, "oop": bool(oop),
                            "context": " | ".join(x for x in [grounds, otext, ctx] if x)})
        if not entries:
            return {"error": "no trial_preservation entries for this trial -- run U3 extract first"}

        # embed issues + entry contexts together, cosine match
        texts = list(issues) + [e["context"][:4000] for e in entries]
        vecs, _model, _rev = _embed(EMBED_URL_DEFAULT, texts)
        ni = len(issues)

        results = []
        for qi, issue in enumerate(issues):
            ctype = _classify(issue)
            issue_lower = issue.lower()
            sims = []
            for ej, e in enumerate(entries):
                raw = _cosine(vecs[qi], vecs[ni + ej])
                order = raw + _ground_bonus(issue_lower, e["grounds"])
                sims.append((order, raw, e))
            sims.sort(key=lambda s: s[0], reverse=True)   # rank by bonus-adjusted score
            matches = [{"preservation_id": e["id"], "locus": e["locus"], "ruling": e["ruling"],
                        "ruling_locus": e["ruling_locus"], "grounds": e["grounds"],
                        "oop": e["oop"], "similarity": round(raw, 4)}
                       for _order, raw, e in sims[:TOP_K] if raw >= MATCH_FLOOR]
            status, needs_oop, rationale = _decide(ctype, matches)
            best = round(sims[0][1], 4) if sims else None
            results.append({"issue_index": qi + 1, "issue_text": issue, "complaint_type": ctype,
                            "status": status, "needs_offer_of_proof": needs_oop,
                            "rationale": rationale, "matched": matches, "best_similarity": best})

        # persist
        cur.execute("DELETE FROM issue_preservation WHERE appellate_case_id=CAST(%s AS uuid) "
                    "AND source='auto'", (str(appellate_case_id),))
        for r in results:
            cur.execute(
                "INSERT INTO issue_preservation (tenant_id, matter_id, appellate_case_id, trial_id, "
                "  issue_index, issue_text, complaint_type, status, needs_offer_of_proof, rationale, "
                "  matched, best_similarity, source) VALUES (%s, CAST(%s AS uuid), CAST(%s AS uuid), "
                "  CAST(%s AS uuid), %s,%s,%s,%s,%s,%s, CAST(%s AS jsonb), %s, 'auto') "
                "ON CONFLICT (appellate_case_id, issue_index) DO UPDATE SET "
                "  issue_text=EXCLUDED.issue_text, complaint_type=EXCLUDED.complaint_type, "
                "  status=EXCLUDED.status, needs_offer_of_proof=EXCLUDED.needs_offer_of_proof, "
                "  rationale=EXCLUDED.rationale, matched=EXCLUDED.matched, "
                "  best_similarity=EXCLUDED.best_similarity, updated_at=now()",
                (tenant, matter_id, str(appellate_case_id), trial_id, r["issue_index"],
                 r["issue_text"][:2000], r["complaint_type"], r["status"], r["needs_offer_of_proof"],
                 r["rationale"], json.dumps(r["matched"]), r["best_similarity"]))
        conn.commit()

        from collections import Counter
        return {"appellate_case_id": str(appellate_case_id), "issues": len(results),
                "by_status": dict(Counter(r["status"] for r in results)),
                "by_type": dict(Counter(r["complaint_type"] for r in results)),
                "results": results}
    finally:
        conn.close()


def list_preservation(tenant_id, appellate_case_id) -> list:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT issue_index, complaint_type, status, needs_offer_of_proof, best_similarity, "
            "       rationale, issue_text FROM issue_preservation "
            "WHERE appellate_case_id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s ORDER BY issue_index",
            (str(appellate_case_id), (tenant_id or "").strip()))
        cols = ["issue_index", "complaint_type", "status", "needs_offer_of_proof",
                "best_similarity", "rationale", "issue_text"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Preservation check (Module B / U8)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--appeal", required=True)
    ap.add_argument("--issues-from", dest="issues_from", default="section",
                    choices=["section", "brief"])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    if args.list:
        out = list_preservation(args.tenant, args.appeal)
    else:
        out = check_preservation(args.tenant, args.appeal, issues_source=args.issues_from)
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
