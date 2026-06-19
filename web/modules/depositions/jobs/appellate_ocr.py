# -*- coding: utf-8 -*-
"""appellate_ocr.py -- OCR upgrade for image-based record pages (exhibit volumes).

Operational home of the RR OCR capture (promoted from the build-session script). After
native geometry is built, the exhibit volume bound into a Reporter's Record is typically
scanned (image): native extraction yields little or garbage text, so Bates stamps and
exhibit content are unreadable. This job re-extracts the record PDF, OCRs the pages whose
native text is degenerate (few distinct words), merges them with the native tokens for the
born-digital testimony, and persists ONE canonical 'ocr_paddle' rendition. Char offsets
index the merged canonical -- the §0 invariant -- and persist_geometry is the one writer.

Engine: RapidOCR / onnxruntime (CPU in-process here). The GPU sidecar image
(praesidium-ocr:gpu) is the burst home; swap it in behind _ocr_page_lines once it is a
live service.

  python -m modules.depositions.jobs.appellate_ocr --tenant T --appeal CID [--redo]
"""
from __future__ import annotations
import os
import re
import types
import logging
import collections

logger = logging.getLogger(__name__)

RR_KINDS = ("RR", "SUPP_RR")
RENDITION = "ocr_paddle"
MIN_DISTINCT_TOKENS = 20   # a page with fewer distinct alphabetic words is treated as image/garbage
DPI = 220

_OCR = None


def _engine():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        try:
            _OCR = RapidOCR(det_use_cuda=True, cls_use_cuda=True, rec_use_cuda=True)
        except TypeError:
            _OCR = RapidOCR()
    return _OCR


def _ocr_page_lines(page, dpi=DPI):
    import numpy as np
    scale = 72.0 / dpi
    pix = page.get_pixmap(dpi=dpi)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = img[:, :, :3]
    img = np.ascontiguousarray(img[:, :, ::-1])
    res, _ = _engine()(img)
    out = []
    for item in (res or []):
        box, text, conf = item[0], item[1], item[2]
        if not (text or "").strip():
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        out.append({"text": text.strip(), "conf": float(conf),
                    "x": x0 * scale, "y": y0 * scale, "w": (x1 - x0) * scale, "h": (y1 - y0) * scale})
    out.sort(key=lambda t: (round(t["y"] / 6.0), t["x"]))
    return out


_ALPHA = re.compile(r"[A-Za-z]{2,}")


def _pages_needing_ocr(native):
    distinct = collections.defaultdict(set)
    for t in native.tokens:
        for w in _ALPHA.findall((t.text or "").lower()):
            distinct[t.page_number].add(w)
    return {p for p in range(1, native.page_count + 1)
            if len(distinct.get(p, ())) < MIN_DISTINCT_TOKENS}


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def ocr_record_geometry(tenant_id, appellate_case_id, redo=False) -> dict:
    import fitz
    from modules.ediscovery.services.geometry_extraction import extract_pdf_with_geometry, LayoutToken
    from modules.ediscovery.services.geometry_io import persist_geometry

    tenant = (tenant_id or "").strip()
    cid = str(appellate_case_id)
    out = {"docs_ocred": 0, "ocr_pages": 0, "skipped": 0}

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id::text, storage_path, geometry_corpus, geometry_doc_id::text "
                    "FROM record_documents WHERE appellate_case_id=CAST(%s AS uuid) "
                    "  AND record_kind IN %s AND TRIM(tenant_id)=%s "
                    "  AND storage_path IS NOT NULL AND geometry_doc_id IS NOT NULL",
                    (cid, RR_KINDS, tenant))
        recs = cur.fetchall()
    finally:
        conn.close()

    for rdoc_id, path, corpus, doc_id in recs:
        if not redo:
            conn = _connect()
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1 FROM doc_geometry WHERE corpus=%s AND doc_id=%s AND rendition=%s",
                            (corpus, doc_id, RENDITION))
                if cur.fetchone():
                    out["skipped"] += 1
                    continue
            finally:
                conn.close()
        if not path or not os.path.isfile(path):
            out["skipped"] += 1
            continue

        native = extract_pdf_with_geometry(path)
        need = _pages_needing_ocr(native)
        if not need:
            out["skipped"] += 1
            continue

        kept = [t for t in native.tokens if t.page_number not in need]
        ocr_tokens = []
        doc = fitz.open(path)
        try:
            for pno in sorted(need):
                pg = doc[pno - 1]
                pw, ph = pg.rect.width, pg.rect.height
                for ln, l in enumerate(_ocr_page_lines(pg)):
                    ocr_tokens.append(LayoutToken(
                        page_number=pno, page_width=float(pw), page_height=float(ph),
                        x=float(l["x"]), y=float(l["y"]), w=float(l["w"]), h=float(l["h"]),
                        char_start=0, char_end=0, unit="line", text=l["text"],
                        source="rapid_ocr", confidence=float(l["conf"]),
                        font_size=None, is_bold=False, is_italic=False, font_name="",
                        block_no=None, line_no=ln))
        finally:
            doc.close()

        merged = kept + ocr_tokens
        merged.sort(key=lambda t: (t.page_number, round(t.y / 6.0), t.x))
        buf, pos, last_page = [], 0, None
        for t in merged:
            if last_page is not None and t.page_number != last_page:
                buf.append("\n"); pos += 1
            elif buf:
                buf.append(" "); pos += 1
            t.char_start = pos
            buf.append(t.text); pos += len(t.text)
            t.char_end = pos
            last_page = t.page_number
        canonical = "".join(buf)
        bad = sum(1 for t in merged if canonical[t.char_start:t.char_end] != t.text)
        if bad:
            logger.error("appellate_ocr §0 self-check FAILED doc=%s (%d bad) -- skip", doc_id, bad)
            out["skipped"] += 1
            continue

        res = types.SimpleNamespace(tokens=merged, canonical_text=canonical,
                                    page_count=native.page_count, text_source="rapid_ocr_hybrid")
        conn = _connect()
        try:
            persist_geometry(conn, tenant, corpus, doc_id, RENDITION, res)
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("appellate_ocr persist failed doc=%s", doc_id)
            raise
        finally:
            conn.close()
        out["docs_ocred"] += 1
        out["ocr_pages"] += len(need)
        logger.info("appellate_ocr doc=%s ocr_pages=%d", doc_id, len(need))

    return out


if __name__ == "__main__":
    import argparse, json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--appeal", required=True, help="appellate_case_id")
    ap.add_argument("--redo", action="store_true")
    a = ap.parse_args()
    print(json.dumps(ocr_record_geometry(a.tenant, a.appeal, redo=a.redo), indent=2))
