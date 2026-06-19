"""trial_exhibits.py -- Module A / Unit 2: trial exhibit register + extractor.

Tier-1 deterministic population from the trial transcript: walk the page:line lines,
find exhibit references ("Plaintiff's Exhibit 12", "Exhibit P-3") and the offer /
admit / exclude / withdraw language around them (incl. the court's ruling on the
next line), and upsert one trial_exhibits row per (party, number) with its lifecycle
status + the page:line loci. Editable thereafter; admitted exhibits feed Module B's
record.

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.trial_exhibits --extract --transcript UUID [--tenant T]
  python -m modules.depositions.jobs.trial_exhibits --list --trial UUID
  python -m modules.depositions.jobs.trial_exhibits --admitted --trial UUID
  python -m modules.depositions.jobs.trial_exhibits --set --exhibit UUID --status admitted
        [--document DOC_UUID --source dms]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_EX_RE = re.compile(
    r"\b(?:(?P<party>Plaintiff|Defendant|State|Petitioner|Respondent|Movant|Joint|Court)"
    r"(?:'s|s')?\s+)?Exhibit\s+(?:No\.?\s*)?(?P<num>[A-Z]{0,2}-?\d+[A-Z]?)\b", re.I)

_RE_OFFER = re.compile(r"\boffer(?:s|ed|ing)?\b|\bmove[sd]?\s+(?:to\s+admit|the\s+admission)"
                       r"|\btender(?:s|ed)?\b|\binto\s+evidence\b", re.I)
_RE_ADMIT = re.compile(r"\badmit(?:ted|s)?\b|\breceived\s+in\s+evidence\b|\bwill\s+be\s+received\b"
                       r"|\bis\s+received\b", re.I)
_RE_EXCLUDE = re.compile(r"\bexclud(?:e|ed|ing)\b|\bnot\s+admitted\b|\bdenied\b|\bnot\s+received\b", re.I)
_RE_MARK = re.compile(r"\bmark(?:ed)?\s+for\s+identification\b|\bmark(?:ed)?\b", re.I)
_RE_WITHDRAW = re.compile(r"\bwithdraw(?:s|n|ing)?\b", re.I)

# Reporter's exhibit-index row: "P-1 Settlement Agreement 24 v1 24 v1 R"
# -> label, offered page (+vol), admitted page (+vol), use code.
_INDEX_RE = re.compile(
    r"^(?P<label>[PD]-\d+[A-Z]?)\s+.+?\s+(?P<off>\d+)\s+v\d+\s+(?P<adm>\d+)\s+v\d+\s+[A-Z]\s*$")
# Bulk stipulation: "admit Plaintiff's 1 through 30" / "Defendant's 1".
_RANGE_RE = re.compile(
    r"\b(?P<party>Plaintiff|Defendant|State|Petitioner|Respondent)(?:'s|s')?\s+"
    r"(?:Exhibits?\s+)?(?:Nos?\.?\s*)?(?P<n1>\d+)(?:\s*(?:through|thru|to|[-–])\s*(?P<n2>\d+))?", re.I)
_ADMIT_CTX = re.compile(r"\badmit", re.I)

_RANK = {"marked": 1, "offered": 2, "withdrawn": 3, "excluded": 4, "admitted": 5}


def _new_ex(party, num, label=None):
    return {"party": party, "num": num,
            "label": label or ("%s Exhibit %s" % (party.title() + "'s" if party else "", num)).strip(),
            "status": "marked", "admitted": False, "notes": None,
            "marked_page": None, "marked_line": None, "offered_page": None,
            "offered_line": None, "offered_locus": None, "ruling_page": None,
            "ruling_line": None, "ruling_locus": None}


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _norm_party(p):
    if not p:
        return ""
    p = p.lower()
    return {"petitioner": "plaintiff", "movant": "plaintiff",
            "respondent": "defendant"}.get(p, p)


def _norm_num(n):
    return re.sub(r"\s+", "", (n or "")).upper()


def _events(text):
    ev = set()
    if _RE_OFFER.search(text):
        ev.add("offered")
    if _RE_ADMIT.search(text):
        ev.add("admitted")
    if _RE_EXCLUDE.search(text):
        ev.add("excluded")
    if _RE_WITHDRAW.search(text):
        ev.add("withdrawn")
    if _RE_MARK.search(text):
        ev.add("marked")
    return ev


def _apply(ex, ev, page, line, vol):
    locus = ("%d RR %d:%d" % (vol, page, line)) if vol else ("RR %d:%d" % (page, line))
    if "marked" in ev and ex["marked_page"] is None:
        ex["marked_page"], ex["marked_line"] = page, line
    if "offered" in ev:
        ex["offered_page"], ex["offered_line"], ex["offered_locus"] = page, line, locus
    ruling = ev & {"admitted", "excluded", "withdrawn"}
    if ruling:
        ex["ruling_page"], ex["ruling_line"], ex["ruling_locus"] = page, line, locus
    # status = highest-rank event seen so far
    for e in ("marked", "offered", "withdrawn", "excluded", "admitted"):
        if e in ev and _RANK[e] > _RANK[ex["status"]]:
            ex["status"] = e
    ex["admitted"] = ex["status"] == "admitted"


def extract_exhibits(tenant_id, transcript_id) -> dict:
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT trial_id::text, matter_id::text, COALESCE(volume,0), "
                    "       transcript_kind FROM deposition_transcripts "
                    "WHERE id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s",
                    (str(transcript_id), tenant))
        meta = cur.fetchone()
        if not meta:
            raise ValueError("transcript not found")
        trial_id, matter_id, vol, kind = meta
        cur.execute("SELECT page, line, text FROM transcript_lines "
                    "WHERE transcript_id=CAST(%s AS uuid) ORDER BY page, line",
                    (str(transcript_id),))
        lines = cur.fetchall()

        exhibits = {}
        pending = None   # (key, line_index) last offered, for next-line court rulings
        for i, (page, line, text) in enumerate(lines):
            text = text or ""

            # (1) reporter's exhibit-index row -- authoritative offered/admitted
            mi = _INDEX_RE.match(text)
            if mi:
                lab = mi.group("label")
                party = "plaintiff" if lab.upper().startswith("P") else "defendant"
                num = _norm_num(lab.split("-", 1)[1])
                ex = exhibits.setdefault((party, num), _new_ex(party, num))
                off, adm = int(mi.group("off")), int(mi.group("adm"))
                ex["offered_page"], ex["offered_locus"] = off, "%d RR %d" % (vol or 1, off)
                ex["ruling_page"], ex["ruling_locus"] = adm, "%d RR %d" % (vol or 1, adm)
                ex["status"], ex["admitted"] = "admitted", True
                ex["notes"] = ex["notes"] or "per reporter's exhibit index"
                continue

            ev = _events(text)

            # (2) bulk stipulation -- "admit Plaintiff's 1 through 30"
            if _ADMIT_CTX.search(text):
                for rm in _RANGE_RE.finditer(text):
                    pty = _norm_party(rm.group("party"))
                    n1 = int(rm.group("n1")); n2 = int(rm.group("n2") or n1)
                    if not pty or n1 > n2 or n2 - n1 > 200:
                        continue
                    for nn in range(n1, n2 + 1):
                        ex = exhibits.setdefault((pty, str(nn)), _new_ex(pty, str(nn)))
                        _apply(ex, {"offered", "admitted"}, page, line, vol)
                        ex["notes"] = ex["notes"] or "admitted by stipulation"

            # (3) testimony references + offer/admit language
            refs = list(_EX_RE.finditer(text))
            if refs:
                for m in refs:
                    party = _norm_party(m.group("party"))
                    num = _norm_num(m.group("num"))
                    key = (party, num)
                    ex = exhibits.get(key)
                    if ex is None:
                        ex = _new_ex(party, num, label=m.group(0).strip())
                        exhibits[key] = ex
                    _apply(ex, ev or {"marked"}, page, line, vol)
                    if "offered" in ev:
                        pending = (key, i)
                    if ev & {"admitted", "excluded", "withdrawn"}:
                        pending = None
            elif pending and (i - pending[1] <= 6) and (ev & {"admitted", "excluded"}):
                _apply(exhibits[pending[0]], ev, page, line, vol)
                pending = None

        # reconcile bare 'Exhibit N' references into a uniquely-matching party'd exhibit
        for (pty, num), ex in list(exhibits.items()):
            if pty:
                continue
            cands = [k for k in exhibits if k[1] == num and k[0] in ("plaintiff", "defendant")]
            if len(cands) == 1:
                tgt = exhibits[cands[0]]
                if tgt["marked_page"] is None and ex["marked_page"] is not None:
                    tgt["marked_page"], tgt["marked_line"] = ex["marked_page"], ex["marked_line"]
                del exhibits[(pty, num)]

        # idempotent: clear this transcript's auto-extracted rows (keep doc-linked)
        cur.execute("DELETE FROM trial_exhibits WHERE transcript_id=CAST(%s AS uuid) "
                    "AND document_id IS NULL", (str(transcript_id),))
        n = 0
        for ex in exhibits.values():
            cur.execute(
                "INSERT INTO trial_exhibits (tenant_id, matter_id, trial_id, transcript_id, "
                "  party, exhibit_number, exhibit_label, status, admitted, marked_page, marked_line, "
                "  offered_page, offered_line, offered_locus, ruling_page, ruling_line, ruling_locus) "
                "VALUES (%s, CAST(%s AS uuid), CAST(%s AS uuid), CAST(%s AS uuid), %s,%s,%s,%s,%s,"
                "        %s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (trial_id, party, exhibit_number) DO UPDATE SET "
                "  status=CASE WHEN %s > %s THEN EXCLUDED.status ELSE trial_exhibits.status END, "
                "  admitted=trial_exhibits.admitted OR EXCLUDED.admitted, "
                "  exhibit_label=COALESCE(trial_exhibits.exhibit_label, EXCLUDED.exhibit_label), "
                "  offered_locus=COALESCE(EXCLUDED.offered_locus, trial_exhibits.offered_locus), "
                "  ruling_locus=COALESCE(EXCLUDED.ruling_locus, trial_exhibits.ruling_locus), "
                "  transcript_id=EXCLUDED.transcript_id, updated_at=now()",
                (tenant, matter_id, trial_id, str(transcript_id), ex["party"], ex["num"],
                 ex["label"], ex["status"], ex["admitted"], ex["marked_page"], ex["marked_line"],
                 ex["offered_page"], ex["offered_line"], ex["offered_locus"], ex["ruling_page"],
                 ex["ruling_line"], ex["ruling_locus"],
                 _RANK[ex["status"]], _RANK.get("marked")))
            n += 1
        conn.commit()
        from collections import Counter
        return {"transcript_id": str(transcript_id), "trial_id": trial_id,
                "exhibits": n, "by_status": dict(Counter(e["status"] for e in exhibits.values()))}
    finally:
        conn.close()


def list_exhibits(tenant_id, trial_id) -> list:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT party, exhibit_number, exhibit_label, status, admitted, offered_locus, "
            "       ruling_locus, sponsoring_witness, document_id::text FROM trial_exhibits "
            "WHERE trial_id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s "
            "ORDER BY party, length(exhibit_number), exhibit_number", (str(trial_id), (tenant_id or '').strip()))
        cols = ["party", "number", "label", "status", "admitted", "offered_locus",
                "ruling_locus", "witness", "document_id"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def admitted_exhibits(tenant_id, trial_id) -> list:
    return [e for e in list_exhibits(tenant_id, trial_id) if e["status"] == "admitted"]


def set_exhibit(tenant_id, exhibit_id, status=None, document_id=None, source=None,
                witness=None, notes=None) -> dict:
    conn = _connect()
    try:
        cur = conn.cursor()
        sets, params = [], {}
        if status:
            sets.append("status=%(st)s"); params["st"] = status
            sets.append("admitted=%(ad)s"); params["ad"] = (status == "admitted")
        if document_id:
            sets.append("document_id=CAST(%(doc)s AS uuid)"); params["doc"] = document_id
            sets.append("document_source=%(src)s"); params["src"] = source or "dms"
        if witness is not None:
            sets.append("sponsoring_witness=%(w)s"); params["w"] = witness
        if notes is not None:
            sets.append("notes=%(n)s"); params["n"] = notes
        if not sets:
            return {"error": "nothing to set"}
        sets.append("updated_at=now()")
        params["id"] = exhibit_id; params["t"] = (tenant_id or "").strip()
        cur.execute("UPDATE trial_exhibits SET " + ", ".join(sets) +
                    " WHERE id=CAST(%(id)s AS uuid) AND TRIM(tenant_id)=%(t)s", params)
        conn.commit()
        return {"updated": cur.rowcount}
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Trial exhibit register (Module A / U2)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--admitted", action="store_true")
    ap.add_argument("--set", action="store_true")
    ap.add_argument("--transcript", default=None)
    ap.add_argument("--trial", default=None)
    ap.add_argument("--exhibit", default=None)
    ap.add_argument("--status", default=None)
    ap.add_argument("--document", default=None)
    ap.add_argument("--source", default=None)
    args = ap.parse_args()
    if args.extract:
        out = extract_exhibits(args.tenant, args.transcript)
    elif args.list:
        out = list_exhibits(args.tenant, args.trial)
    elif args.admitted:
        out = admitted_exhibits(args.tenant, args.trial)
    elif args.set:
        out = set_exhibit(args.tenant, args.exhibit, status=args.status,
                          document_id=args.document, source=args.source)
    else:
        ap.error("one of --extract/--list/--admitted/--set required")
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
