# -*- coding: utf-8 -*-
"""exhibit_segmenter.py -- bundle-robust exhibit volume mapping + testimony usage linkage.

Operational home of the segmenter (promoted from the build-session script). Runs after
the RR is ingested (geometry captured, transcript_lines + trial_exhibits populated).

A) Volume mapping: resolves ALL RR-side records (RR + SUPP_RR) for the appellate case --
   each may be a separate bundled PDF volume. Per document it computes a testimony
   boundary (a bundle doc has transcript_lines so exhibits sit after the testimony; a
   standalone exhibit-volume PDF has none -> scan all pages), reads exhibit Bates stamps
   (PL01.7, D03.2) from the clean geometry (ocr_paddle else native), groups by exhibit,
   and writes rr_record_id / rr_page_first / rr_page_last / rr_page_label.

B) Usage linkage: scans the testimony (transcript_lines) for every reference to each
   exhibit (reusing the U2 _EX_RE + event classifiers) -> one trial_exhibit_usages row
   per mention (page:line, char offsets, kind, snippet). Bare "Exhibit N" disambiguates
   by preferring the exhibit physically bound in the record volume, then the unique
   party'd register row, over blank-party artifacts.

  python -m modules.depositions.jobs.exhibit_segmenter --tenant T --appeal APPELLATE_CASE_ID [--dry]
"""
from __future__ import annotations
import re
import collections
import logging

logger = logging.getLogger(__name__)

STAMP = re.compile(r'^([A-Za-z]{1,4})0*(\d{1,3})\.(\d+)$')
PREFIX_PARTY = {'PL': 'plaintiff', 'PX': 'plaintiff', 'PLF': 'plaintiff', 'P': 'plaintiff',
                'D': 'defendant', 'DX': 'defendant', 'DEF': 'defendant', 'DF': 'defendant'}
RR_KINDS = ('RR', 'SUPP_RR')
STAMP_SQL = r"^[A-Za-z]{1,4}[0-9]{1,3}\.[0-9]+$"


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _digits(s):
    return re.sub(r'\D', '', s or '')


def segment_exhibits(tenant_id, appellate_case_id, commit=True) -> dict:
    """Map exhibit volume pages and link testimony usages for one appellate case."""
    import psycopg2.extras
    from modules.depositions.jobs.trial_exhibits import _EX_RE, _events, _norm_party

    tenant = (tenant_id or "").strip()
    cid = str(appellate_case_id)
    conn = _connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    out = {"records": 0, "exhibits_mapped": 0, "usages": 0, "unmatched": []}
    try:
        cur.execute(
            "SELECT rd.id::text AS rr_record_id, rd.record_kind, rd.geometry_corpus, "
            "       rd.geometry_doc_id::text AS geometry_doc_id, rd.rr_transcript_id::text AS rr_transcript_id "
            "FROM record_documents rd "
            "WHERE rd.appellate_case_id = CAST(%s AS uuid) AND rd.record_kind IN %s "
            "  AND TRIM(rd.tenant_id) = %s "
            "ORDER BY rd.record_kind, rd.volume NULLS FIRST",
            (cid, RR_KINDS, tenant))
        recs = cur.fetchall()
        out["records"] = len(recs)
        if not recs:
            return out

        by_full, by_num = {}, collections.defaultdict(list)
        for r in recs:
            tid = r["rr_transcript_id"]
            if not tid:
                continue
            cur.execute("SELECT id::text, party, exhibit_number FROM trial_exhibits "
                        "WHERE transcript_id = %s AND TRIM(tenant_id) = %s", (tid, tenant))
            for e in cur.fetchall():
                d = _digits(e["exhibit_number"])
                p = (e["party"] or "").strip().lower()
                by_full[(tid, p, d)] = e["id"]
                by_num[(tid, d)].append((e["id"], p))

        volume_mapped = set()

        def resolve(tid, party, d):
            if party:
                eid = by_full.get((tid, party, d))
                if eid:
                    return eid
            cands = by_num.get((tid, d), [])
            if len(cands) == 1:
                return cands[0][0]
            in_vol = [c for c in cands if c[0] in volume_mapped]
            if len(in_vol) == 1:
                return in_vol[0][0]
            partied = [c for c in cands if c[1]]
            if len(partied) == 1:
                return partied[0][0]
            return None

        # A) volume mapping
        cand = collections.defaultdict(lambda: collections.defaultdict(lambda: [10**9, -1, 0, None]))
        for r in recs:
            if not r["geometry_doc_id"]:
                continue
            cur.execute("SELECT COALESCE(MAX(page),0) AS b FROM transcript_lines WHERE transcript_id=%s",
                        (r["rr_transcript_id"],))
            boundary = cur.fetchone()["b"] or 0
            cur.execute("SELECT rendition FROM doc_geometry WHERE corpus=%s AND doc_id=%s "
                        "ORDER BY (rendition='ocr_paddle') DESC, built_at DESC LIMIT 1",
                        (r["geometry_corpus"], r["geometry_doc_id"]))
            row = cur.fetchone()
            rr_rend = (row or {}).get("rendition") or "native_pdf"
            cur.execute("SELECT page_number, text FROM doc_layout_tokens "
                        "WHERE corpus=%s AND doc_id=%s AND rendition=%s AND page_number > %s AND text ~ %s",
                        (r["geometry_corpus"], r["geometry_doc_id"], rr_rend, boundary, STAMP_SQL))
            for row in cur.fetchall():
                m = STAMP.match(row["text"])
                if not m:
                    continue
                prefix, num = m.group(1).upper(), int(m.group(2))
                party = PREFIX_PARTY.get(prefix)
                if not party:
                    continue
                slot = cand[(party, num)][r["rr_record_id"]]
                slot[0] = min(slot[0], row["page_number"]); slot[1] = max(slot[1], row["page_number"])
                slot[2] += 1; slot[3] = prefix

        for (party, num), docs in sorted(cand.items()):
            rrid, (pf, pl, cnt, prefix) = max(docs.items(), key=lambda kv: kv[1][2])
            label = "%s%02d" % (prefix, num)
            ex_id = None
            for r in recs:
                ex_id = by_full.get((r["rr_transcript_id"], party, str(num)))
                if ex_id:
                    break
            if ex_id:
                cur.execute("UPDATE trial_exhibits SET rr_record_id=%s, rr_page_first=%s, "
                            "rr_page_last=%s, rr_page_label=%s WHERE id=%s",
                            (rrid, pf, pl, label, ex_id))
                out["exhibits_mapped"] += cur.rowcount
                volume_mapped.add(ex_id)
            else:
                out["unmatched"].append(label)

        # B) usage linkage
        for r in recs:
            tid = r["rr_transcript_id"]
            if not tid:
                continue
            cur.execute("DELETE FROM trial_exhibit_usages WHERE transcript_id = %s", (tid,))
            cur.execute("SELECT page, line, char_start, char_end, text FROM transcript_lines "
                        "WHERE transcript_id = %s ORDER BY page, line", (tid,))
            for ln in cur.fetchall():
                text = ln["text"] or ""
                ev = _events(text)
                kind = next((k for k in ("admitted", "offered", "excluded", "marked") if k in ev), "reference")
                seen = set()
                for m in _EX_RE.finditer(text):
                    party = _norm_party(m.group("party"))
                    d = _digits(m.group("num"))
                    if not d:
                        continue
                    ex_id = resolve(tid, party, d)
                    if not ex_id or (ex_id, ln["page"], ln["line"]) in seen:
                        continue
                    seen.add((ex_id, ln["page"], ln["line"]))
                    cur.execute(
                        "INSERT INTO trial_exhibit_usages "
                        "(tenant_id, exhibit_id, transcript_id, page, line, char_start, char_end, locus, usage_kind, snippet) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (tenant, ex_id, tid, ln["page"], ln["line"], ln["char_start"], ln["char_end"],
                         "RR %d:%d" % (ln["page"], ln["line"]), kind, text[:240]))
                    out["usages"] += 1

        if commit:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        logger.exception("exhibit segmentation failed for appeal %s", cid)
        raise
    finally:
        conn.close()
    logger.info("exhibit_segmenter appeal=%s mapped=%d usages=%d unmatched=%s",
                cid, out["exhibits_mapped"], out["usages"], out["unmatched"])
    return out


if __name__ == "__main__":
    import argparse, json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--appeal", required=True, help="appellate_case_id")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    print(json.dumps(segment_exhibits(a.tenant, a.appeal, commit=not a.dry), indent=2))
