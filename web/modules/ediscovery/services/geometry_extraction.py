"""
Geometry-aware PDF extraction — Geometry Plumbing Contract v1.1, producer slice.

canonical_text is built *from* the positioned word tokens, in reading order, so
every token's (char_start, char_end) indexes the exact same string chunks /
search / redaction / spine index. The §0 offset bridge is EXACT BY CONSTRUCTION.

  canonical_text  = reading-order concatenation of this doc's word tokens.
  normalized_text = downstream embedding INPUT only; NEVER an offset basis.

fitz native space is top-left origin, y-down, in PDF points — the canvas
convention, so normalized coords need NO flip. page_width/height (points) kept
for burn-in round-trip (normalized -> points for apply_redactions()).

OCR (boxes from hOCR/ALTO) and Office (boxes from the rendered PDF) reuse this
exact shape: same LayoutToken stream, same canonical-from-tokens rule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# word tuple from page.get_text("words"):
#   (x0, y0, x1, y1, "word", block_no, line_no, word_no)
_X0, _Y0, _X1, _Y1, _TEXT, _BLOCK, _LINE, _WORD = range(8)

_PAGE_BREAK = "\f"  # page-boundary marker inside canonical_text


@dataclass
class LayoutToken:
    page_number: int
    page_width: float
    page_height: float
    x: float
    y: float
    w: float
    h: float
    char_start: int
    char_end: int
    unit: str = "word"
    text: str = ""
    source: str = "pdf_textlayer"
    confidence: Optional[float] = None
    font_size: Optional[float] = None
    is_bold: bool = False
    is_italic: bool = False
    font_name: str = ""
    block_no: Optional[int] = None
    line_no: Optional[int] = None


@dataclass
class GeometryExtractionResult:
    canonical_text: str
    tokens: list
    page_count: int
    text_source: str          # 'pdf_textlayer' | 'none' (caller sets 'ocr' on backfill)
    has_text_layer: bool      # False -> queue OCR backfill (ocr_image rendition)
    rendition: str = "native_pdf"


def _clean_word(s: str) -> str:
    # Strip NUL BEFORE measuring so offsets stay exact. Never global-replace the
    # assembled canonical_text — that would shift offsets.
    return s.replace("\x00", "") if s else ""


def extract_pdf_with_geometry(file_path: str) -> Optional[GeometryExtractionResult]:
    import fitz  # PyMuPDF

    try:
        doc = fitz.open(file_path)
    except Exception as e:
        logger.error("PyMuPDF open failed for %s: %s", file_path, e)
        return None

    if getattr(doc, "is_encrypted", False):
        try:
            doc.authenticate("")
        except Exception as e:
            logger.warning("authenticate('') failed for %s: %s", file_path, e)

    parts: list = []
    tokens: list = []
    cursor = 0
    any_text = False
    page_count = len(doc)

    try:
        for pno, page in enumerate(doc, start=1):
            try:
                rect = page.rect
                pw, ph = float(rect.width), float(rect.height)
                words = page.get_text("words", sort=True)  # reading order
            except Exception as e:
                logger.warning("page %d skipped in %s: %s", pno, file_path, e)
                parts.append(_PAGE_BREAK)
                cursor += len(_PAGE_BREAK)
                continue

            # Typography map (additive; never feeds canonical_text or offsets).
            # Spans carry font/size/flags; each word is matched to a span by
            # bbox-center containment below. §0 stays exact by construction.
            _spans = []
            try:
                _d = page.get_text("dict")
                for _blk in _d.get("blocks", []):
                    if _blk.get("type", 0) != 0:
                        continue
                    for _ln in _blk.get("lines", []):
                        for _sp in _ln.get("spans", []):
                            _bb = _sp.get("bbox")
                            if not _bb:
                                continue
                            _spans.append((
                                _bb[0], _bb[1], _bb[2], _bb[3],
                                float(_sp.get("size") or 0.0),
                                int(_sp.get("flags") or 0),
                                (_sp.get("font") or ""),
                            ))
            except Exception:
                _spans = []

            prev_line = None
            for w in words:
                wtext = _clean_word(w[_TEXT])
                if not wtext:
                    continue
                any_text = True

                if parts and not parts[-1].endswith(_PAGE_BREAK):
                    line = (w[_BLOCK], w[_LINE])
                    sep = "\n" if line != prev_line else " "
                    parts.append(sep)
                    cursor += len(sep)
                prev_line = (w[_BLOCK], w[_LINE])

                start = cursor
                parts.append(wtext)
                cursor += len(wtext)

                if pw > 0 and ph > 0:
                    _cx = (w[_X0] + w[_X1]) / 2.0
                    _cy = (w[_Y0] + w[_Y1]) / 2.0
                    _fsize = None
                    _bold = _ital = False
                    _font = ""
                    for _sx0, _sy0, _sx1, _sy1, _sz, _fl, _fn in _spans:
                        if _sx0 <= _cx <= _sx1 and _sy0 <= _cy <= _sy1:
                            _fsize = _sz or None
                            _lf = _fn.lower()
                            _bold = bool(_fl & 16) or ("bold" in _lf)
                            _ital = bool(_fl & 2) or ("italic" in _lf) or ("oblique" in _lf)
                            _font = _fn
                            break
                    tokens.append(LayoutToken(
                        page_number=pno,
                        page_width=pw, page_height=ph,
                        x=w[_X0] / pw, y=w[_Y0] / ph,
                        w=(w[_X1] - w[_X0]) / pw, h=(w[_Y1] - w[_Y0]) / ph,
                        char_start=start, char_end=cursor,
                        unit="word", text=wtext, source="pdf_textlayer",
                        font_size=_fsize, is_bold=_bold,
                        is_italic=_ital, font_name=_font,
                        block_no=int(w[_BLOCK]), line_no=int(w[_LINE]),
                    ))

            parts.append(_PAGE_BREAK)
            cursor += len(_PAGE_BREAK)
    finally:
        doc.close()

    canonical = "".join(parts)
    return GeometryExtractionResult(
        canonical_text=canonical if canonical.strip() else "",
        tokens=tokens,
        page_count=page_count,
        text_source="pdf_textlayer" if any_text else "none",
        has_text_layer=any_text,
        rendition="native_pdf",
    )


def tokens_as_rows(result, *, tenant_id: str, corpus: str, doc_id: str) -> Iterator[dict]:
    """Insert-ready dicts for doc_layout_tokens. Bind doc_id with CAST(:doc_id AS uuid);
    keep tenant_id char(36) as stored. Persistence policy is the caller's: eager for
    DMS, on first-open/production-staging for the 1.5M eDiscovery corpus (§5)."""
    for t in result.tokens:
        yield {
            "tenant_id": tenant_id, "corpus": corpus, "doc_id": doc_id,
            "rendition": result.rendition,
            "page_number": t.page_number,
            "page_width": t.page_width, "page_height": t.page_height,
            "x": t.x, "y": t.y, "w": t.w, "h": t.h,
            "char_start": t.char_start, "char_end": t.char_end,
            "unit": t.unit, "text": t.text,
            "source": t.source, "confidence": t.confidence,
            "font_size": getattr(t, "font_size", None),
            "is_bold": getattr(t, "is_bold", False),
            "is_italic": getattr(t, "is_italic", False),
            "font_name": getattr(t, "font_name", ""),
            "block_no": getattr(t, "block_no", None),
            "line_no": getattr(t, "line_no", None),
        }


def verify_offsets(result) -> tuple:
    """Self-check §0: canonical_text[char_start:char_end] == token.text for every token."""
    ok = bad = 0
    for t in result.tokens:
        if result.canonical_text[t.char_start:t.char_end] == t.text:
            ok += 1
        else:
            bad += 1
    return ok, bad
