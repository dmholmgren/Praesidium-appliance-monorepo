"""trial_preservation.py -- Module A / Unit 3: objection<->ruling index + extractor.

Tier-1 deterministic population from the trial transcript: walk page:line lines,
detect an objection (a lawyer speaker line carrying "objection" / "I object"),
accumulate its grounds across continuation lines, then scan forward for the
court's next *ruling* utterance (skipping clarifying questions like
"What's your objection?"). Flags running/continuing objections, offers of proof /
bills of exception, and motion-in-limine references. Sets a Tier-1 `preserved`
heuristic (ruled => yes, no ruling found => unclear); Module B/U8 refines this
against the specific appellate complaints.

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.trial_preservation --extract --transcript UUID [--tenant T]
  python -m modules.depositions.jobs.trial_preservation --list --trial UUID [--ruling overruled]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# A speaker line: "MR. HOLMGREN:", "MS. RYBACK:", "THE COURT:", "THE WITNESS:",
# "BY MR. SMITH:", "Q.", "A."  -- the boundary between utterances.
_SPEAKER = re.compile(
    r"^\s*(?P<sp>(?:THE\s+COURT|THE\s+WITNESS|THE\s+BAILIFF|THE\s+REPORTER|"
    r"(?:BY\s+)?(?:MR|MS|MRS|DR)\.\s+[A-Z][A-Za-z'\-]+|Q|A))\b\s*[:.]", re.I)
# A lawyer (not the court/witness) -- the only speaker who can lodge an objection.
_LAWYER = re.compile(r"^\s*(?P<sp>(?:MR|MS|MRS|DR)\.\s+[A-Z][A-Za-z'\-]+)\s*:", re.I)
_COURT = re.compile(r"^\s*THE\s+COURT\s*:", re.I)

_OBJECT = re.compile(r"\bobjection\b|\bI\s+object\b|\bwe\s+object\b", re.I)
# Ruling keywords in a THE COURT utterance (a clarifying question carries none).
_SUSTAIN = re.compile(r"\bsustain(?:ed|s|ing)?\b", re.I)
_OVERRULE = re.compile(r"\boverrul(?:e|ed|es|ing)\b", re.I)
_GRANT = re.compile(r"\bgrant(?:ed|s|ing)?\b", re.I)
_DENY = re.compile(r"\b(?:deni(?:ed|es)|deny)\b", re.I)
_CARRY = re.compile(r"\bcarr(?:y|ied|ies)\b|\btake\s+(?:it|that)\s+under\s+advisement\b|\breserve\s+ruling\b", re.I)
_STRICK = re.compile(r"\bstrick(?:en)?\b|\bstrike\b|\bdisregard\b", re.I)
_ALLOW = re.compile(r"\bI'?ll\s+allow\s+it\b|\byou\s+may\s+answer\b|\bgo\s+ahead\b|\bI'?ll\s+permit\b", re.I)
_WITHDRAW = re.compile(r"\bwithdraw(?:s|n|ing)?\b", re.I)

_RUNNING = re.compile(r"\b(?:running|continuing)\s+objection\b", re.I)
_MIL = re.compile(r"\bmotion\s+in\s+limine\b|\bin\s+limine\b|\blimine\b", re.I)
_OOP = re.compile(r"\boffer\s+of\s+proof\b|\bbill\s+of\s+exception", re.I)
# Counsel self-abandons the objection (no ruling sought) -> not preserved (TRAP 33.1).
_ABANDON = re.compile(
    r"\bwithdraw\s+(?:the|my|that)\s+objection\b|\b(?:I'?ll|I\s+will|we'?ll|we\s+will)\s+move\s+on\b"
    r"|\bnever\s*mind\b|\bI'?ll\s+rephrase\b|\bI'?ll\s+withdraw\b", re.I)

# Normalized grounds vocabulary -- (regex, code). Order matters (specific first).
_GROUNDS = [
    (re.compile(r"\bhearsay\b", re.I), "hearsay"),
    (re.compile(r"\b(?:lack[s]?\s+(?:of\s+)?)?(?:foundation|predicate)\b", re.I), "foundation"),
    (re.compile(r"\bspeculat\w+\b|\bcalls?\s+for\s+speculation\b", re.I), "speculation"),
    (re.compile(r"\blead(?:ing)?\b", re.I), "leading"),
    (re.compile(r"\brelevan\w+\b|\bimmaterial\b", re.I), "relevance"),
    (re.compile(r"\bnon\s*-?\s*responsive\b", re.I), "nonresponsive"),
    (re.compile(r"\bargumentative\b", re.I), "argumentative"),
    (re.compile(r"\basked\s+and\s+answered\b", re.I), "asked_and_answered"),
    (re.compile(r"\bbest\s+evidence\b", re.I), "best_evidence"),
    (re.compile(r"\bprivileg\w+\b", re.I), "privilege"),
    (re.compile(r"\bcumulative\b", re.I), "cumulative"),
    (re.compile(r"\b(?:unfair\w*\s+)?prejudic\w+\b|\b403\b", re.I), "prejudice"),
    (re.compile(r"\b(?:beyond|outside|exceeds)\s+the\s+scope\b", re.I), "beyond_scope"),
    (re.compile(r"\bform\s+of\s+the\s+question\b|\bform\b", re.I), "form"),
    (re.compile(r"\bnarrative\b", re.I), "narrative"),
    (re.compile(r"\bcompound\b", re.I), "compound"),
    (re.compile(r"\bvague\w*\b|\bambiguous\b", re.I), "vague"),
    (re.compile(r"\bassumes\s+facts?\s+not\s+in\s+evidence\b", re.I), "assumes_facts"),
    (re.compile(r"\b(?:calls?\s+for\s+a?\s*)?legal\s+conclusion\b", re.I), "legal_conclusion"),
    (re.compile(r"\bmis(?:states?|characteriz\w+)\b|\bmisquot\w+\b", re.I), "misstates"),
    (re.compile(r"\blacks?\s+personal\s+knowledge\b", re.I), "lacks_knowledge"),
    (re.compile(r"\b(?:not\s+)?authenticat\w+\b", re.I), "authentication"),
    (re.compile(r"\bsidebar\b", re.I), "sidebar"),
    (re.compile(r"\bimproper\b", re.I), "improper"),
]

_MAX_GROUND_LINES = 5   # objection utterance window for grounds
_MAX_RULING_LINES = 14  # forward window to find the court's ruling


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _speaker_of(text):
    m = _SPEAKER.match(text or "")
    return m.group("sp").upper().strip() if m else None


def _after_colon(text):
    i = (text or "").find(":")
    return text[i + 1:].strip() if i >= 0 else (text or "").strip()


def _grounds_of(text):
    codes = []
    for rx, code in _GROUNDS:
        if rx.search(text) and code not in codes:
            codes.append(code)
    return codes


def _ruling_of(text):
    """Classify a THE COURT utterance. Returns (ruling, stricken) or (None, False)."""
    stricken = bool(_STRICK.search(text))
    if _SUSTAIN.search(text):
        return "sustained", stricken
    if _OVERRULE.search(text):
        return "overruled", stricken
    if _CARRY.search(text):
        return "carried", stricken
    if _ALLOW.search(text):
        return "overruled", stricken      # "I'll allow it" == objection overruled
    if _GRANT.search(text):
        return "granted", stricken
    if _DENY.search(text):
        return "denied", stricken
    return None, stricken


def extract_preservation(tenant_id, transcript_id) -> dict:
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT trial_id::text, matter_id::text, COALESCE(volume,0) "
                    "FROM deposition_transcripts WHERE id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s",
                    (str(transcript_id), tenant))
        meta = cur.fetchone()
        if not meta:
            raise ValueError("transcript not found")
        trial_id, matter_id, vol = meta
        cur.execute("SELECT page, line, text FROM transcript_lines "
                    "WHERE transcript_id=CAST(%s AS uuid) ORDER BY page, line",
                    (str(transcript_id),))
        lines = cur.fetchall()
        n_lines = len(lines)

        def locus(p, l):
            return ("%d RR %d:%d" % (vol, p, l)) if vol else ("RR %d:%d" % (p, l))

        events = []
        i = 0
        while i < n_lines:
            page, line, text = lines[i]
            text = text or ""
            lm = _LAWYER.match(text)
            if not (lm and _OBJECT.search(text)):
                i += 1
                continue

            speaker = lm.group("sp").upper().strip()
            # (1) accumulate the objection utterance across continuation lines
            utter = [_after_colon(text)]
            j = i + 1
            while j < n_lines and (j - i) <= _MAX_GROUND_LINES:
                t2 = lines[j][2] or ""
                if _speaker_of(t2):           # next speaker ends this utterance
                    break
                utter.append(t2.strip())
                j += 1
            obj_text = " ".join(u for u in utter if u).strip()

            grounds = _grounds_of(obj_text)
            running = bool(_RUNNING.search(obj_text))
            mil = bool(_MIL.search(obj_text))
            oop = bool(_OOP.search(obj_text))

            # (2) scan forward for the court's next ruling utterance
            ruling = ruling_text = None
            rpage = rline = None
            stricken = False
            k = i + 1
            while k < n_lines and (k - i) <= _MAX_RULING_LINES:
                kp, kl, kt = lines[k]
                kt = kt or ""
                if _COURT.match(kt):
                    # gather the court's utterance (may span a couple lines)
                    cu = [_after_colon(kt)]
                    m = k + 1
                    while m < n_lines and (m - k) <= 2 and not _speaker_of(lines[m][2] or ""):
                        cu.append((lines[m][2] or "").strip()); m += 1
                    court_txt = " ".join(c for c in cu if c).strip()
                    r, st = _ruling_of(court_txt)
                    if r:
                        ruling, ruling_text, rpage, rline = r, court_txt, kp, kl
                        stricken = st
                        break
                    # a clarifying court question (no ruling kw) -- keep scanning
                elif _LAWYER.match(kt) and not _OBJECT.search(kt):
                    # another lawyer started arguing; keep scanning for the court
                    pass
                k += 1

            ruled = ruling is not None
            # self-abandoned objection (no court ruling): withdrawn, not preserved
            if not ruled and _ABANDON.search(obj_text):
                ruling, preserved = "withdrawn", "no"
            else:
                preserved = "yes" if ruled else "unclear"
            events.append({
                "speaker": speaker, "grounds": ",".join(grounds) or None,
                "obj_text": obj_text[:2000] or None, "page": page, "line": line,
                "locus": locus(page, line), "ruled": ruled, "ruling": ruling,
                "ruling_text": (ruling_text or None) and ruling_text[:1000],
                "rpage": rpage, "rline": rline,
                "rlocus": locus(rpage, rline) if rpage else None,
                "running": running, "mil": mil, "oop": oop, "stricken": stricken,
                "preserved": preserved,
            })
            i = j   # resume past the consumed objection utterance

        # idempotent: clear this transcript's auto rows, re-insert
        cur.execute("DELETE FROM trial_preservation WHERE transcript_id=CAST(%s AS uuid) "
                    "AND source='auto'", (str(transcript_id),))
        for e in events:
            cur.execute(
                "INSERT INTO trial_preservation (tenant_id, matter_id, trial_id, transcript_id, "
                "  objector_speaker, grounds, objection_text, objection_page, objection_line, "
                "  objection_locus, ruled, ruling, ruling_text, ruling_page, ruling_line, "
                "  ruling_locus, running, motion_in_limine, offer_of_proof, stricken, preserved, "
                "  source) VALUES (%s, CAST(%s AS uuid), CAST(%s AS uuid), CAST(%s AS uuid), "
                "  %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'auto') "
                "ON CONFLICT (transcript_id, objection_page, objection_line) DO UPDATE SET "
                "  objector_speaker=EXCLUDED.objector_speaker, grounds=EXCLUDED.grounds, "
                "  objection_text=EXCLUDED.objection_text, ruled=EXCLUDED.ruled, "
                "  ruling=EXCLUDED.ruling, ruling_text=EXCLUDED.ruling_text, "
                "  ruling_page=EXCLUDED.ruling_page, ruling_line=EXCLUDED.ruling_line, "
                "  ruling_locus=EXCLUDED.ruling_locus, running=EXCLUDED.running, "
                "  motion_in_limine=EXCLUDED.motion_in_limine, offer_of_proof=EXCLUDED.offer_of_proof, "
                "  stricken=EXCLUDED.stricken, preserved=EXCLUDED.preserved, updated_at=now()",
                (tenant, matter_id, trial_id, str(transcript_id), e["speaker"], e["grounds"],
                 e["obj_text"], e["page"], e["line"], e["locus"], e["ruled"], e["ruling"],
                 e["ruling_text"], e["rpage"], e["rline"], e["rlocus"], e["running"], e["mil"],
                 e["oop"], e["stricken"], e["preserved"]))
        conn.commit()
        from collections import Counter
        return {"transcript_id": str(transcript_id), "trial_id": trial_id,
                "objections": len(events),
                "by_ruling": dict(Counter(e["ruling"] or "none" for e in events)),
                "running": sum(1 for e in events if e["running"]),
                "offers_of_proof": sum(1 for e in events if e["oop"])}
    finally:
        conn.close()


def list_preservation(tenant_id, trial_id, ruling=None) -> list:
    conn = _connect()
    try:
        cur = conn.cursor()
        sql = ("SELECT objector_speaker, grounds, objection_locus, ruled, ruling, ruling_locus, "
               "       running, offer_of_proof, stricken, preserved, objection_text "
               "FROM trial_preservation WHERE trial_id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s")
        params = [str(trial_id), (tenant_id or "").strip()]
        if ruling:
            sql += " AND ruling=%s"; params.append(ruling)
        sql += " ORDER BY objection_page, objection_line"
        cur.execute(sql, params)
        cols = ["speaker", "grounds", "objection_locus", "ruled", "ruling", "ruling_locus",
                "running", "offer_of_proof", "stricken", "preserved", "objection_text"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Trial preservation index (Module A / U3)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--transcript", default=None)
    ap.add_argument("--trial", default=None)
    ap.add_argument("--ruling", default=None)
    args = ap.parse_args()
    if args.extract:
        out = extract_preservation(args.tenant, args.transcript)
    elif args.list:
        out = list_preservation(args.tenant, args.trial, ruling=args.ruling)
    else:
        ap.error("one of --extract/--list required")
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
