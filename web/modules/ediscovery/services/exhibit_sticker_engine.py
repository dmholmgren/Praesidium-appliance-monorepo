"""
modules/ediscovery/services/exhibit_sticker_engine.py
=====================================================
Exhibit sticker embossing for PDF documents.

Generates and stamps exhibit label stickers onto page 1 of PDFs,
matching the standard LegalExhibitStickers.com format (121x72pt
rounded-corner rectangle with EXHIBIT header, exhibit label, and
optional case info line).

Same PyMuPDF pipeline as bates_engine.stamp_pdf().

Usage:
    from modules.ediscovery.services.exhibit_sticker_engine import emboss_exhibit_sticker

    emboss_exhibit_sticker(
        input_pdf_path="/mnt/praesidium/.../exhibit.pdf",
        output_pdf_path="/mnt/praesidium/.../exhibit.pdf.tmp",
        exhibit_label="A",
        case_info="DC-2024-12345 | Smith v. Jones",
        color="yellow",
        style="3line",
        position="top-right",
        margin=36,
    )
"""

import logging
import os
from pathlib import Path
from typing import Optional, Tuple

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

# ---- Color palette (matches LegalExhibitStickers.com physical products) --
STICKER_COLORS = {
    "blue":        (0.76, 0.87, 0.93),
    "yellow":      (1.0, 0.84, 0.0),
    "white":       (1.0, 1.0, 1.0),
    "transparent": (1.0, 1.0, 1.0),
    "green":       (0.70, 0.88, 0.55),
    "red":         (1.0, 0.70, 0.70),
}

# 121 x 72 pts = 1.68in x 1.0in -- matches physical sticker
STICKER_W = 121
STICKER_H = 72


# ---- Drawing helper ------------------------------------------------------

def _rounded_rect(shape, rect: Tuple[float, float, float, float],
                  r: float, color, fill, width: float):
    """Rounded rectangle via Bezier curves on a PyMuPDF Shape."""
    x0, y0, x1, y1 = rect
    shape.draw_line(fitz.Point(x0 + r, y0), fitz.Point(x1 - r, y0))
    shape.draw_curve(fitz.Point(x1 - r, y0), fitz.Point(x1, y0),
                     fitz.Point(x1, y0 + r))
    shape.draw_line(fitz.Point(x1, y0 + r), fitz.Point(x1, y1 - r))
    shape.draw_curve(fitz.Point(x1, y1 - r), fitz.Point(x1, y1),
                     fitz.Point(x1 - r, y1))
    shape.draw_line(fitz.Point(x1 - r, y1), fitz.Point(x0 + r, y1))
    shape.draw_curve(fitz.Point(x0 + r, y1), fitz.Point(x0, y1),
                     fitz.Point(x0, y1 - r))
    shape.draw_line(fitz.Point(x0, y1 - r), fitz.Point(x0, y0 + r))
    shape.draw_curve(fitz.Point(x0, y0 + r), fitz.Point(x0, y0),
                     fitz.Point(x0 + r, y0))
    shape.finish(color=color, fill=fill, width=width, closePath=True)


# ---- Sticker creation ----------------------------------------------------

def create_sticker_page(
    exhibit_label: str = "A",
    case_info: Optional[str] = None,
    color: str = "yellow",
    style: str = "3line",
) -> fitz.Document:
    """Return a single-page fitz.Document containing the sticker graphic."""

    bg = STICKER_COLORS.get(color, STICKER_COLORS["yellow"])
    black = (0, 0, 0)
    W, H = STICKER_W, STICKER_H

    doc = fitz.open()
    page = doc.new_page(width=W, height=H)

    # -- Background with double border --
    s = page.new_shape()
    _rounded_rect(s, (1, 1, W - 1, H - 1), 8, black, bg, 2.5)
    _rounded_rect(s, (4, 4, W - 4, H - 4), 6, black, bg, 1.0)
    s.commit()

    # -- Text --
    s2 = page.new_shape()

    def _centered(text, fontsize, y):
        w = fitz.get_text_length(text, fontname="hebo", fontsize=fontsize)
        s2.insert_text(((W - w) / 2, y), text,
                       fontname="hebo", fontsize=fontsize, color=black)

    def _auto_shrink(text, max_fs, min_fs, max_w):
        fs = max_fs
        while fitz.get_text_length(text, fontname="hebo", fontsize=fs) > max_w and fs > min_fs:
            fs -= 0.5
        return fs

    if style == "2line":
        _centered("EXHIBIT", 10, 22)
        fs = _auto_shrink(exhibit_label, 28, 14, W - 20)
        _centered(exhibit_label, fs, 55)
    else:
        _centered("EXHIBIT", 9, 18)
        fs = _auto_shrink(exhibit_label, 22, 12, W - 20)
        _centered(exhibit_label, fs, 46)
        ct = case_info or "Case No. | Case Name"
        cs = _auto_shrink(ct, 6.5, 4, W - 16)
        _centered(ct, cs, 64)

    s2.commit()
    return doc


# ---- Emboss a PDF --------------------------------------------------------

def emboss_exhibit_sticker(
    input_pdf_path: str,
    output_pdf_path: str,
    exhibit_label: str = "A",
    case_info: Optional[str] = None,
    color: str = "yellow",
    style: str = "3line",
    position: str = "top-right",
    margin: int = 36,
    page_number: int = 0,
) -> bool:
    """Stamp an exhibit sticker onto a PDF.

    Returns True on success, False on failure.
    """
    try:
        Path(output_pdf_path).parent.mkdir(parents=True, exist_ok=True)
        sticker = create_sticker_page(exhibit_label, case_info, color, style)
        sw, sh = sticker[0].rect.width, sticker[0].rect.height

        doc = fitz.open(str(input_pdf_path))
        if len(doc) == 0:
            doc.close()
            sticker.close()
            return False

        pages = [page_number] if page_number >= 0 else list(range(len(doc)))

        for pn in pages:
            if pn >= len(doc):
                continue
            page = doc[pn]
            pw, ph = page.rect.width, page.rect.height

            coords = {
                "top-right":    (pw - sw - margin, margin),
                "top-left":     (margin, margin),
                "top-center":   ((pw - sw) / 2, margin),
                "bottom-right": (pw - sw - margin, ph - sh - margin),
                "bottom-left":  (margin, ph - sh - margin),
                "bottom-center":((pw - sw) / 2, ph - sh - margin),
            }
            x, y = coords.get(position, coords["top-right"])
            page.show_pdf_page(fitz.Rect(x, y, x + sw, y + sh), sticker, 0)

        doc.save(str(output_pdf_path), garbage=4, deflate=True)
        doc.close()
        sticker.close()
        return True

    except Exception as exc:
        logger.exception("emboss_exhibit_sticker failed: %s -> %s: %s",
                         input_pdf_path, output_pdf_path, exc)
        return False
