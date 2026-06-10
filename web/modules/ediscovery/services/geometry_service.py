"""
On-demand geometry service — Geometry Plumbing Contract v1.1, §5 cache + §6 consumer.
See geometry_extraction.py for the producer. v1 scope: born-digital PDF (native_pdf).
"""

import logging
import os
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras

from modules.ediscovery.services.geometry_extraction import (
    extract_pdf_with_geometry,
    verify_offsets,
)

logger = logging.getLogger(__name__)


def _conn_kwargs():
    raw = os.environ.get("DATABASE_URL", "")
    for p in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://"):
        if raw.startswith(p):
            raw = "postgresql://" + raw[len(p):]
            break
    u = urlparse(raw)
    return {
        "dbname":   u.path.lstrip("/") or "praesidium",
        "user":     u.username or "praesidium",
        "password": u.password or "",
        "host":     u.hostname or "172.28.0.1",
        "port":     str(u.port or 5432),
    }


def _resolve_ediscovery_file(cur, tenant_id, doc_id):
    cur.execute("""
        SELECT d.file_path, d.original_path, d.native_path,
               d.doc_type, d.extracted_text, c.storage_path
        FROM ediscovery_documents d
        JOIN ediscovery_collections c ON c.id = d.collection_id
        WHERE d.id = %(doc)s::uuid AND TRIM(d.tenant_id) = %(tid)s
    """, {"doc": str(doc_id), "tid": tenant_id.strip()})
    row = cur.fetchone()
    if not row:
        return None, None, None
    file_path, original_path, native_path, doc_type, stored_canonical, storage_path = row
    rel = file_path or original_path or native_path
    abs_path = os.path.join(storage_path, rel) if (storage_path and rel) else None
    return abs_path, doc_type, stored_canonical


def _resolve_dms_file(cur, tenant_id, doc_id):
    """Resolve a DMS document to (abs_path, doc_type, stored_canonical).

    file_path is stored absolute. doc_type is inferred from the extension
    (dms_documents.doc_type is a format tag, null for this corpus). stored_canonical
    is returned as None BY DESIGN: dms_documents.content_text was produced by a
    different extractor and is NOT the geometry spine, so it must not gate the
    §0 canonical-drift check. Geometry establishes the canonical spine here;
    re-anchoring content_text/primitive offsets onto it is a separate step."""
    cur.execute(
        """SELECT file_path FROM dms_documents
           WHERE id = %(doc)s::uuid AND TRIM(tenant_id) = %(tid)s""",
        {"doc": str(doc_id), "tid": tenant_id.strip()},
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None, None, None
    abs_path = row[0]
    ext = os.path.splitext(abs_path)[1].lower().lstrip(".")
    doc_type = "pdf" if ext == "pdf" else ext
    return abs_path, doc_type, None


def get_or_build_geometry(tenant_id, corpus, doc_id, rendition="native_pdf", force=False):
    conn = psycopg2.connect(**_conn_kwargs())
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            if not force:
                cur.execute("""
                    SELECT count(*) FROM doc_layout_tokens
                    WHERE corpus=%(c)s AND doc_id=%(d)s::uuid AND rendition=%(r)s
                """, {"c": corpus, "d": str(doc_id), "r": rendition})
                n = cur.fetchone()[0]
                if n:
                    return {"status": "cached", "token_count": n}

            if corpus == "ediscovery":
                abs_path, doc_type, stored_canonical = _resolve_ediscovery_file(cur, tenant_id, doc_id)
            elif corpus == "dms":
                abs_path, doc_type, stored_canonical = _resolve_dms_file(cur, tenant_id, doc_id)
            else:
                return {"status": "unsupported_corpus", "token_count": 0}
            if not abs_path or not os.path.exists(abs_path):
                return {"status": "file_not_found", "token_count": 0, "path": abs_path}
            if doc_type != "pdf":
                return {"status": "not_born_digital_pdf", "token_count": 0, "doc_type": doc_type}

            result = extract_pdf_with_geometry(abs_path)
            if result is None or not result.has_text_layer:
                return {"status": "no_text_layer_queue_ocr", "token_count": 0}

            if stored_canonical is not None and result.canonical_text != stored_canonical:
                logger.warning("geometry canonical drift for %s/%s — not caching (re-ingest needed)",
                               corpus, doc_id)
                return {"status": "canonical_drift", "token_count": 0,
                        "canonical_len": len(result.canonical_text),
                        "stored_len": len(stored_canonical)}

            ok, bad = verify_offsets(result)
            if bad:
                logger.error("offset self-check failed (%d bad) for %s/%s", bad, corpus, doc_id)
                return {"status": "offset_check_failed", "token_count": 0, "bad": bad}

            from modules.ediscovery.services import geometry_io
            geometry_io.persist_geometry(conn, tenant_id, corpus, doc_id, rendition, result)
            conn.commit()
            return {"status": "built", "token_count": len(result.tokens),
                    "pages": result.page_count, "offsets_ok": ok}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def resolve_boxes(tenant_id, corpus, doc_id, char_start, char_end, rendition="native_pdf"):
    conn = psycopg2.connect(**_conn_kwargs())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT page_number, min(x) AS x0, min(y) AS y0,
                       max(x + w) AS x1, max(y + h) AS y1
                FROM doc_layout_tokens
                WHERE corpus=%(c)s AND doc_id=%(d)s::uuid AND rendition=%(r)s
                  AND char_start < %(e)s AND char_end > %(s)s
                GROUP BY page_number
                ORDER BY page_number
            """, {"c": corpus, "d": str(doc_id), "r": rendition,
                  "s": char_start, "e": char_end})
            return [
                {"page": r[0], "x": float(r[1]), "y": float(r[2]),
                 "w": float(r[3]) - float(r[1]), "h": float(r[4]) - float(r[2])}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


# ===========================================================================
# Text-rebased box resolution (spine highlight bridge)
# ---------------------------------------------------------------------------
# An allegation's char offsets index dms_documents.content_text, which (until
# canonical-at-ingest converges the two) is a different canonical string than
# the geometry tokens' char ranges. So we rebase a VERBATIM text slice into
# geometry space by normalized-substring match, then union the overlapping
# token boxes per page. Returns percent (0-100) boxes for the SVG overlay.
# Forward-compatible: when content_text == geometry canonical, this still
# resolves (and a direct offset path can short-circuit it).
# ===========================================================================
import re as _re


def _norm_ws_geo(s):
    return _re.sub(r"\s+", " ", (s or "")).strip()


def _norm_with_map(s):
    """Whitespace-collapsed string + index map back to raw positions."""
    out = []
    idx = []
    prev_sp = False
    for i, ch in enumerate(s):
        if ch.isspace():
            if prev_sp:
                continue
            out.append(" ")
            idx.append(i)
            prev_sp = True
        else:
            out.append(ch)
            idx.append(i)
            prev_sp = False
    return "".join(out), idx


def resolve_boxes_by_text(tenant_id, corpus, doc_id, needle,
                          rendition="native_pdf", max_pages=8):
    """Rebase `needle` into geometry space; return percent boxes per page.

    Returns {matched: bool, boxes: [{page_number,x,y,width,height}], pages: [..]}
    with x/y/width/height as PERCENT of page (0-100), matching the
    PdfAnnotationViewer overlay coordinate convention.
    """
    needle_n = _norm_ws_geo(needle)
    if len(needle_n) < 12:
        return {"matched": False, "boxes": [], "pages": []}
    conn = psycopg2.connect(**_conn_kwargs())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT char_start, char_end, text, page_number, x, y, w, h
                FROM doc_layout_tokens
                WHERE corpus=%(c)s AND doc_id=%(d)s::uuid AND rendition=%(r)s
                ORDER BY char_start
            """, {"c": corpus, "d": str(doc_id), "r": rendition})
            toks = cur.fetchall()
    finally:
        conn.close()
    if not toks:
        return {"matched": False, "boxes": [], "pages": []}

    maxend = max(t[1] for t in toks)
    buf = [" "] * (maxend + 1)
    for cs, ce, txt, _pg, _x, _y, _w, _h in toks:
        if not txt:
            continue
        for i, ch in enumerate(txt):
            p = cs + i
            if 0 <= p <= maxend:
                buf[p] = ch
    geo = "".join(buf)
    geo_n, geo_map = _norm_with_map(geo)

    pos = -1
    used = 0
    for L in (len(needle_n), 220, 140, 90, 55):
        cand = needle_n[:L].strip()
        if len(cand) < 12:
            break
        hit = geo_n.find(cand)
        if hit >= 0:
            pos = hit
            used = len(cand)
            break
    if pos < 0:
        return {"matched": False, "boxes": [], "pages": []}

    gs = geo_map[pos]
    ge = geo_map[min(pos + used - 1, len(geo_map) - 1)] + 1

    by_pg = {}
    for cs, ce, txt, pg, x, y, w, h in toks:
        if cs < ge and ce > gs:
            b = by_pg.get(pg)
            if b is None:
                by_pg[pg] = [x, y, x + w, y + h]
            else:
                b[0] = min(b[0], x)
                b[1] = min(b[1], y)
                b[2] = max(b[2], x + w)
                b[3] = max(b[3], y + h)
    boxes = [
        {"page_number": pg,
         "x": round(float(b[0]) * 100, 3),
         "y": round(float(b[1]) * 100, 3),
         "width": round(float(b[2] - b[0]) * 100, 3),
         "height": round(float(b[3] - b[1]) * 100, 3)}
        for pg, b in sorted(by_pg.items())
    ]
    return {"matched": True, "boxes": boxes[:max_pages],
            "pages": [bx["page_number"] for bx in boxes]}
