"""appellate_verify.py -- Module B / Unit 7: semantic record-cite verification.

The headline / patent candidate. For each factual sentence in the brief that carries
a record cite ([vol] RR [page]/[page-range], CR [page]), resolve the cite against the
ingested page-addressed record (U4) and semantically check -- in the ModernBERT-768
space the record is already embedded in -- whether the cited span actually SUPPORTS
the proposition. Each cite gets a verdict:

  supported    proposition <-> cited span cosine is high
  weak         middling -- attorney should confirm
  unsupported  the cited span does not support the sentence (mis-cite)
  overbroad    supported but the cite points at a large span (page range / long page)
  unresolved   the cite points at a volume/page not in the ingested record (a real
               coverage gap -- e.g. the brief cites 13 RR but only vol 1 is loaded)

No bolt-on brief tool can do this: it requires the record held as addressed,
embedded data. Determinism: bulk work is the cheap local embed, never an LLM pass.

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.appellate_verify --appeal UUID [--brief-doc UUID]
        [--limit N] [--show]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# Calibrated to the ModernBERT-768 record space (on-topic testimony ~0.45-0.6,
# off-topic ~0.3). Tunable; thresholds are the single point to adjust.
SUPPORTED_AT = 0.46
WEAK_AT = 0.34
OVERBROAD_CHARS = 1600

# Record cites in brief prose. RR must be followed by whitespace+digit so the
# exhibit form 'RRCDEx 42' is not captured.
_RR_FIND = re.compile(r"\b(?:\d+\s+)?RR\s+\d+(?:\s*[-–]\s*\d+)?(?::\d+(?:\s*[-–]\s*\d+)?)?", re.I)
_CR_FIND = re.compile(r"\b(?:\d+(?:st|nd|rd|th)?\s+Supp\.?\s+)?CR\s+\d+", re.I)


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _clean_cite(raw):
    """Collapse page furniture inside a cite (\\f/\\n -> space). We keep a leading
    volume even when a page break splits it from RR ('4\\fRR 120' -> '4 RR 120'):
    mis-reading footer bleed as a volume yields a safe 'unresolved' (that volume
    isn't loaded) rather than a false-resolve against the wrong volume."""
    return re.sub(r"\s+", " ", raw).strip()


def _find_cites(text):
    """Yield (raw, kind, start, end) for record cites, de-overlapped (RR wins)."""
    spans = []
    for m in _RR_FIND.finditer(text):
        spans.append((m.start(), m.end(), _clean_cite(m.group(0)), "RR"))
    for m in _CR_FIND.finditer(text):
        spans.append((m.start(), m.end(), _clean_cite(m.group(0)), "CR"))
    spans.sort()
    out, last = [], -1
    for s, e, raw, kind in spans:
        if s < last:
            continue
        out.append((raw, kind, s, e))
        last = e
    return out


_SENT_BACK = re.compile(r"[.;]\s+(?=[A-Z(“\"])")
_SENT_FWD = re.compile(r"[.;]\s")


def _sentence_of(text, start, end):
    """Return (proposition, char_start, char_end): the sentence containing the cite,
    with the cite (and any wrapping parens) removed."""
    ws = max(0, start - 700)
    left = text[ws:start]
    mb = list(_SENT_BACK.finditer(left))
    s0 = ws + (mb[-1].end() if mb else 0)
    we = min(len(text), end + 350)
    mf = _SENT_FWD.search(text[end:we])
    s1 = end + (mf.start() + 1 if mf else (we - end))
    # proposition = sentence minus the cite; trim dangling '(' / '()' / 'See'
    prop = (text[s0:start] + " " + text[end:s1])
    prop = re.sub(r"\(\s*\)", " ", prop)
    prop = re.sub(r"\(\s*(?:see|see also|e\.g\.,?|citing|quoting)?\s*$", " ", prop, flags=re.I)
    prop = re.sub(r"[;,]?\s*\)\s*", " ", prop)
    prop = re.sub(r"\s+", " ", prop).strip(" ;,.")
    return prop, s0, s1


def _cosine(a, b):
    # praesidium-embed returns normalized vectors -> cosine = dot
    return float(sum(x * y for x, y in zip(a, b)))


def verify_brief(tenant_id, appellate_case_id, brief_document_id=None, limit=0) -> dict:
    from modules.depositions.jobs.appellate_brief import _brief_canonical, _find_brief
    from modules.depositions.jobs.appellate_pipeline import resolve_cite
    from modules.depositions.jobs.embed_qa import _embed, EMBED_URL_DEFAULT
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        brief = _find_brief(cur, appellate_case_id, brief_document_id)
        if not brief:
            return {"error": "no BRIEF document attached"}
        bdoc_id, storage_path, label = brief
        canonical = _brief_canonical(tenant, bdoc_id, storage_path)

        cites = _find_cites(canonical)
        if limit:
            cites = cites[:limit]

        items = []
        for raw, kind, s, e in cites:
            prop, s0, s1 = _sentence_of(canonical, s, e)
            brief_page = canonical[:s].count("\f") + 1
            rr = resolve_cite(tenant, appellate_case_id, raw)
            items.append({"raw": raw, "kind": kind, "start": s, "end": e,
                          "brief_page": brief_page, "prop": prop, "resolve": rr})

        # batch-embed the resolved (proposition, span) pairs
        resolved = [it for it in items if it["resolve"].get("resolved") and it["prop"]]
        if resolved:
            texts = [it["prop"] for it in resolved] + \
                    [it["resolve"]["text"][:4000] for it in resolved]
            vecs, _model, _rev = _embed(EMBED_URL_DEFAULT, texts)
            n = len(resolved)
            for i, it in enumerate(resolved):
                sim = _cosine(vecs[i], vecs[n + i])
                span = it["resolve"]["text"] or ""
                if sim < WEAK_AT:
                    verdict = "unsupported"
                elif len(span) > OVERBROAD_CHARS and sim < SUPPORTED_AT:
                    verdict = "overbroad"
                elif sim < SUPPORTED_AT:
                    verdict = "weak"
                else:
                    verdict = "supported"
                it["similarity"] = round(sim, 4)
                it["verdict"] = verdict
        for it in items:
            if "verdict" not in it:
                it["similarity"] = None
                it["verdict"] = "unresolved"
                it["reason"] = it["resolve"].get("reason", "could not resolve cite")

        # persist
        cur.execute("DELETE FROM record_cite_checks WHERE appellate_case_id=CAST(%s AS uuid) "
                    "AND brief_document_id=CAST(%s AS uuid)", (str(appellate_case_id), bdoc_id))
        from psycopg2.extras import execute_values
        rows = [(tenant, str(appellate_case_id), bdoc_id, it["raw"], it["kind"],
                 it["resolve"].get("locus"), it["brief_page"], it["start"], it["end"],
                 it["prop"][:2000], (it["resolve"].get("text") or "")[:2000],
                 it.get("similarity"), it["verdict"], it.get("reason"))
                for it in items]
        if rows:
            execute_values(
                cur,
                "INSERT INTO record_cite_checks (tenant_id, appellate_case_id, brief_document_id, "
                "  cite_text, cite_kind, locus, brief_page, char_start, char_end, proposition, "
                "  record_span, similarity, verdict, reason) VALUES %s",
                rows,
                template="(%s,CAST(%s AS uuid),CAST(%s AS uuid),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                page_size=500)
        conn.commit()

        from collections import Counter
        summary = dict(Counter(it["verdict"] for it in items))
        flagged = [{"cite": it["raw"], "locus": it["resolve"].get("locus"),
                    "brief_page": it["brief_page"], "verdict": it["verdict"],
                    "similarity": it.get("similarity"), "proposition": it["prop"][:240],
                    "reason": it.get("reason")}
                   for it in items if it["verdict"] != "supported"]
        return {"brief": label, "brief_document_id": bdoc_id, "cites": len(items),
                "summary": summary, "flagged": flagged}
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Record-cite verification (Module B / U7)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--appeal", required=True)
    ap.add_argument("--brief-doc", dest="brief_doc", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    out = verify_brief(args.tenant, args.appeal, args.brief_doc, limit=args.limit)
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
