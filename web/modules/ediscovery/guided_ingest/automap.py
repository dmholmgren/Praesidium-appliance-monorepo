"""
modules/ediscovery/guided_ingest/automap.py

Load-file field auto-mapping for the guided confirm screen. Finds the load file
(.dat/.opt/.dii/.lfp/.csv) under a proposed unit's source path (dir, zip, or
file), sniffs its column headers (reusing production_import._sniff_columns), and
proposes a deterministic header -> target-field map for human confirmation.
"""
import os
import zipfile

from modules.ediscovery.production_import import _detect_format, _sniff_columns

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


def find_load_file(path):
    """Return (filename, first_8k_bytes) for the load file at/under path."""
    p = path or ""
    if p.lower().endswith(".zip") and os.path.isfile(p):
        try:
            with zipfile.ZipFile(p) as z:
                names = [n for n in z.namelist()
                         if (not n.endswith("/")) and n.lower().endswith(LOAD_EXTS)]
                names.sort(key=lambda n: LOAD_EXTS.index(os.path.splitext(n.lower())[1]))
                if names:
                    with z.open(names[0]) as fh:
                        return os.path.basename(names[0]), fh.read(8192)
        except Exception:
            return None, None
        return None, None
    if os.path.isfile(p) and p.lower().endswith(LOAD_EXTS):
        with open(p, "rb") as fh:
            return os.path.basename(p), fh.read(8192)
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
            with open(best[1], "rb") as fh:
                return os.path.basename(best[1]), fh.read(8192)
    return None, None


def _clean_cols(cols):
    """Drop BOM / control-char-only tokens and strip stray control chars so the
    confirm screen shows clean header names (some .dat files use 0x14 as quote)."""
    ctrl = "".join(chr(i) for i in range(0x20)) + "\ufeff"
    out = []
    for c in cols:
        cc = c.replace("\ufeff", "").strip(ctrl).strip()
        if cc and not all(ord(ch) < 0x20 for ch in cc):
            out.append(cc)
    return out


def automap_for_path(path):
    fn, raw = find_load_file(path)
    if not fn:
        return {"found": False, "columns": [], "suggested": {},
                "target_fields": TARGET_FIELDS}
    fmt = _detect_format(fn)
    cols = _clean_cols(_sniff_columns(raw, fmt))
    return {"found": True, "load_file": fn, "format": fmt, "columns": cols,
            "suggested": suggest_map(cols), "target_fields": TARGET_FIELDS}
