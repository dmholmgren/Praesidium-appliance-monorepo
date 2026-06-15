"""
modules/ediscovery/guided_ingest/automap.py

Load-file field auto-mapping for the guided confirm screen. Finds the load file
(.dat/.opt/.dii/.lfp/.csv) under a proposed unit's source path (dir, zip, or
file), sniffs its column headers (reusing production_import._sniff_columns), and
proposes a deterministic header -> target-field map for human confirmation.

Split/spanned archives (Relativity name.001.zip / WinZip name.z01..name.zip)
can't be column-sniffed without full reassembly, so they're reported as a
"deferred" load file: the panel confirms the load file is present and that the
field map is applied automatically at ingest, instead of showing blank.
"""
import os
import zipfile

from modules.ediscovery.production_import import _detect_format, _sniff_columns
from modules.ediscovery.jobs.split_zip_reassemble import (
    detect_split_zip, SPLIT_ZIP_PATTERN, WINZIP_SEG_PATTERN,
)

LOAD_EXTS = (".dat", ".opt", ".dii", ".lfp", ".csv")

TARGET_FIELDS = [
    {"key": "bates_begin", "label": "Bates Begin", "required": True},
    {"key": "bates_end", "label": "Bates End", "required": False},
    {"key": "doc_date", "label": "Document Date", "required": False},
    {"key": "author", "label": "Author / From", "required": False},
    {"key": "recipients", "label": "Recipients / To", "required": False},
    {"key": "subject", "label": "Subject", "required": False},
    {"key": "custodian", "label": "Custodian", "required": False},
    {"key": "doc_type", "label": "Document Type", "required": False},
    {"key": "file_path", "label": "Native File Path", "required": False},
    {"key": "text_path", "label": "Extracted Text Path", "required": False},
    {"key": "confidentiality", "label": "Confidentiality / Privilege", "required": False},
    {"key": "md5_hash", "label": "MD5 Hash", "required": False},
]

# Ordered keyword hints per field; first unused column whose normalized header
# contains a keyword wins. Order matters (begin before generic 'bates').
_HINTS = {
    "bates_begin": ["begin bates", "beg bates", "begbates", "bates begin", "bates beg",
                    "beginning bates", "prod beg", "prodbeg", "start bates", "begdoc",
                    "beg doc", "begin production", "control number"],
    "bates_end": ["end bates", "endbates", "bates end", "ending bates", "prod end",
                  "prodend", "enddoc", "end doc", "end production"],
    "doc_date": ["date sent", "sent date", "master date", "doc date", "document date",
                 "date created", "sort date", "date"],
    "author": ["author", "email from", "from", "sender"],
    "recipients": ["recipient", "email to", "to"],
    "subject": ["subject", "email subject", "title"],
    "custodian": ["custodian", "source party", "source"],
    "doc_type": ["doc type", "document type", "file type", "filetype",
                 "file extension", "extension", "application", "record type"],
    "file_path": ["native path", "native link", "nativelink", "native file",
                  "native", "file path", "filepath", "item path", "doc link", "link"],
    "text_path": ["extracted text path", "text path", "extracted text", "ocr path",
                  "text link", "textlink", "full text", "fulltext", "text"],
    "confidentiality": ["confidential", "privilege", "designation", "conf"],
    "md5_hash": ["md5 hash", "md5", "hash"],
}


def _norm(s):
    return " ".join("".join(c if c.isalnum() else " " for c in s.lower()).split())


def suggest_map(columns):
    """Deterministic header -> target field guess. Each column used at most once."""
    norm = [(_norm(c), c) for c in columns]
    used, m = set(), {}
    for f in TARGET_FIELDS:
        chosen = None
        for kw in _HINTS.get(f["key"], []):
            for nc, orig in norm:
                if orig in used:
                    continue
                if kw in nc:
                    chosen = orig
                    break
            if chosen:
                break
        if chosen:
            m[f["key"]] = chosen
            used.add(chosen)
    return m


def _load_in_zip(zip_path):
    """(name, first 8k bytes) for the top-priority load file inside a single,
    self-contained zip, or (None, None)."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = [n for n in z.namelist()
                     if (not n.endswith("/")) and n.lower().endswith(LOAD_EXTS)]
            names.sort(key=lambda n: LOAD_EXTS.index(os.path.splitext(n.lower())[1]))
            if names:
                with z.open(names[0]) as fh:
                    return os.path.basename(names[0]), fh.read(8192)
    except Exception:
        return None, None
    return None, None


def find_load_file(path):
    """Return (filename, first_8k_bytes) for the load file at/under path.

    Handles: a load file directly, a single self-contained .zip (load file
    inside), and a directory (loose load file, else a single self-contained
    .zip sitting in it). Split/spanned sets are not handled here — see
    _split_set_info / automap_for_path.
    """
    p = path or ""
    if p.lower().endswith(".zip") and os.path.isfile(p):
        return _load_in_zip(p)
    if os.path.isfile(p) and p.lower().endswith(LOAD_EXTS):
        with open(p, "rb") as fh:
            return os.path.basename(p), fh.read(8192)
    if os.path.isdir(p):
        best = None
        zips = []
        for dp, _d, fns in os.walk(p):
            for fn in fns:
                e = os.path.splitext(fn.lower())[1]
                if e in LOAD_EXTS:
                    rank = LOAD_EXTS.index(e)
                    if best is None or rank < best[0]:
                        best = (rank, os.path.join(dp, fn))
                elif fn.lower().endswith(".zip"):
                    zips.append(os.path.join(dp, fn))
        if best:
            with open(best[1], "rb") as fh:
                return os.path.basename(best[1]), fh.read(8192)
        # No loose load file — if exactly one self-contained zip lives here and
        # it isn't part of a split set, peek inside it.
        if len(zips) == 1 and not _is_split_member(os.path.basename(zips[0])):
            return _load_in_zip(zips[0])
    return None, None


def _is_split_member(name):
    return bool(SPLIT_ZIP_PATTERN.match(name) or WINZIP_SEG_PATTERN.match(name))


def _family_base(name):
    m = SPLIT_ZIP_PATTERN.match(name)
    if m:
        return m.group(1)
    m = WINZIP_SEG_PATTERN.match(name)
    if m:
        return m.group(1)
    if name.lower().endswith(".zip"):
        return name[:-4]
    return None


def _split_set_info(path):
    """If `path` (a directory or one segment of a set) belongs to a split/
    spanned archive, return {label, parts} describing it, else None."""
    p = path or ""
    d = p if os.path.isdir(p) else os.path.dirname(p)
    if not os.path.isdir(d):
        return None
    try:
        names = [n for n in os.listdir(d) if os.path.isfile(os.path.join(d, n))]
    except OSError:
        return None

    # Restrict to the family implied by `path` when a specific file was given,
    # so a directory holding several productions doesn't confuse detection.
    target_base = None
    if not os.path.isdir(p):
        target_base = _family_base(os.path.basename(p))

    families = {}
    for n in names:
        if not (_is_split_member(n) or n.lower().endswith(".zip")):
            continue
        b = _family_base(n)
        if b is None:
            continue
        if target_base is not None and b != target_base:
            continue
        families.setdefault(b, []).append(os.path.join(d, n))

    # Detect each format on its OWN member subset so an unrelated sibling (e.g.
    # a plain name.zip next to name.001.zip/.002.zip) can't break detection.
    for base, members in families.items():
        relativity = [m for m in members
                      if SPLIT_ZIP_PATTERN.match(os.path.basename(m))]
        winzip = [m for m in members
                  if WINZIP_SEG_PATTERN.match(os.path.basename(m))]
        plain = [m for m in members if m.lower().endswith(".zip")
                 and not SPLIT_ZIP_PATTERN.match(os.path.basename(m))]
        for candidate in (relativity, winzip + plain[:1] if winzip else []):
            if len(candidate) < 2:
                continue
            ok, _b, ordered = detect_split_zip(candidate)
            if ok:
                return {"label": f"{base} (split archive, {len(ordered)} parts)",
                        "parts": len(ordered)}
    return None


def _clean_cols(cols):
    """Drop BOM / control-char-only tokens and strip stray control chars so the
    confirm screen shows clean header names (some .dat files use 0x14 as quote)."""
    ctrl = "".join(chr(i) for i in range(0x20)) + "﻿"
    out = []
    for c in cols:
        cc = c.replace("﻿", "").strip(ctrl).strip()
        if cc and not all(ord(ch) < 0x20 for ch in cc):
            out.append(cc)
    return out


def find_load_file_path(path):
    """Absolute on-disk path of the load file (.dat/.opt/...) under `path`, or
    None when it only exists inside a zip (caller falls back to the DAG)."""
    p = path or ""
    if os.path.isfile(p) and p.lower().endswith(LOAD_EXTS):
        return p
    if os.path.isdir(p):
        best = None
        for dp, _d, fns in os.walk(p):
            for fn in fns:
                e = os.path.splitext(fn.lower())[1]
                if e in LOAD_EXTS:
                    rank = LOAD_EXTS.index(e)
                    if best is None or rank < best[0]:
                        best = (rank, os.path.join(dp, fn))
        if best:
            return best[1]
    return None


def automap_for_path(path):
    fn, raw = find_load_file(path)
    if fn:
        fmt = _detect_format(fn)
        cols = _clean_cols(_sniff_columns(raw, fmt))
        return {"found": True, "load_file": fn, "format": fmt, "columns": cols,
                "suggested": suggest_map(cols), "target_fields": TARGET_FIELDS}
    # Couldn't read a load file directly — is this a split/spanned set we can at
    # least name? Report it as deferred (mapped automatically at ingest).
    info = _split_set_info(path)
    if info:
        return {"found": True, "deferred": True, "load_file": info["label"],
                "columns": [], "suggested": {}, "target_fields": TARGET_FIELDS,
                "message": ("Load file is inside a split archive (%d parts). "
                            "Columns can't be previewed here; the field map is "
                            "detected and applied automatically at ingest."
                            % info["parts"])}
    return {"found": False, "columns": [], "suggested": {},
            "target_fields": TARGET_FIELDS}
