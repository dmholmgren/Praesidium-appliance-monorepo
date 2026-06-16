"""appellate_completeness.py -- Appellate Surface / C-6: record completeness / omission
detection (the novel guardrail).

The appellate surface holds BOTH the parent trial matter's full file (projected via
the matter_links 'appeal_of' row -> folder 06) AND the clerk-compiled Clerk's Record
(parsed by the U9.0 cr_parser into document_sections 'cr_filing'). C-6 compares them:

  CR-OMISSION  trial-court filing types present in the trial file but ABSENT from the
               Clerk's Record  ->  prompt TRAP 34.5(c) supplemental clerk's record.
  RR-GAP       Reporter's Record volumes/pages the brief relies on but not in the
               filed record  ->  prompt TRAP 34.6 supplemental reporter's record.

Matching is canonical-filing-TYPE coverage (deterministic + explainable -- this is a
legal guardrail), because trial dms_documents are path-indexed with no extracted text.
Embedding corroboration (best-effort) records the closest thing actually in the CR.
Omissions the brief relies on are elevated. Idempotent: replaces prior OPEN gaps,
preserves dismissed/resolved ones.

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.appellate_completeness --matter UUID [--tenant T] [--write]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re

logger = logging.getLogger(__name__)


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


# ---- canonical filing-type classifier over the trial filename -----------------
# (ctype, cr_expected) -- cr_expected=True means this filed document belongs in a
# clerk's record and its absence is a real omission candidate.
_RULES = [
    (r"^~\$", "temp", False),
    (r"notebook", "internal_notebook", False),
    (r"automated certificate|certificate of (service|conference)", "certificate", False),
    (r"proposed (order|final judgment|findings|fof)", "proposed_draft", False),
    (r"notice of (hearing|trial setting|dismissal)|amended notice of hearing", "notice_admin", False),
    (r"vacation letter|letter to court", "correspondence", False),
    (r"request for (clerk|reporter)'s record", "record_request", False),
    (r"status report", "status_report", False),
    (r"disclosure|discovery|responses to post[- ]judgment", "discovery", False),
    (r"declaration", "declaration_evidence", False),
    (r"exhibit list|witness list|trial exhibits|rule 166|slip listing|pre-trial filing|"
     r"^px\d|^pl\d|^d\d|settlement agreement|guaranty", "trial_prep_exhibit", False),
    (r"no[- ]evidence motion for summary judgment", "msj_noevidence", True),
    (r"response to no[- ]evidence", "msj_response", True),
    (r"reply iso no evidence", "msj_reply", True),
    (r"traditional motion for summary judgment|pages from traditional motion", "msj_traditional", True),
    (r"response to traditional motion for summary", "msj_response", True),
    (r"reply iso traditional", "msj_reply", True),
    (r"response to (motion for )?summary judgment|response to.*msj", "msj_response", True),
    (r"motion to strike", "motion_strike", True),
    (r"response to motion for leave|supplemental response to motion for leave", "motion_leave", True),
    (r"motion for leave", "motion_leave", True),
    (r"reply iso motion for attorney|reply.*attorneys'? fees", "motion_fees", True),
    (r"response to motion for attorney", "motion_fees", True),
    (r"motion for attorney", "motion_fees", True),
    (r"post[- ]trial brief", "post_trial_brief", True),
    (r"motion for plaintiff'?s witness to appear|agreed motion for witness to appear", "motion_admin", False),
    (r"first amended answer", "answer", True),
    (r"answer to counterclaim|answer to petition in intervention", "answer", True),
    (r"answer", "answer", True),
    (r"counterclaim", "counterclaim", True),
    (r"petition in intervention|intervention", "intervention", True),
    (r"final judgment", "judgment", True),
    (r"notice of appeal", "notice_of_appeal", True),
    (r"return of service", "return_of_service", True),
    (r"findings of fact|fof and col|foc and col", "ffcl", True),
    (r"petition", "petition", True),
]

# cr_type (from cr_parser) -> canonical types it covers in the record.
_CR_COVERS = {
    "petition": {"petition"},
    "answer": {"answer"},
    "intervention": {"intervention"},
    "counterclaim": {"counterclaim"},
    "findings_conclusions": {"ffcl"},
    "judgment": {"judgment"},
    "notice_of_appeal": {"notice_of_appeal"},
}

# omission grouping: ctype -> (gap_group, base_severity, brief_keyword)
# base severity is INTRINSIC importance with NO brief reliance; brief_reliance
# (the keyword appearing in the brief) bumps it one notch. The scope's load-bearing
# signal is "filings the brief relies on" -- so an omission the brief never cites
# stays a review-candidate, and only a relied-on omission escalates.
_GROUP = {
    "msj_noevidence":  ("summary_judgment", "medium", "summary judgment"),
    "msj_traditional": ("summary_judgment", "medium", "summary judgment"),
    "msj_response":    ("summary_judgment", "medium", "summary judgment"),
    "msj_reply":       ("summary_judgment", "medium", "summary judgment"),
    "motion_strike":   ("motion_to_strike", "low", "motion to strike"),
    "motion_leave":    ("motion_for_leave", "low", "leave to"),
    "motion_fees":     ("attorneys_fees", "medium", "attorney"),
    "post_trial_brief": ("post_trial_brief", "medium", "post-trial"),
    "return_of_service": ("service", "low", None),
    "ffcl":            ("findings", "high", "findings of fact"),
}
_GROUP_LABEL = {
    "summary_judgment": "Summary-judgment motion practice",
    "motion_to_strike": "Motion to Strike (and any ruling)",
    "motion_for_leave": "Motion for Leave (and response)",
    "attorneys_fees": "Motion for Attorneys' Fees (and ruling)",
    "post_trial_brief": "Post-Trial Brief",
    "service": "Return of Service",
    "findings": "Findings of Fact and Conclusions of Law",
}
_BUMP = {"low": "medium", "medium": "high", "high": "high"}


def _norm(fn):
    s = fn.lower()
    s = re.sub(r"\.(pdf|docx|doc)$", "", s)
    s = re.sub(r"\s*\(\d+\)\s*$", "", s)              # "(1)" dup marker
    s = re.sub(r"[_\-]?redacted[_\-]*", "", s)
    return s.strip()


def _classify(fn):
    s = _norm(fn)
    for pat, ct, exp in _RULES:
        if re.search(pat, s):
            return ct, exp
    return "other", False


def _trial_root(cur, matter_id):
    """Resolve the parent trial matter (matter_links 'appeal_of') -> its disk_root."""
    cur.execute(
        "SELECT mf.disk_root FROM matter_links ml "
        "JOIN matter_folders mf ON mf.matter_id = ml.to_matter_id "
        "WHERE ml.from_matter_id = CAST(%s AS uuid) AND ml.relation='appeal_of' "
        "  AND COALESCE(mf.disk_root,'') <> '' LIMIT 1", (matter_id,))
    r = cur.fetchone()
    return r[0] if r else None


def _embed_sim(group_titles, cr_labels):
    """best-effort: cosine of each group title vs the CR filing labels. Returns
    {group: (best_label, best_sim)}; {} if the embed service is unavailable."""
    try:
        from modules.depositions.jobs.embed_qa import _embed, EMBED_URL_DEFAULT
        import math
        base = os.environ.get("EMBED_URL", EMBED_URL_DEFAULT)
        texts = group_titles + cr_labels
        embs, _m, _r = _embed(base, texts)
        g = embs[:len(group_titles)]
        c = embs[len(group_titles):]

        def cos(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(y * y for y in b))
            return dot / (na * nb) if na and nb else 0.0
        out = {}
        for i, gt in enumerate(group_titles):
            best, bs = None, -1.0
            for j, cl in enumerate(cr_labels):
                s = cos(g[i], c[j])
                if s > bs:
                    bs, best = s, cl
            out[gt] = (best, round(bs, 4))
        return out
    except Exception as e:
        logger.warning("embed corroboration skipped: %s", e)
        return {}


def check(tenant_id, matter_id, write=False) -> dict:
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT m.matter_number, ac.id::text, ac.appellant FROM matters m "
            "LEFT JOIN appellate_cases ac ON ac.matter_id=m.id "
            "WHERE m.id=CAST(%s AS uuid) AND TRIM(m.tenant_id)=%s", (matter_id, tenant))
        row = cur.fetchone()
        if not row:
            return {"error": "matter not found"}
        matter_number, cid, appellant = row
        if not cid:
            return {"error": "no appellate_case for this matter"}

        root = _trial_root(cur, matter_id)
        if not root:
            return {"error": "no parent trial matter linked (run the appeal_of link first)"}
        if not root.endswith("/"):
            root += "/"

        # --- trial file: classify, dedup docx/pdf twins ---
        cur.execute("SELECT file_path FROM dms_documents WHERE file_path LIKE %s", (root + "%",))
        trial = cur.fetchall()
        seen, present = set(), {}     # ctype -> [filenames]
        for (fp,) in trial:
            fn = fp.split("/")[-1]
            stem = _norm(fn)
            if stem in seen:
                continue
            seen.add(stem)
            ct, exp = _classify(fn)
            if not exp:
                continue
            present.setdefault(ct, []).append(fn)

        # --- CR coverage ---
        cur.execute(
            "SELECT DISTINCT attributes->>'cr_type', attributes->>'cite' FROM document_sections "
            "WHERE section_type='cr_filing' AND attributes->>'appellate_case_id'=%s", (cid,))
        cr_rows = cur.fetchall()
        covered = set()
        cr_labels = []
        for ct, cite in cr_rows:
            covered |= _CR_COVERS.get(ct, set())
            cr_labels.append("%s (%s)" % (ct, cite or ""))

        # --- brief reliance corpus ---
        cur.execute("SELECT COALESCE(string_agg(body,' '),'') FROM brief_sections "
                    "WHERE appellate_case_id=CAST(%s AS uuid)", (cid,))
        brief_text = (cur.fetchone()[0] or "").lower()

        # --- group omissions ---
        groups = {}   # gap_group -> {types:set, files:[], sev, kw}
        for ct, files in present.items():
            if ct in covered:
                continue
            grp, sev, kw = _GROUP.get(ct, (ct, "medium", None))
            g = groups.setdefault(grp, {"types": set(), "files": [], "sev": sev, "kw": kw})
            g["types"].add(ct)
            g["files"].extend(files)
            if {"low": 0, "medium": 1, "high": 2}[sev] > {"low": 0, "medium": 1, "high": 2}[g["sev"]]:
                g["sev"] = sev

        sim = _embed_sim([_GROUP_LABEL.get(g, g) for g in groups], cr_labels) if groups else {}

        gaps = []
        for grp, g in groups.items():
            label = _GROUP_LABEL.get(grp, grp)
            relied = bool(g["kw"] and g["kw"] in brief_text)
            sev = _BUMP[g["sev"]] if relied else g["sev"]
            best_label, best_sim = sim.get(label, (None, None))
            gaps.append({
                "gap_kind": "cr_omission", "filing_type": grp, "severity": sev,
                "title": "%s not in the Clerk's Record" % label,
                "detail": "%d trial-court file(s): %s" % (
                    len(g["files"]), "; ".join(sorted(set(g["files"]))[:8])),
                "trial_refs": sorted(set(g["files"])),
                "best_cr_match": best_label, "best_sim": best_sim,
                "brief_reliance": relied, "rule_cite": "TRAP 34.5(c)",
                "prompt": ("The brief relies on this; " if relied else "")
                          + "designate a Supplemental Clerk's Record (TRAP 34.5(c)) or confirm "
                            "the filing is intentionally outside the record.",
            })

        # --- RR-gap pass ---
        cur.execute("SELECT volume, page_count FROM record_documents "
                    "WHERE appellate_case_id=CAST(%s AS uuid) AND record_kind IN ('RR','SUPP_RR') "
                    "  AND is_record=true", (cid,))
        filed = {(v if v is not None else 1): (pc or 0) for v, pc in cur.fetchall()}
        cur.execute("SELECT record_cite FROM record_fact_element_links "
                    "WHERE appellate_case_id=CAST(%s AS uuid) AND fact_kind='rr_qa' "
                    "  AND status<>'rejected' AND record_cite IS NOT NULL", (cid,))
        rr_cites = [r[0] for r in cur.fetchall()]
        cited = {}    # vol -> max page cited
        for c in rr_cites:
            m = re.search(r"(?:(\d+)\s*RR\b.*?|RR\s*)(\d+)\s*:", c) or re.search(r"RR\s*(\d+)", c)
            if not m:
                continue
            grps = m.groups()
            vol = int(grps[0]) if len(grps) > 1 and grps[0] else 1
            pg = int(grps[-1]) if grps[-1] else 0
            cited[vol] = max(cited.get(vol, 0), pg)
        for vol, maxpg in cited.items():
            if vol not in filed:
                gaps.append({
                    "gap_kind": "rr_gap", "filing_type": "rr_volume", "severity": "high",
                    "title": "Reporter's Record volume %d cited but not in the record" % vol,
                    "detail": "Brief cites RR vol %d (to p.%d) -- not among filed RR volumes %s." % (
                        vol, maxpg, sorted(filed)),
                    "trial_refs": [], "best_cr_match": None, "best_sim": None,
                    "brief_reliance": True, "rule_cite": "TRAP 34.6",
                    "prompt": "Request/obtain RR vol %d (TRAP 34.6) before relying on it." % vol})
            elif maxpg > filed[vol]:
                gaps.append({
                    "gap_kind": "rr_gap", "filing_type": "rr_page", "severity": "medium",
                    "title": "RR vol %d cite (p.%d) beyond the filed volume (%d pp)" % (vol, maxpg, filed[vol]),
                    "detail": "A cited RR page exceeds the filed volume length -- verify pagination "
                              "or obtain a complete volume.",
                    "trial_refs": [], "best_cr_match": None, "best_sim": None,
                    "brief_reliance": True, "rule_cite": "TRAP 34.6",
                    "prompt": "Verify the RR cite or obtain the complete volume (TRAP 34.6)."})

        run_id = "c6-%s" % matter_number
        written = 0
        if write:
            cur.execute("DELETE FROM record_gaps WHERE matter_id=CAST(%s AS uuid) AND status='open'",
                        (matter_id,))
            for g in gaps:
                cur.execute(
                    "INSERT INTO record_gaps (tenant_id, matter_id, appellate_case_id, gap_kind, "
                    "  filing_type, severity, title, detail, trial_refs, best_cr_match, best_sim, "
                    "  brief_reliance, rule_cite, prompt, detected_run) VALUES "
                    "(%s, CAST(%s AS uuid), CAST(%s AS uuid), %s, %s, %s, %s, %s, CAST(%s AS jsonb), "
                    " %s, %s, %s, %s, %s, %s)",
                    (tenant, matter_id, cid, g["gap_kind"], g["filing_type"], g["severity"],
                     g["title"], g["detail"], json.dumps(g["trial_refs"]), g["best_cr_match"],
                     g["best_sim"], g["brief_reliance"], g["rule_cite"], g["prompt"], run_id))
                written += 1
            conn.commit()

        gaps.sort(key=lambda g: ({"high": 0, "medium": 1, "low": 2}[g["severity"]], g["gap_kind"]))
        return {"matter": matter_number, "appellate_case_id": cid, "trial_root": root,
                "cr_covered_types": sorted(covered), "gaps_found": len(gaps),
                "written": written, "gaps": gaps}
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Record completeness / omission check (C-6)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--matter", required=True)
    ap.add_argument("--write", action="store_true", help="persist gaps into record_gaps")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    out = check(args.tenant, args.matter, write=args.write)
    if args.show and "gaps" in out:
        for g in out["gaps"]:
            print("[%s] %s -- %s" % (g["severity"].upper(), g["title"], g["rule_cite"]))
            print("    ", g["detail"])
            if g.get("best_cr_match"):
                print("     closest in CR: %s (sim %s)" % (g["best_cr_match"], g["best_sim"]))
        out = {k: v for k, v in out.items() if k != "gaps"}
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
