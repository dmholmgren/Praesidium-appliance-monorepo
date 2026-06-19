"""Deposition transcript parsers — file -> canonical text + page:line addressing.

§0 invariant: the parser is the ONLY writer of canonical text. It emits an
immutable `canonical_text` (the joined testimony body) plus a `transcript_lines`
table of (page, line) -> (char_start, char_end) spans into that text. Everything
downstream (segmentation, embeddings, designations, clips) indexes into these
offsets and never rewrites the text.

Format support (U1):
  - ascii  : LiveNote / E-Transcript ASCII export (page-numbered, 1..25 lines).
             The litigation lingua franca; the primary native parser.
  - pdf    : text-layer deposition PDF, via PyMuPDF (fitz). Full-page e-transcript
             layout (one printed line per transcript line). Condensed/4-up PDFs
             are detected as low-yield and flagged for a full-page export.
  - native binaries (ptx/cms/lef/sbf/dvt/mdb): proprietary/encrypted vendor
             containers (RealLegal E-Transcript .ptx is AES-encrypted — verified
             against firm samples at build time). Not natively decodable; we
             create the transcript row and raise NotConvertible so the alert/UI
             prompts for an ASCII or PDF export. The same file re-ingests cleanly
             once exported — no rework.

Pagination is driven by the printed line numbers, not by page furniture: a page
boundary is where the line number resets toward 1 (with the printed page-number
header used to label the page when present, and form-feeds honoured as hard
boundaries). This is robust across vendors whose page headers differ, and it
guarantees unique (page, line) keys for the transcript_lines primary key.

NOTE: validated here against synthetic + the formats the firm actually receives;
real vendor ASCII exports vary in header furniture — calibrate `_PAGEHDR_RE` /
`_PAGENO_RE` against a live export if a specific reporter's layout under-parses.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# --- format detection -------------------------------------------------------

BINARY_FORMATS = {
    ".ptx": "ptx", ".cms": "cms", ".lef": "lef", ".sbf": "sbf",
    ".dvt": "dvt", ".mdb": "mdb", ".trm": "trm",
}
ASCII_FORMATS = {".txt": "ascii", ".asc": "ascii", ".lst": "ascii", ".prn": "ascii"}

# Max plausible printed line number on a page (25-line pages are standard; some
# reporters run to 28). Anything above this is body text, not a line number.
MAX_LINE_NO = 28
# A page reset lands at a small line number (1, sometimes 2-3 after a header).
RESET_CEIL = 3


class NotConvertible(Exception):
    """Raised for proprietary/encrypted transcript containers we cannot parse
    natively. Carries the detected format so the ledger error_detail can prompt
    for the right export."""

    def __init__(self, source_format: str, msg: str):
        self.source_format = source_format
        super().__init__(msg)


def detect_format(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in ASCII_FORMATS:
        return "ascii"
    if ext in BINARY_FORMATS:
        return BINARY_FORMATS[ext]
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return "unknown"
    if not head:
        return "unknown"
    printable = sum(1 for b in head if 9 <= b <= 13 or 32 <= b <= 126)
    return "ascii" if printable / len(head) > 0.85 else "unknown"


# --- result model -----------------------------------------------------------

@dataclass
class ParsedLine:
    page: int
    line: int
    text: str
    char_start: int = 0
    char_end: int = 0


@dataclass
class ParsedTranscript:
    source_format: str
    canonical_text: str
    lines: list = field(default_factory=list)   # list[ParsedLine]
    page_first: int = 0
    page_last: int = 0

    @property
    def line_count(self) -> int:
        return len(self.lines)


def _dedupe_keys(raw_lines: list) -> list:
    """Guarantee unique (page,line) for the transcript_lines primary key. Fast
    path when already unique; otherwise re-derive pages from line-number resets
    and bump any residual collisions. A parser glitch (e.g. a mis-read page
    number) thus degrades gracefully instead of failing the whole ingest."""
    keys = [(ln.page, ln.line) for ln in raw_lines]
    if len(set(keys)) == len(keys):
        return raw_lines
    logger.warning("transcript parse: %d duplicate page:line key(s) — re-paginating",
                   len(keys) - len(set(keys)))
    page, prev_n = 0, None
    for ln in raw_lines:
        if prev_n is None or ln.line <= prev_n:
            page += 1
        ln.page, prev_n = page, ln.line
    used = set()
    for ln in raw_lines:
        while (ln.page, ln.line) in used:
            ln.line += 1
        used.add((ln.page, ln.line))
    return raw_lines


def _assemble(source_format: str, raw_lines: list) -> ParsedTranscript:
    """Join the line bodies with '\n' and stamp char_start/char_end into the
    joined canonical text."""
    raw_lines = _dedupe_keys(raw_lines)
    parts = []
    offset = 0
    for ln in raw_lines:
        ln.char_start = offset
        ln.char_end = offset + len(ln.text)
        parts.append(ln.text)
        offset += len(ln.text) + 1   # +1 for the joining newline
    canonical = "\n".join(parts)
    pages = [ln.page for ln in raw_lines] or [0]
    return ParsedTranscript(
        source_format=source_format, canonical_text=canonical, lines=raw_lines,
        page_first=min(pages), page_last=max(pages))


# --- line classifiers (shared by ascii + pdf) -------------------------------

# A numbered transcript line: optional leading spaces, a 1-2 digit line number,
# whitespace, then a non-empty body.
_LINE_RE = re.compile(r"^[ \t]*(\d{1,2})[ \t]+(.*\S.*)$")
# A bare page-number line (header/footer furniture).
_PAGENO_RE = re.compile(r"^[ \t]*(\d{1,4})[ \t]*$")
# An explicit "PAGE 12" / "Page 12" header.
_PAGEHDR_RE = re.compile(r"^[ \t]*(?:PAGE|Page)\s+(\d{1,4})\b")


def _plausible_pageno(n, current):
    """Is a bare-number/header a real printed PAGE number? Reject 4-digit years
    (a wrapped date line like '...January 14, 2026' is NOT a page) and absurd
    jumps (exhibit/Bates stamps) — both otherwise corrupt the (page,line)
    addressing and can collide the transcript_lines primary key."""
    if 1900 <= n <= 2099:
        return False
    if current and n > current + 100:
        return False
    return 1 <= n <= 9999


class _Paginator:
    """Turns a stream of physical text lines into (page, line, body) tuples,
    deriving page boundaries from line-number resets and printed page headers."""

    def __init__(self):
        self.page = 0
        self.prev_n = 0
        self.started = False
        self.pending_pageno = None   # printed page number seen, applied at reset

    def feed_lines(self, physical_lines, hard_page_break=False):
        if hard_page_break and self.started:
            self.page += 1
            self.prev_n = 0
            self.pending_pageno = None
        out = []
        for raw in physical_lines:
            mh = _PAGEHDR_RE.match(raw)
            if mh:
                v = int(mh.group(1))
                if _plausible_pageno(v, self.page):
                    self.pending_pageno = v
                continue
            mp = _PAGENO_RE.match(raw)
            if mp:
                v = int(mp.group(1))
                if _plausible_pageno(v, self.page):
                    self.pending_pageno = v
                continue
            m = _LINE_RE.match(raw)
            if not m:
                continue
            n = int(m.group(1))
            if n > MAX_LINE_NO:
                continue   # body text that merely starts with a big number
            if not self.started:
                self.page = (self.pending_pageno
                             if self.pending_pageno is not None else 1)
                self.pending_pageno = None
                self.started = True
            elif n <= self.prev_n:
                if n > RESET_CEIL and self.pending_pageno is None:
                    # not a page reset and no header -> body line like "25 years"
                    continue
                self.page = (self.pending_pageno
                             if self.pending_pageno is not None else self.page + 1)
                self.pending_pageno = None
            out.append((self.page, n, m.group(2).rstrip()))
            self.prev_n = n
        return out


# --- ascii parser -----------------------------------------------------------

def parse_ascii(path: str, source_format: str = "ascii") -> ParsedTranscript:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    pag = _Paginator()
    raw_lines: list = []
    if "\f" in text:
        for chunk in text.split("\f"):
            for page, n, body in pag.feed_lines(chunk.splitlines(),
                                                hard_page_break=True):
                raw_lines.append(ParsedLine(page=page, line=n, text=body))
    else:
        for page, n, body in pag.feed_lines(text.splitlines()):
            raw_lines.append(ParsedLine(page=page, line=n, text=body))

    if not raw_lines:
        raise NotConvertible(
            source_format,
            "ascii parse yielded no numbered transcript lines (unrecognised "
            "layout — verify it is a page:line export, not condensed/summary)")
    return _assemble(source_format, raw_lines)


# --- pdf parser -------------------------------------------------------------

# Some e-transcript PDFs (Esquire/RealLegal) render the inter-token spacing as
# MIDDLE DOT leaders ("·1·  ·A·  · Yes."). Fold those filler glyphs back to real
# spaces so the line-number regexes match (otherwise only a few lines parse).
_LEADER_TRANS = {ord(c): " " for c in "·•‧⋅∙ "}


def parse_pdf_text(path: str, source_format: str = "pdf") -> ParsedTranscript:
    import fitz  # PyMuPDF (same dependency the ediscovery geometry lane uses)

    raw_lines: list = []
    pag = _Paginator()
    with fitz.open(path) as doc:
        if doc.page_count == 0:
            raise NotConvertible("pdf", "empty pdf")
        for pidx in range(doc.page_count):
            page = doc.load_page(pidx)
            text = (page.get_text("text") or "").translate(_LEADER_TRANS)
            # each PDF page is a hard physical page boundary
            for pg, n, body in pag.feed_lines(text.splitlines(),
                                              hard_page_break=True):
                raw_lines.append(ParsedLine(page=pg, line=n, text=body))

    if not raw_lines:
        raise NotConvertible(
            "pdf",
            "no text layer / no numbered lines (scanned image PDF, or a "
            "condensed/4-up layout). Re-export as a full-page text PDF, or "
            "route through OCR.")
    return _assemble(source_format, raw_lines)


# --- geometry-driven pdf parser (real reporter's records) -------------------

# Real RR / trial PDFs put the line number in a narrow LEFT column and the body to
# its right; get_text('text') serializes them out of order. We reconstruct (page,
# line) from the positioned word tokens (normalized 0..1 bboxes) -- the same
# geometry that drives DMS sectioning -- keying off column x + row y, robust across
# reporters' header furniture.
_GEO_LEFT_BAND = 0.18    # the line-number column lives left of this (norm x)
_GEO_TOP_BAND = 0.075    # page-number header band (norm y)
_GEO_ROW_TOL = 0.007     # y tolerance to group words into one transcript line


def _cluster_rows(words, tol):
    rows = []
    for w in sorted(words, key=lambda t: (t.y, t.x)):
        if rows and abs(w.y - rows[-1][0]) <= tol:
            rows[-1][1].append(w)
        else:
            rows.append((w.y, [w]))
    return rows


def parse_pdf_geometry(path: str, source_format: str = "pdf"):
    """Reconstruct page:line from positioned tokens. Returns a ParsedTranscript, or
    None if the doc has text but no page:line column layout (caller falls back).
    Raises NotConvertible only for a scanned PDF (no text layer -> OCR)."""
    from modules.ediscovery.services.geometry_extraction import extract_pdf_with_geometry
    res = extract_pdf_with_geometry(path)
    if res is None or not getattr(res, "has_text_layer", False):
        raise NotConvertible("pdf", "no text layer (scanned) -- route to OCR")

    words_by_page = {}
    for t in res.tokens:
        if getattr(t, "unit", "word") != "word":
            continue
        words_by_page.setdefault(t.page_number, []).append(t)

    raw_lines = []
    seq_page = 0
    for pno in sorted(words_by_page):
        words = words_by_page[pno]
        hdr = [w for w in words if w.y <= _GEO_TOP_BAND
               and (w.text or "").strip().isdigit()
               and len((w.text or "").strip()) <= 4]
        printed = int(sorted(hdr, key=lambda w: -w.x)[0].text.strip()) if hdr else None
        body = [w for w in words if w.y > _GEO_TOP_BAND]
        page_lines = []
        for _y, rw in _cluster_rows(body, _GEO_ROW_TOL):
            rw.sort(key=lambda w: w.x)
            # drop leading MIDDLE-DOT leader tokens so the real line number is found
            real = [w for w in rw if (w.text or "").strip().strip("·•‧⋅∙")]
            if not real:
                continue
            lead = real[0]
            lt = (lead.text or "").strip()
            if lead.x < _GEO_LEFT_BAND and lt.isdigit() and 1 <= int(lt) <= 99:
                text = " ".join((w.text or "") for w in real[1:]).strip().translate(_LEADER_TRANS)
                if text:
                    page_lines.append((int(lt), text))
        if not page_lines:
            continue
        seq_page += 1
        page = printed if printed is not None else seq_page
        for (line_no, text) in page_lines:
            raw_lines.append(ParsedLine(page=page, line=line_no, text=text))

    if len(raw_lines) < 5:
        return None
    return _assemble(source_format, raw_lines)


def parse_pdf(path: str, source_format: str = "pdf") -> ParsedTranscript:
    """Geometry-first PDF transcript parse (handles the left-column line-number
    layout real reporters use); fall back to the text-layer paginator for simple
    full-page exports."""
    try:
        geo = parse_pdf_geometry(path, source_format)
    except NotConvertible:
        raise
    except Exception:
        logger.exception("geometry pdf parse failed; trying text paginator")
        geo = None
    try:
        txt = parse_pdf_text(path, source_format)
    except NotConvertible:
        txt = None
    except Exception:
        logger.exception("text pdf parse failed")
        txt = None
    cands = [c for c in (geo, txt) if c is not None]
    if not cands:
        raise NotConvertible("pdf", "no parsable transcript lines")
    # Keep the richer parse: geometry shines on real reporter's-record column
    # layouts, but collapses on some e-transcript portfolios where the text
    # paginator recovers the full transcript — so pick whichever yields more
    # lines (geometry wins ties for its better page:line fidelity).
    best = max(cands, key=lambda c: c.line_count)
    if geo is not None and txt is not None and best is txt and txt.line_count > geo.line_count * 2:
        logger.info("pdf parse: text paginator (%d lines) beat geometry (%d) — using text",
                    txt.line_count, geo.line_count)
    return best


# --- dispatch ---------------------------------------------------------------

def parse_transcript(path: str, source_format: str = "") -> ParsedTranscript:
    """Top-level entry. Detects the format if not given and dispatches to the
    right parser. Raises NotConvertible for proprietary containers."""
    fmt = source_format or detect_format(path)
    if fmt == "pdf":
        return parse_pdf(path, fmt)
    if fmt == "ascii":
        return parse_ascii(path, fmt)
    if fmt in BINARY_FORMATS.values():
        raise NotConvertible(
            fmt,
            "proprietary transcript container (%s) — encrypted/binary vendor "
            "format; export to ASCII (page:line) or a full-page PDF and "
            "re-ingest." % fmt)
    raise NotConvertible(fmt or "unknown",
                         "unrecognised transcript format: %s" % (fmt or "?"))
