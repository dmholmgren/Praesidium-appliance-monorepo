"""Structural Q&A segmentation — transcript_lines -> transcript_qa_units.

Layer-1 primitive. Walks the addressing-layer lines in page:line order, tags
each with a structural role (examiner question / witness answer / examination
change / colloquy / parenthetical), and groups them into Q&A exchange units —
the semantic chunk that gets embedded (U2) and that element-driven suggestion
ranks (U10).

Structural-first (no LLM): roles come from the stable stenographic grammar of a
transcript — "Q." / "A." stems, "BY MR. NAME:" examination headers, "SPEAKER:"
colloquy, and "(...)" parentheticals. The segmenter also writes the resolved
qa_role back onto transcript_lines (metadata on the addressing layer; it never
touches canonical_text — §0).

A unit spans from its question's first line to its answer's last line. Runs of
colloquy / objections / parentheticals between exchanges become is_colloquy units
so nothing in the transcript is lost from the index.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# Q. / A. stems (with or without the period; some vendors use "Q" alone).
_Q_RE = re.compile(r"^\s*Q[\.\)]?\s+(.*)$|^\s*Q\s*$")
_A_RE = re.compile(r"^\s*A[\.\)]?\s+(.*)$|^\s*A\s*$")
# "BY MR. SMITH:" / "BY MS. JONES:" — examination header (sets the examiner).
_BY_RE = re.compile(r"^\s*BY\s+(MR\.|MS\.|MRS\.|DR\.)?\s*([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)*)\s*:\s*(.*)$")
# "THE WITNESS:", "THE COURT:", "MR. SMITH:" — colloquy speaker label.
_SPEAKER_RE = re.compile(r"^\s*((?:THE\s+[A-Z]+)|(?:MR\.|MS\.|MRS\.|DR\.)\s+[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)*)\s*:\s*(.*)$")
# A parenthetical stage direction "(Whereupon, ...)".
_PAREN_RE = re.compile(r"^\s*\(.*\)?\s*$")
# "THE WITNESS:" answers map to the witness too.
_WITNESS_LABEL = "THE WITNESS"


@dataclass
class QAUnit:
    seq: int
    examiner: str = ""
    witness: str = ""
    q_start_page: int = 0
    q_start_line: int = 0
    a_end_page: int = 0
    a_end_line: int = 0
    char_start: int = 0
    char_end: int = 0
    question_text: str = ""
    answer_text: str = ""
    is_colloquy: bool = False
    line_roles: list = field(default_factory=list)  # [(transcript_line_key, role)]


def _classify(text: str):
    """Return (role, payload). role in {q, a, by, speaker, paren, cont}."""
    if _PAREN_RE.match(text):
        return "paren", text.strip()
    m = _BY_RE.match(text)
    if m:
        name = ((m.group(1) or "") + " " + (m.group(2) or "")).strip()
        return "by", (name, (m.group(3) or "").strip())
    if _Q_RE.match(text):
        m2 = _Q_RE.match(text)
        return "q", (m2.group(1) or "").strip()
    if _A_RE.match(text):
        m2 = _A_RE.match(text)
        return "a", (m2.group(1) or "").strip()
    m = _SPEAKER_RE.match(text)
    if m:
        return "speaker", (m.group(1).strip(), (m.group(2) or "").strip())
    return "cont", text.strip()


def segment(lines: list) -> list:
    """lines: list of dicts with keys page, line, text (page:line order).
    Returns (units, line_roles) where units is list[QAUnit] and line_roles is a
    list of (page, line, role) for writing back to transcript_lines.qa_role."""
    units: list = []
    line_roles: list = []
    seq = 0
    examiner = ""
    witness = ""
    cur = None          # current QAUnit being built
    mode = None         # 'q' | 'a' | 'colloquy'

    def flush():
        nonlocal cur
        if cur is not None:
            cur.question_text = cur.question_text.strip()
            cur.answer_text = cur.answer_text.strip()
            units.append(cur)
            cur = None

    def new_unit(page, line, colloquy=False):
        nonlocal cur, seq
        flush()
        seq += 1
        cur = QAUnit(seq=seq, examiner=examiner, witness=witness,
                     q_start_page=page, q_start_line=line,
                     a_end_page=page, a_end_line=line, is_colloquy=colloquy)
        return cur

    for ln in lines:
        page, line, text = ln["page"], ln["line"], ln.get("text", "")
        role, payload = _classify(text)

        if role == "by":
            name, rest = payload
            examiner = name or examiner
            line_roles.append((page, line, "BY"))
            # a "BY MR. X:" line often introduces the next question
            if cur and mode == "a":
                flush()
            mode = None
            if rest:
                new_unit(page, line)
                cur.question_text = rest
                mode = "q"
            continue

        if role == "q":
            new_unit(page, line)
            cur.question_text = payload
            cur.a_end_page, cur.a_end_line = page, line
            mode = "q"
            line_roles.append((page, line, "Q"))
            continue

        if role == "a":
            if cur is None or cur.is_colloquy:
                new_unit(page, line)
            cur.answer_text = payload
            cur.a_end_page, cur.a_end_line = page, line
            if not witness:
                witness = _WITNESS_LABEL
                cur.witness = witness
            mode = "a"
            line_roles.append((page, line, "A"))
            continue

        if role == "speaker":
            spk, rest = payload
            if spk == _WITNESS_LABEL:
                # witness testimony: a real answer. Break out of any colloquy
                # (objection) run so it is not buried as colloquy text.
                if cur is None or cur.is_colloquy:
                    new_unit(page, line)
                cur.answer_text = (cur.answer_text + " " + rest).strip()
                cur.a_end_page, cur.a_end_line = page, line
                mode = "a"
                line_roles.append((page, line, "A"))
            else:
                if mode != "colloquy" or cur is None:
                    new_unit(page, line, colloquy=True)
                    mode = "colloquy"
                cur.answer_text = (cur.answer_text + " " + spk + ": " + rest).strip()
                cur.a_end_page, cur.a_end_line = page, line
                line_roles.append((page, line, "COLLOQUY"))
            continue

        if role == "paren":
            if mode != "colloquy" or cur is None:
                new_unit(page, line, colloquy=True)
                mode = "colloquy"
            cur.answer_text = (cur.answer_text + " " + payload).strip()
            cur.a_end_page, cur.a_end_line = page, line
            line_roles.append((page, line, "COLLOQUY"))
            continue

        # continuation of whatever we're in
        if cur is None:
            new_unit(page, line, colloquy=True)
            mode = "colloquy"
        if mode == "q":
            cur.question_text = (cur.question_text + " " + payload).strip()
        else:
            cur.answer_text = (cur.answer_text + " " + payload).strip()
        cur.a_end_page, cur.a_end_line = page, line
        role_tag = {"q": "Q", "a": "A"}.get(mode, "COLLOQUY")
        line_roles.append((page, line, role_tag))

    flush()
    return units, line_roles
