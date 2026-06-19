"""Event-typed trial segmentation -- transcript_lines -> transcript_qa_units.

Module A, Unit 1. A trial transcript rides the SAME page:line spine and the SAME
Q&A primitive as a deposition (parsers/qa_segmenter.py), but it is not one long
examination: it is a sequence of events -- voir dire, openings, witness
examinations (direct/cross/redirect/recross), colloquy/sidebars, objections and
rulings, the charge, closings, the verdict. So this segmenter is a thin event
layer over the depo Q&A segmenter, not a fork:

  Pass 1 (structural, no LLM): walk the lines and split them into event blocks
  from the stable stenographic grammar -- examination headers ("CROSS-
  EXAMINATION"), witness-sworn lines ("JOHN SMITH, having been duly sworn"),
  and phase headers ("OPENING STATEMENT", "THE COURT'S CHARGE", "VERDICT").

  Pass 2: examination blocks run THROUGH the depo Q&A segmenter -- witness
  testimony stays the searchable Q&A evidence (embedded in U2, ranked in U10),
  now tagged with its event_type (direct/cross/...) and witness. Narrative
  blocks (openings/charge/closings/colloquy) become one span unit each so
  nothing is lost from the index. Objection/ruling colloquy inside an
  examination is tagged event_type so U3's preservation index has a head start.

Return contract is identical to qa_segmenter.segment(): (units, line_roles),
units carrying an extra .event_type attribute the DAG persists. §0 holds: this
only reads transcript_lines and writes metadata (qa_role/event_type), never
canonical_text.
"""
from __future__ import annotations

import re

from .qa_segmenter import QAUnit, segment as qa_segment


# --- event taxonomy ---------------------------------------------------------

# Examination header: "DIRECT EXAMINATION", "CROSS-EXAMINATION", "FURTHER
# REDIRECT EXAMINATION", "VOIR DIRE EXAMINATION (of the witness)".
_EXAM_RE = re.compile(
    r"^\s*(?:FURTHER\s+)?(DIRECT|CROSS|REDIRECT|RECROSS|VOIR\s+DIRE)"
    r"[\s\-]+EXAMINATION\b", re.I)
_EXAM_KIND = {
    "DIRECT": "direct", "CROSS": "cross", "REDIRECT": "redirect",
    "RECROSS": "recross", "VOIR DIRE": "voir_dire",
}

# Witness sworn / identification: "JOHN A. SMITH, having been first duly sworn"
# or "... was duly sworn, testified as follows".
_SWORN_RE = re.compile(
    r"^\s*([A-Z][A-Z][A-Z .,'\-]*?),?\s+(?:having been|was|having first been)"
    r"\b.*\bsworn\b", re.I)

# Standalone phase headers (a line that IS the header). Ordered; first match wins.
_PHASE_RE = [
    (re.compile(r"^\s*VOIR\s+DIRE(\s+EXAMINATION)?\s*$", re.I), "voir_dire"),
    (re.compile(r"^\s*(OPENING\s+STATEMENT|OPENING\s+STATEMENTS|"
                r"OPENING\s+ARGUMENT)\b", re.I), "opening"),
    (re.compile(r"^\s*(CLOSING\s+ARGUMENT|CLOSING\s+ARGUMENTS|"
                r"CLOSING\s+STATEMENT|FINAL\s+ARGUMENT|SUMMATION)\b", re.I),
     "closing"),
    (re.compile(r"^\s*(THE\s+COURT'?S\s+CHARGE|CHARGE\s+OF\s+THE\s+COURT|"
                r"COURT'?S\s+CHARGE|JURY\s+INSTRUCTIONS|JURY\s+CHARGE)\b", re.I),
     "charge"),
    (re.compile(r"^\s*(THE\s+VERDICT|VERDICT\s+OF\s+THE\s+JURY|VERDICT)\s*$",
                re.I), "verdict"),
    (re.compile(r"^\s*(BENCH\s+CONFERENCE|SIDE-?BAR(\s+CONFERENCE)?)\b", re.I),
     "bench_conference"),
    (re.compile(r"^\s*PROCEEDINGS\s*$", re.I), "proceedings"),
]

# Objection / ruling markers (used to tag colloquy units inside examinations).
_OBJ_RE = re.compile(r"\bobjection\b", re.I)
_RULING_RE = re.compile(r"\b(sustained|overruled|granted|denied|so\s+ordered|"
                        r"i'?ll\s+allow\s+it|carried\b)\b", re.I)


def _clean_name(raw: str) -> str:
    return re.sub(r"\s+", " ", raw).strip(" ,.")


def _colloquy_event(text: str):
    """Tag an examination colloquy unit as objection/ruling when the reporter's
    grammar makes it deterministic. Precise objection<->ruling pairing is U3."""
    has_obj = bool(_OBJ_RE.search(text))
    has_rule = bool(_RULING_RE.search(text))
    if has_obj:
        return "objection"
    if has_rule:
        return "ruling"
    return "colloquy"


def _block(event_type, witness, exam):
    return {"event_type": event_type, "witness": witness, "exam": exam,
            "lines": []}


def segment(lines: list):
    """lines: list of dicts with keys page, line, text (page:line order).
    Returns (units, line_roles): units is list[QAUnit] (each with .event_type),
    line_roles is list[(page, line, role)] for transcript_lines.qa_role."""
    # --- Pass 1: split into event blocks ----------------------------------
    blocks = [_block("proceedings", "", False)]
    witness = ""

    for ln in lines:
        text = ln.get("text", "") or ""

        m = _EXAM_RE.match(text)
        if m:
            kind = _EXAM_KIND[re.sub(r"\s+", " ", m.group(1).upper())]
            blocks.append(_block(kind, witness, True))
            # the header line itself is structural, not testimony
            blocks[-1]["lines"].append({**ln, "_role": "EXAM_HEADER",
                                        "_skip_body": True})
            continue

        sw = _SWORN_RE.match(text)
        if sw:
            witness = _clean_name(sw.group(1))
            if blocks[-1]["exam"] and not blocks[-1]["witness"]:
                blocks[-1]["witness"] = witness
            blocks[-1]["lines"].append(ln)
            continue

        phase = None
        for rx, etype in _PHASE_RE:
            if rx.match(text):
                phase = etype
                break
        if phase is not None:
            # narrative events (opening/closing/charge/verdict/...) are not
            # witness testimony -> no witness attribution.
            blocks.append(_block(phase, "", False))
            blocks[-1]["lines"].append(ln)
            continue

        blocks[-1]["lines"].append(ln)

    # --- Pass 2: build units ----------------------------------------------
    units: list = []
    line_roles: list = []
    seq = 0

    for b in blocks:
        body = [l for l in b["lines"] if not l.get("_skip_body")]
        # emit a record role for skipped structural header lines
        for l in b["lines"]:
            if l.get("_skip_body"):
                line_roles.append((l["page"], l["line"], "EXAM_HEADER"))
        if not body:
            continue

        if b["exam"]:
            sub_units, sub_roles = qa_segment(body)
            for u in sub_units:
                seq += 1
                u.seq = seq
                # the sworn witness of the block owns its examination testimony
                # (per-witness boundaries); fall back to the segmenter's label.
                if b["witness"]:
                    u.witness = b["witness"]
                u.event_type = (_colloquy_event(u.answer_text)
                                if u.is_colloquy else b["event_type"])
                units.append(u)
            line_roles.extend(sub_roles)
        else:
            # narrative event -> one span unit (nothing lost from the index)
            seq += 1
            first, last = body[0], body[-1]
            span_text = " ".join((l.get("text", "") or "").strip()
                                 for l in body).strip()
            u = QAUnit(
                seq=seq, examiner="", witness=b["witness"],
                q_start_page=first["page"], q_start_line=first["line"],
                a_end_page=last["page"], a_end_line=last["line"],
                question_text="", answer_text=span_text, is_colloquy=True)
            u.event_type = b["event_type"]
            units.append(u)
            role = b["event_type"].upper()
            for l in body:
                line_roles.append((l["page"], l["line"], role))

    return units, line_roles
