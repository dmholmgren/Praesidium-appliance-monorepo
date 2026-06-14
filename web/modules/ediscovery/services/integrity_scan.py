"""
Document integrity scanner — adversarial-document defense (prompt injection,
hidden text, invisible Unicode).

Praesidium processes opposing-party documents by design. This scanner detects
attempts to manipulate AI passes via content embedded in documents:

  PDF render-level checks (PyMuPDF over the source file):
    white_text         — near-white glyph color (luminance >= 0.95)
    microscopic_font   — font size < 2.0pt with non-whitespace text
    offpage_text       — span bbox outside the page rect
    zero_alpha_text    — fully transparent glyphs (where alpha is exposed)

  Canonical-string checks (§0 — offsets index the canonical string):
    unicode_tag_chars  — U+E0000–E007F ASCII smuggling (always critical)
    bidi_override      — directional override/isolate characters
    zero_width_chars   — ZW space/joiner/non-joiner/WJ/BOM above threshold
    soft_hyphen_run    — dense or consecutive soft hyphens
    instruction_pattern— prompt-injection phrasing (override verbs, role
                         markers, eDiscovery classification steering)

  EML/HTML checks:
    html_hidden_text   — display:none / visibility:hidden / font-size:0-1px /
                         white-on-unstated-background blocks carrying text

  DOCX checks (zip + regex on word/document.xml, no python-docx dependency):
    docx_hidden_run    — w:vanish runs
    docx_white_text    — w:color FFFFFF runs
    docx_tiny_font     — w:sz < 4 half-points

  Cross-escalation: render-hidden text whose content ALSO matches an
  instruction pattern is flagged hidden_instruction at critical severity —
  the smoking gun. The hidden text is preserved verbatim in the canonical
  string (never stripped): in litigation it is evidence of bad-faith
  production, and the flag row carries offsets + provenance to prove it.

OCR false-positive guard: scanned PDFs legitimately carry invisible OCR text
layers. Pages where text overlaps a near-full-page image are downgraded to
info with possible_ocr_layer=true in evidence.

CLI (run inside praesidium-web container):
  python -m modules.ediscovery.services.integrity_scan --doc <uuid> --corpus ediscovery
  python -m modules.ediscovery.services.integrity_scan --collection <uuid>
  python -m modules.ediscovery.services.integrity_scan --sweep --corpus ediscovery --limit 100
  python -m modules.ediscovery.services.integrity_scan --file /tmp/suspect.pdf   (no DB writes)
  add --dry-run to print findings without writing.
"""

import argparse
import json
import logging
import os
import re
import sys
import zipfile

logger = logging.getLogger(__name__)

SCANNER_VERSION = "1.0.0"
MAX_PDF_PAGES = 500
SNIPPET_PAD = 60

# ---------------------------------------------------------------- patterns

def _rx(p):
    return re.compile(p, re.IGNORECASE)

# (name, regex, severity)
INSTRUCTION_PATTERNS = [
    ("override_directive", _rx(
        r"(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+|your\s+)?"
        r"(previous|prior|above|earlier|preceding|system|original)\s+"
        r"(instructions?|prompts?|context|messages?|rules?|directives?)"), "critical"),
    ("classification_steer", _rx(
        r"(classify|mark|treat|label|code|designate)\s+(this|the|all|every)\s*"
        r"(documents?|files?|records?|emails?|messages?)?\s*as\s+"
        r"(privileged|non[\s-]?responsive|not\s+responsive|irrelevant|junk|"
        r"confidential|work\s+product)"), "critical"),
    ("suppress_processing", _rx(
        r"do\s+not\s+(summari[sz]e|analy[sz]e|review|extract|process|index|"
        r"read|translate|classify|produce)\s+(this|the)\s+"
        r"(document|file|email|text|content|message)"), "critical"),
    ("role_marker", _rx(
        r"<\|im_(start|end)\|>|\[/?INST\]|<<SYS>>|<\|(system|assistant|user|end)\|>"),
        "critical"),
    ("ai_address", _rx(
        r"\byou\s+are\s+(an?\s+)?(ai|a\.i\.|llm|large\s+language\s+model|"
        r"language\s+model|chat\s*bot|claude|chatgpt|gpt[\s-]?\d|copilot)\b"), "warning"),
    ("ai_directive", _rx(
        r"\b(as\s+an?\s+(ai|llm|language\s+model)|to\s+(the\s+)?(ai|llm|model)\s+"
        r"(reading|reviewing|processing)\s+this)\b"), "warning"),
    ("prompt_literal", _rx(r"\b(system\s+prompt|prompt\s+injection)\b"), "warning"),
]

# canonical-string character classes
TAG_BLOCK = re.compile(r"[\U000E0000-\U000E007F]")
BIDI = re.compile(r"[\u202A-\u202E\u2066-\u2069]")
ZERO_WIDTH = re.compile(r"[\u200B\u200C\u200D\u2060\uFEFF]")
SOFT_HYPHEN_RUN = re.compile(r"\u00AD{3,}")

ZW_THRESHOLD = 10       # ZWJ/ZWNJ legitimate in some scripts
BIDI_THRESHOLD = 3      # legitimate in RTL documents
SOFT_HYPHEN_DENSITY = 50

# HTML hidden-content (EML bodies)
HTML_HIDDEN_STYLE = _rx(
    r"<[a-z][^>]{0,400}style\s*=\s*[\"'][^\"']*"
    r"(display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*[01]\s*(px|pt)|"
    r"color\s*:\s*(#fff(fff)?|white)\b|opacity\s*:\s*0(\.0+)?\s*[;\"'])"
    r"[^\"']*[\"'][^>]*>")

# DOCX run-property markers
DOCX_VANISH = re.compile(r"<w:vanish\s*/?>")
DOCX_WHITE = re.compile(r"<w:color\s+w:val=\"(FFFFFF|ffffff)\"")
DOCX_TINY = re.compile(r"<w:sz\s+w:val=\"([0-3])\"")


def _snippet(text, start, end):
    a = max(0, start - SNIPPET_PAD)
    b = min(len(text), end + SNIPPET_PAD)
    return text[a:b].replace("\n", " ")[:300]


def _instruction_hits(text):
    hits = []
    for name, rx, sev in INSTRUCTION_PATTERNS:
        for m in rx.finditer(text):
            hits.append({"pattern": name, "severity": sev,
                         "char_start": m.start(), "char_end": m.end(),
                         "match": m.group(0)[:200]})
            if len(hits) >= 200:
                return hits
    return hits


# ---------------------------------------------------------------- text scan

def scan_text(canonical):
    """Character-level checks against the §0 canonical string."""
    flags = []
    if not canonical:
        return flags

    for m in TAG_BLOCK.finditer(canonical):
        flags.append({"flag_type": "unicode_tag_chars", "severity": "critical",
                      "char_start": m.start(), "char_end": m.end(),
                      "evidence": {"codepoint": hex(ord(m.group(0))),
                                   "snippet": _snippet(canonical, m.start(), m.end())}})
        if len(flags) >= 50:
            break

    bidi = list(BIDI.finditer(canonical))
    if len(bidi) >= BIDI_THRESHOLD:
        m = bidi[0]
        flags.append({"flag_type": "bidi_override", "severity": "warning",
                      "char_start": m.start(), "char_end": m.end(),
                      "evidence": {"count": len(bidi),
                                   "snippet": _snippet(canonical, m.start(), m.end())}})

    zw = list(ZERO_WIDTH.finditer(canonical))
    if len(zw) >= ZW_THRESHOLD:
        m = zw[0]
        flags.append({"flag_type": "zero_width_chars", "severity": "warning",
                      "char_start": m.start(), "char_end": m.end(),
                      "evidence": {"count": len(zw),
                                   "snippet": _snippet(canonical, m.start(), m.end())}})

    runs = list(SOFT_HYPHEN_RUN.finditer(canonical))
    if runs or canonical.count("\u00AD") >= SOFT_HYPHEN_DENSITY:
        s = runs[0].start() if runs else canonical.find("\u00AD")
        flags.append({"flag_type": "soft_hyphen_run", "severity": "info",
                      "char_start": s, "char_end": s + 1,
                      "evidence": {"total": canonical.count("\u00AD"),
                                   "runs": len(runs)}})

    for h in _instruction_hits(canonical):
        flags.append({"flag_type": "instruction_pattern", "severity": h["severity"],
                      "char_start": h["char_start"], "char_end": h["char_end"],
                      "evidence": {"pattern": h["pattern"], "match": h["match"],
                                   "snippet": _snippet(canonical, h["char_start"],
                                                       h["char_end"])}})
    return flags


# ----------------------------------------------------------------- pdf scan

def _luminance(color_int):
    r = (color_int >> 16) & 255
    g = (color_int >> 8) & 255
    b = color_int & 255
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def scan_pdf(abs_path):
    """Render-level checks. Returns (flags, hidden_texts) where hidden_texts
    feeds the cross-escalation check."""
    import fitz  # PyMuPDF — present in praesidium-web

    flags, hidden_texts = [], []
    doc = fitz.open(abs_path)
    try:
        npages = min(doc.page_count, MAX_PDF_PAGES)
        for pno in range(npages):
            page = doc[pno]
            page_rect = page.rect
            # OCR-layer guard: near-full-page image present?
            ocr_suspect = False
            try:
                for img in page.get_image_info():
                    bb = fitz.Rect(img["bbox"])
                    if bb.get_area() >= 0.85 * page_rect.get_area():
                        ocr_suspect = True
                        break
            except Exception:
                pass

            d = page.get_text("dict")
            for block in d.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = (span.get("text") or "").strip()
                        if not text:
                            continue
                        bbox = span.get("bbox")
                        size = span.get("size", 0)
                        color = span.get("color", 0)
                        alpha = span.get("alpha", 255)
                        common = {"page_number": pno + 1,
                                  "bbox": [round(v, 1) for v in bbox] if bbox else None,
                                  "text": text[:200], "font_size": round(size, 2),
                                  "color": f"#{color:06x}"}

                        def add(ftype, sev, extra=None):
                            ev = dict(common)
                            if extra:
                                ev.update(extra)
                            if ocr_suspect and ftype in ("white_text", "zero_alpha_text"):
                                sev = "info"
                                ev["possible_ocr_layer"] = True
                            flags.append({"flag_type": ftype, "severity": sev,
                                          "page_number": pno + 1, "evidence": ev})
                            hidden_texts.append(text)

                        if alpha == 0:
                            add("zero_alpha_text", "warning")
                        elif _luminance(color) >= 0.95:
                            add("white_text", "warning",
                                {"luminance": round(_luminance(color), 3)})
                        if 0 < size < 2.0:
                            add("microscopic_font", "warning")
                        if bbox and not fitz.Rect(bbox).intersects(page_rect):
                            add("offpage_text", "warning")

                        if len(flags) >= 300:
                            return flags, hidden_texts
        if doc.page_count > MAX_PDF_PAGES:
            flags.append({"flag_type": "scan_truncated", "severity": "info",
                          "page_number": MAX_PDF_PAGES,
                          "evidence": {"total_pages": doc.page_count}})
    finally:
        doc.close()
    return flags, hidden_texts


# ----------------------------------------------------------------- eml scan

def scan_eml(abs_path):
    import email
    from email import policy

    flags = []
    with open(abs_path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        try:
            html = part.get_content()
        except Exception:
            continue
        for m in HTML_HIDDEN_STYLE.finditer(html):
            tail = html[m.end():m.end() + 400]
            inner = re.sub(r"<[^>]+>", " ", tail)
            inner = re.sub(r"\s+", " ", inner).strip()
            if len(inner) < 15:        # hidden spacer/tracking junk, not payload
                continue
            flags.append({"flag_type": "html_hidden_text", "severity": "warning",
                          "evidence": {"style_tag": m.group(0)[:250],
                                       "hidden_content": inner[:300]}})
            if len(flags) >= 50:
                return flags
    return flags


# ---------------------------------------------------------------- docx scan

def scan_docx(abs_path):
    flags = []
    try:
        with zipfile.ZipFile(abs_path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except Exception:
        return flags
    for rx, ftype in ((DOCX_VANISH, "docx_hidden_run"),
                      (DOCX_WHITE, "docx_white_text"),
                      (DOCX_TINY, "docx_tiny_font")):
        hits = list(rx.finditer(xml))
        if not hits:
            continue
        m = hits[0]
        # surface nearby run text for evidence
        seg = xml[m.start():m.start() + 1200]
        texts = re.findall(r"<w:t[^>]*>([^<]+)</w:t>", seg)
        flags.append({"flag_type": ftype, "severity": "warning",
                      "evidence": {"count": len(hits),
                                   "nearby_text": " ".join(texts)[:300]}})
    return flags


# --------------------------------------------------------- cross-escalation

def cross_escalate(flags, hidden_texts):
    """Render-hidden text that itself matches an instruction pattern is the
    smoking gun — emit hidden_instruction at critical."""
    out = []
    for t in hidden_texts:
        for h in _instruction_hits(t):
            out.append({"flag_type": "hidden_instruction", "severity": "critical",
                        "evidence": {"pattern": h["pattern"],
                                     "hidden_text": t[:300], "match": h["match"]}})
            break
    return out


# ------------------------------------------------------------- file routing

def scan_file(abs_path, canonical=None):
    """Scan a raw file + optional canonical text. No DB. Returns flag list."""
    ext = os.path.splitext(abs_path)[1].lower()
    flags, hidden = [], []
    if ext == ".pdf":
        f, hidden = scan_pdf(abs_path)
        flags.extend(f)
    elif ext == ".eml":
        flags.extend(scan_eml(abs_path))
    elif ext in (".docx", ".docm"):
        flags.extend(scan_docx(abs_path))
    if canonical:
        flags.extend(scan_text(canonical))
    flags.extend(cross_escalate(flags, hidden))
    return flags


# ----------------------------------------------------------------- db layer

def _connect():
    import psycopg2
    from modules.ediscovery.services.geometry_service import _conn_kwargs
    conn = psycopg2.connect(**_conn_kwargs())
    conn.autocommit = False
    return conn


def scan_document(tenant_id, corpus, doc_id, dry_run=False):
    """Resolve file + canonical for one document, scan, persist."""
    from modules.ediscovery.services.geometry_service import (
        _resolve_ediscovery_file, _resolve_dms_file)

    conn = _connect()
    tid = tenant_id.strip()
    try:
        with conn.cursor() as cur:
            if corpus == "ediscovery":
                abs_path, _doc_type, canonical = _resolve_ediscovery_file(cur, tid, doc_id)
            elif corpus == "dms":
                abs_path, _doc_type, canonical = _resolve_dms_file(cur, tid, doc_id)
                canonical = None  # dms content_text is not the geometry spine
            else:
                return {"status": "unsupported", "flags": 0}

            status, flags, err = "clean", [], None
            if abs_path and os.path.exists(abs_path):
                try:
                    flags = scan_file(abs_path, canonical=canonical)
                except Exception as e:
                    status, err = "error", f"{type(e).__name__}: {e}"
                    logger.exception("integrity scan failed for %s/%s", corpus, doc_id)
            elif canonical:
                flags = scan_text(canonical)
            else:
                status = "file_not_found"

            if flags and status == "clean":
                status = "flagged"

            if dry_run:
                return {"status": status, "flags": len(flags),
                        "detail": flags, "error": err}

            cur.execute(
                "DELETE FROM doc_integrity_flags WHERE corpus=%s AND doc_id=%s::uuid",
                (corpus, str(doc_id)))
            for fl in flags:
                cur.execute(
                    """INSERT INTO doc_integrity_flags
                       (tenant_id, corpus, doc_id, flag_type, severity,
                        page_number, char_start, char_end, evidence, scanner_version)
                       VALUES (%s,%s,%s::uuid,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                    (tid, corpus, str(doc_id), fl["flag_type"], fl["severity"],
                     fl.get("page_number"), fl.get("char_start"), fl.get("char_end"),
                     json.dumps(fl.get("evidence", {})), SCANNER_VERSION))
            cur.execute(
                """INSERT INTO doc_integrity_scans
                   (tenant_id, corpus, doc_id, scanner_version, status, flags_found, error)
                   VALUES (%s,%s,%s::uuid,%s,%s,%s,%s)
                   ON CONFLICT (corpus, doc_id) DO UPDATE SET
                     scanner_version=EXCLUDED.scanner_version,
                     status=EXCLUDED.status, flags_found=EXCLUDED.flags_found,
                     error=EXCLUDED.error, scanned_at=now()""",
                (tid, corpus, str(doc_id), SCANNER_VERSION, status, len(flags), err))
        conn.commit()
        return {"status": status, "flags": len(flags), "error": err}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _iter_doc_ids(corpus, tenant_id, collection_id=None, limit=None, unscanned_only=True):
    conn = _connect()
    tid = tenant_id.strip()
    try:
        with conn.cursor() as cur:
            if corpus == "ediscovery":
                sql = """SELECT d.id FROM ediscovery_documents d
                         WHERE TRIM(d.tenant_id)=%(tid)s"""
                params = {"tid": tid}
                if collection_id:
                    sql += " AND d.collection_id=%(col)s::uuid"
                    params["col"] = str(collection_id)
            else:
                sql = "SELECT d.id FROM documents d WHERE TRIM(d.tenant_id)=%(tid)s"
                params = {"tid": tid}
            if unscanned_only:
                sql += (" AND NOT EXISTS (SELECT 1 FROM doc_integrity_scans s"
                        " WHERE s.corpus=%(corpus)s AND s.doc_id=d.id)")
                params["corpus"] = corpus
            sql += " ORDER BY d.id"
            if limit:
                sql += " LIMIT %(lim)s"
                params["lim"] = int(limit)
            cur.execute(sql, params)
            return [str(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()


# ----------------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description="Praesidium document integrity scanner")
    ap.add_argument("--tenant", default="986c0fee-1390-43bb-ad28-8cd1db6de53f")
    ap.add_argument("--corpus", default="ediscovery", choices=["ediscovery", "dms"])
    ap.add_argument("--doc")
    ap.add_argument("--collection")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--rescan", action="store_true", help="include already-scanned docs")
    ap.add_argument("--file", help="scan a raw file path, no DB")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    if args.file:
        flags = scan_file(args.file)
        print(json.dumps({"file": args.file, "flags_found": len(flags),
                          "flags": flags}, indent=2, default=str))
        return

    if args.doc:
        res = scan_document(args.tenant, args.corpus, args.doc, dry_run=args.dry_run)
        print(json.dumps(res, indent=2, default=str))
        return

    if args.collection or args.sweep:
        ids = _iter_doc_ids(args.corpus, args.tenant,
                            collection_id=args.collection, limit=args.limit,
                            unscanned_only=not args.rescan)
        totals = {"scanned": 0, "flagged": 0, "errors": 0, "flags": 0}
        for did in ids:
            r = scan_document(args.tenant, args.corpus, did, dry_run=args.dry_run)
            totals["scanned"] += 1
            totals["flags"] += r["flags"]
            if r["status"] == "flagged":
                totals["flagged"] += 1
                print(f"FLAGGED {did}: {r['flags']} flag(s)")
            elif r["status"] == "error":
                totals["errors"] += 1
                print(f"ERROR   {did}: {r.get('error')}")
        print(json.dumps(totals, indent=2))
        return

    ap.print_help()


if __name__ == "__main__":
    main()
