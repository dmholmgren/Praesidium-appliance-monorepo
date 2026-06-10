# -*- coding: utf-8 -*-
"""
modules/ediscovery/services/geometry_io.py

Single-source geometry I/O: BUILD ONCE, READ EVERYWHERE.

The coordinate mapper (extract_pdf_with_geometry) is expensive and, run repeatedly,
risks cross-stage drift: three independent extractions only stay byte-aligned because
PyMuPDF is deterministic. The §0 spine assumes ONE canonical string, so we persist it.

Geometry = doc_geometry (canonical_text + metadata, one row per doc)
         + doc_layout_tokens (the positioned word tokens).
Both are written together, once, in build_geometry(). Every consumer (boundary,
sectioner, chunker, resolve_boxes, future re-embedding) reads the identical artifact
via load_geometry() and never re-parses the PDF.

load_geometry returns the same LayoutToken shape the extractor produces, so existing
callers swap `extract_pdf_with_geometry(path)` -> `load_geometry(corpus, doc_id)` with
no other change to their token handling.
"""
from __future__ import annotations

import logging

import psycopg2
import psycopg2.extras

from modules.ediscovery.services.geometry_service import _conn_kwargs
from modules.ediscovery.services.geometry_extraction import (
    extract_pdf_with_geometry,
    verify_offsets,
    LayoutToken,
)

logger = logging.getLogger("praesidium.geometry_io")

RENDITION = "native_pdf"
EXTRACTION_MODEL = "geometry_v1"

_TOKEN_INSERT = """
    INSERT INTO doc_layout_tokens
      (tenant_id, corpus, doc_id, rendition, page_number, page_width, page_height,
       x, y, w, h, char_start, char_end, unit, text, source, confidence,
       font_size, is_bold, is_italic, font_name, block_no, line_no)
    VALUES %s
"""
_TOKEN_TMPL = "(%s,%s,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"


def _token_rows(tenant_id, corpus, doc_id, rendition, tokens):
    return [
        (tenant_id, corpus, str(doc_id), rendition, t.page_number,
         t.page_width, t.page_height, t.x, t.y, t.w, t.h,
         t.char_start, t.char_end, t.unit, t.text, t.source, t.confidence,
         getattr(t, "font_size", None), getattr(t, "is_bold", False),
         getattr(t, "is_italic", False), getattr(t, "font_name", ""),
         getattr(t, "block_no", None), getattr(t, "line_no", None))
        for t in tokens
    ]


def _persist(conn, tenant_id, corpus, doc_id, rendition, res):
    """Write tokens + canonical header for `res` into the given txn (no commit)."""
    with conn.cursor() as c:
        c.execute(
            "DELETE FROM doc_layout_tokens WHERE corpus=%s AND doc_id=%s::uuid AND rendition=%s",
            (corpus, str(doc_id), rendition),
        )
        psycopg2.extras.execute_values(
            c, _TOKEN_INSERT,
            _token_rows(tenant_id, corpus, doc_id, rendition, res.tokens),
            template=_TOKEN_TMPL,
        )
        c.execute("""
            INSERT INTO doc_geometry
              (tenant_id, corpus, doc_id, rendition, canonical_text, page_count,
               char_count, token_count, has_text_layer, source, extraction_model, built_at)
            VALUES (%s,%s,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (corpus, doc_id, rendition) DO UPDATE SET
              tenant_id        = EXCLUDED.tenant_id,
              canonical_text   = EXCLUDED.canonical_text,
              page_count       = EXCLUDED.page_count,
              char_count       = EXCLUDED.char_count,
              token_count      = EXCLUDED.token_count,
              has_text_layer   = EXCLUDED.has_text_layer,
              source           = EXCLUDED.source,
              extraction_model = EXCLUDED.extraction_model,
              built_at         = now()
        """, (tenant_id, corpus, str(doc_id), rendition, res.canonical_text,
              res.page_count, len(res.canonical_text), len(res.tokens),
              True, res.text_source, EXTRACTION_MODEL))


def persist_geometry(conn, tenant_id, corpus, doc_id, rendition, res):
    """Public single-source persist: write doc_layout_tokens (incl. block/line)
    + the doc_geometry canonical header for an already-extracted, §0-verified
    result, in the caller's transaction (caller commits). This is the ONE writer
    of geometry rows -- every producer routes through it (no duplicated INSERTs)."""
    _persist(conn, tenant_id, corpus, doc_id, rendition, res)


def build_geometry(tenant_id, corpus, doc_id, path, rendition=RENDITION, conn=None):
    """Extract ONCE, §0-verify, persist canonical (doc_geometry) + tokens
    (doc_layout_tokens) atomically. Returns (canonical, tokens, meta).

    Returns None if the PDF has no text layer (caller should queue OCR).
    Raises ValueError if the §0 offset self-check fails (never persists bad geometry).
    Idempotent: replaces any prior geometry for (corpus, doc_id, rendition).

    If `conn` is supplied the writes join the caller's transaction (the caller
    commits); otherwise an own connection is opened and committed here.
    """
    res = extract_pdf_with_geometry(path)
    if res is None or not res.has_text_layer:
        return None
    ok, bad = verify_offsets(res)
    if bad:
        raise ValueError(
            f"\u00a70 offset self-check failed ({bad} bad tokens) for {corpus}/{doc_id}; "
            "refusing to persist geometry")

    own = conn is None
    if own:
        conn = psycopg2.connect(**_conn_kwargs())
        conn.autocommit = False
    try:
        _persist(conn, tenant_id, corpus, doc_id, rendition, res)
        if own:
            conn.commit()
    except Exception:
        if own:
            conn.rollback()
        raise
    finally:
        if own:
            conn.close()

    meta = {"page_count": res.page_count, "char_count": len(res.canonical_text),
            "token_count": len(res.tokens), "offsets_ok": ok,
            "source": res.text_source, "built": True}
    return res.canonical_text, res.tokens, meta


def load_geometry(corpus, doc_id, rendition=RENDITION, conn=None):
    """Read persisted (canonical, tokens, meta) for a doc, or None if not built.

    Tokens are rebuilt as LayoutToken objects ordered by char_start — the same
    shape extract_pdf_with_geometry returns — so consumers are drop-in.
    canonical + tokens come from the SAME build, so §0 holds by construction.
    """
    own = conn is None
    if own:
        conn = psycopg2.connect(**_conn_kwargs())
        conn.autocommit = True
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT canonical_text, page_count, char_count, token_count,
                       has_text_layer, source
                FROM doc_geometry
                WHERE corpus=%s AND doc_id=%s::uuid AND rendition=%s
            """, (corpus, str(doc_id), rendition))
            hdr = c.fetchone()
            if not hdr:
                return None
            canonical, page_count, char_count, token_count, has_text, source = hdr
            c.execute("""
                SELECT page_number, page_width, page_height, x, y, w, h,
                       char_start, char_end, unit, text, source, confidence,
                       font_size, is_bold, is_italic, font_name, block_no, line_no
                FROM doc_layout_tokens
                WHERE corpus=%s AND doc_id=%s::uuid AND rendition=%s
                ORDER BY char_start
            """, (corpus, str(doc_id), rendition))
            toks = [
                LayoutToken(
                    page_number=r[0],
                    page_width=float(r[1]), page_height=float(r[2]),
                    x=float(r[3]), y=float(r[4]), w=float(r[5]), h=float(r[6]),
                    char_start=r[7], char_end=r[8], unit=r[9], text=r[10],
                    source=r[11],
                    confidence=(float(r[12]) if r[12] is not None else None),
                    font_size=(float(r[13]) if r[13] is not None else None),
                    is_bold=bool(r[14]), is_italic=bool(r[15]),
                    font_name=(r[16] or ""),
                    block_no=r[17], line_no=r[18],
                )
                for r in c.fetchall()
            ]
    finally:
        if own:
            conn.close()

    meta = {"page_count": page_count, "char_count": char_count,
            "token_count": token_count, "source": source,
            "has_text_layer": has_text, "built": False}
    return canonical, toks, meta


def ensure_geometry(tenant_id, corpus, doc_id, path, rendition=RENDITION):
    """Load persisted geometry; build it once if absent. Single entry point for
    consumers that just want (canonical, tokens, meta). Returns None if no text layer."""
    g = load_geometry(corpus, doc_id, rendition)
    if g is not None:
        return g
    return build_geometry(tenant_id, corpus, doc_id, path, rendition)
