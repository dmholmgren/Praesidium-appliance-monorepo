# -*- coding: utf-8 -*-
"""
modules/ediscovery/services/cell_extraction.py

Spreadsheet geometry: the 'cell' kind (Document Geometry Plumbing Contract
sect. 2, table doc_layout_cells from migration 0052 -- previously a writer-less
table). Spreadsheets do NOT go through the LibreOffice render lane any more:
rendering an xlsx to PDF destroys the sheet/row/col structure. Instead this
module extracts cells natively and builds the document's sect. 0 canonical as a
deterministic serialization of those cells.

Canonical serialization (deterministic):

    === Sheet: <name> ===\n
    cell\tcell\tcell\n          (one line per row; trailing empty cells trimmed)
    \n                          (single blank line between sheets)

Cell text is sanitized (tab/CR/LF -> space) BEFORE spans are computed, so
    canonical_text[char_start:char_end] == cell.text
holds exactly for every persisted cell (the sect. 0 invariant). The blank line
between sheets means the canonical-only segmenter fallback naturally cuts one
section per sheet without spreadsheet-specific code.

Engines: openpyxl (xlsx/xlsm, read_only + data_only), stdlib csv (csv/tsv).
Legacy .xls is NOT handled here (openpyxl cannot read it) -- it stays on the
render lane.
"""
from __future__ import annotations

import csv
import datetime
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

logger = logging.getLogger("praesidium.cell_extraction")

#: extensions routed to the cells lane (checked by ledger_dag before _route)
SHEET_EXTS = {".xlsx", ".xlsm", ".csv", ".tsv"}

#: hard cap -- a pathological workbook must fail loudly, not OOM a worker
MAX_CELLS = 500_000


@dataclass
class CellToken:
    """One non-empty cell, positioned by sheet/row/col AND by char span into the
    canonical text. The span is the bridge every overlay (redaction, search,
    spine-highlight) resolves through -- the cell analogue of LayoutToken."""
    sheet: str
    row_idx: int        # 1-based
    col_idx: int        # 1-based
    a1_range: str       # e.g. "B7"
    char_start: int
    char_end: int
    text: str


@dataclass
class CellExtractionResult:
    canonical_text: str
    cells: List[CellToken] = field(default_factory=list)
    sheet_count: int = 0
    cell_count: int = 0
    text_source: str = "cells"
    has_text_layer: bool = True


# ---------------------------------------------------------------------------
# deterministic cell formatting
# ---------------------------------------------------------------------------

def _fmt(v) -> str:
    """Deterministic text for a cell value. Sanitizes the delimiters the
    serialization uses (tab/CR/LF) so spans stay exact."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return repr(v)
    if isinstance(v, datetime.datetime):
        if v.time() == datetime.time(0, 0):
            return v.date().isoformat()
        return v.isoformat(sep=" ")
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, datetime.time):
        return v.isoformat()
    s = str(v)
    return (s.replace("\t", " ").replace("\r\n", " ")
             .replace("\n", " ").replace("\r", " "))


def _col_letter(n: int) -> str:
    """1-based column index -> A1 letters (1->A, 27->AA)."""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


# ---------------------------------------------------------------------------
# readers (yield (sheet_name, rows-of-formatted-strings))
# ---------------------------------------------------------------------------

def _sheets_from_xlsx(path: str):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                rows.append([_fmt(v) for v in row])
            yield ws.title, rows
    finally:
        wb.close()


def _sheets_from_csv(path: str, delimiter: str):
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            data = fh.read()
    except UnicodeDecodeError:
        with open(path, "r", encoding="latin-1", newline="") as fh:
            data = fh.read()
    rows = [[_fmt(v) for v in row]
            for row in csv.reader(data.splitlines(), delimiter=delimiter)]
    yield Path(path).stem, rows


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

def extract_cells_with_geometry(path: str):
    """Extract a spreadsheet into (canonical_text, cells) with exact spans.
    Returns None for unsupported extensions or workbooks with no textual cells
    (caller treats that as empty, same as an empty PDF)."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        sheets = _sheets_from_xlsx(str(p))
    elif ext == ".csv":
        sheets = _sheets_from_csv(str(p), ",")
    elif ext == ".tsv":
        sheets = _sheets_from_csv(str(p), "\t")
    else:
        return None

    parts: List[str] = []
    cells: List[CellToken] = []
    pos = 0
    sheet_count = 0

    def emit(s: str):
        nonlocal pos
        parts.append(s)
        pos += len(s)

    for sheet_name, rows in sheets:
        if sheet_count:
            emit("\n")
        sheet_count += 1
        sname = _fmt(sheet_name) or ("Sheet%d" % sheet_count)
        emit("=== Sheet: %s ===\n" % sname)
        for r_i, row in enumerate(rows, start=1):
            last = len(row)
            while last and not row[last - 1]:
                last -= 1
            if last == 0:
                emit("\n")
                continue
            for c_i in range(1, last + 1):
                if c_i > 1:
                    emit("\t")
                txt = row[c_i - 1]
                if txt:
                    start = pos
                    emit(txt)
                    cells.append(CellToken(
                        sheet=sname, row_idx=r_i, col_idx=c_i,
                        a1_range="%s%d" % (_col_letter(c_i), r_i),
                        char_start=start, char_end=pos, text=txt))
                    if len(cells) > MAX_CELLS:
                        raise ValueError(
                            "cell cap exceeded (%d) for %s" % (MAX_CELLS, path))
            emit("\n")

    if not cells:
        return None
    canonical = "".join(parts)
    return CellExtractionResult(
        canonical_text=canonical, cells=cells,
        sheet_count=sheet_count, cell_count=len(cells))


def verify_cell_offsets(res: CellExtractionResult):
    """sect. 0 self-check: every cell's span must reproduce its stored text from
    the canonical. Returns (ok_count, bad_count) -- mirrors verify_offsets."""
    ok = bad = 0
    canon = res.canonical_text
    for c in res.cells:
        if canon[c.char_start:c.char_end] == c.text:
            ok += 1
        else:
            bad += 1
    return ok, bad
