"""
geometry_intake.py -- eDiscovery explode-and-map, GEOMETRY stage.

Establishes the geometry substrate that search-highlighting and redaction depend
on: every reviewable/producible unit resolves to a PDF whose positioned word
tokens (doc_layout_tokens) ARE the canonical text. extracted_text is set to that
token-built canonical, so the segmenter/chunker index exactly what the boxes
index -- the §0 invariant holds across the whole stack and resolve_boxes() can
light up any char range for highlight or burn a redaction over it.

Uniform render->tokenize rule (handoff + this session's decisions), routed per
unit by native extension:

  born-digital PDF  -> tokenize the native (rendition='native_pdf') NOW;
                       extracted_text = token canonical; doc_layout_tokens written.
  office/email/html -> render to PDF first, then tokenize (rendition='rendered_pdf').
                       Needs the HTML->PDF renderer / libreoffice lane -> DEFERRED:
                       staged as stage='render' state='pending'.
  scanned PDF/image -> OCR to a text-layer PDF (ocrmypdf), then tokenize.
                       Needs ocrmypdf -> DEFERRED: staged as stage='ocr' state='pending'.

Only the native-PDF path is live here; the render and ocr lanes are wired as
pending ledger rows so the backlog is observable and the workers can pick them
up once their tooling lands. Segment/chunk run only on units whose canonical is
FINAL (geometry done) -- so we never segment a provisional string that a later
render/OCR will supersede.

Conventions (house): psycopg2 + DATABASE_URL; CAST/TRIM; native_path RELATIVE to
the collection root; per-unit error isolation; idempotent (resume on the ledger).

CLI (inside praesidium-web):
  python -m modules.ediscovery.jobs.geometry_intake \
      --tenant <uuid> --collection <uuid> [--limit N] [--redo] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# rendition rule by native extension -------------------------------------------------
PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".bmp"}
RENDER_EXTS = {".doc", ".docx", ".rtf", ".odt", ".html", ".htm",
               ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".ods", ".eml", ".msg"}

NATIVE_RENDITION = "native_pdf"
RENDER_RENDITION = "rendered_pdf"
OCR_RENDITION = "ocr_pdf"


# ---------------------------------------------------------------------------
# db (mirror preserve/enrich)
# ---------------------------------------------------------------------------

def _db_kwargs() -> dict:
    from urllib.parse import urlparse
    raw = os.environ.get("DATABASE_URL", "")
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://"):
        if raw.startswith(prefix):
            raw = "postgresql://" + raw[len(prefix):]
            break
    p = urlparse(raw)
    return {"dbname": p.path.lstrip("/") or "praesidium", "user": p.username or "praesidium",
            "password": p.password or "", "host": p.hostname or "172.28.0.1",
            "port": str(p.port or 5432)}


def _connect():
    import psycopg2
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _stage(cur, tenant, doc_id, collection_id, stage, state,
           worker_id=None, input_hash=None, duration_ms=None,
           error_class=None, error_message=None):
    cur.execute(
        """
        INSERT INTO ediscovery_stage_status
            (tenant_id, document_id, collection_id, stage, state, attempt,
             worker_id, input_hash, started_at, finished_at, duration_ms,
             error_class, error_message, updated_at)
        VALUES (%s, CAST(%s AS uuid), CAST(%s AS uuid), %s, %s, 1,
                %s, %s, now(), now(), %s, %s, %s, now())
        ON CONFLICT (tenant_id, document_id, stage) DO UPDATE SET
            state=EXCLUDED.state, attempt=ediscovery_stage_status.attempt+1,
            collection_id=EXCLUDED.collection_id, worker_id=EXCLUDED.worker_id,
            input_hash=EXCLUDED.input_hash, finished_at=now(),
            duration_ms=EXCLUDED.duration_ms, error_class=EXCLUDED.error_class,
            error_message=EXCLUDED.error_message, updated_at=now()
        """,
        (tenant, str(doc_id), str(collection_id), stage, state,
         worker_id, input_hash, duration_ms, error_class,
         (error_message[:4000] if error_message else None)),
    )


# ---------------------------------------------------------------------------
# token persistence (replicates geometry_service insert; resolves native_path
# ourselves so we don't depend on the shared resolver's file_path/original_path
# preference, which doesn't match preserve_collection's native-only layout)
# ---------------------------------------------------------------------------

_TOKEN_INSERT = """
INSERT INTO doc_layout_tokens
  (tenant_id, corpus, doc_id, rendition, page_number,
   page_width, page_height, x, y, w, h,
   char_start, char_end, unit, text, source, confidence,
   font_size, is_bold, is_italic, font_name)
VALUES %s
"""
_TOKEN_TEMPLATE = "(%s,%s,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"


def _persist_geometry(cur, tenant, doc_id, rendition, result, corpus="ediscovery",
                      write_canonical=True):
    """Write doc_layout_tokens (and, for ediscovery, extracted_text=canonical) in
    the caller's txn so the §0 spine is atomic. For dms we do NOT touch
    content_text -- geometry establishes its own canonical and allegations bridge
    via resolve_boxes_by_text. Returns (token_count, offsets_ok, bad)."""
    import psycopg2.extras
    from modules.ediscovery.services.geometry_extraction import verify_offsets
    ok, bad = verify_offsets(result)
    if bad:
        raise ValueError("offset self-check failed: %d bad tokens" % bad)

    if write_canonical:
        cur.execute(
            "UPDATE ediscovery_documents SET extracted_text=%s, text_source='extract', "
            "processing_status='geometry_done' WHERE id=CAST(%s AS uuid)",
            (result.canonical_text, str(doc_id)))

    cur.execute("DELETE FROM doc_layout_tokens WHERE corpus=%s "
                "AND doc_id=%s::uuid AND rendition=%s", (corpus, str(doc_id), rendition))
    rows = [
        (tenant, corpus, str(doc_id), rendition, t.page_number,
         t.page_width, t.page_height, t.x, t.y, t.w, t.h,
         t.char_start, t.char_end, t.unit, t.text, t.source, t.confidence,
         getattr(t, "font_size", None), getattr(t, "is_bold", False),
         getattr(t, "is_italic", False), getattr(t, "font_name", ""))
        for t in result.tokens
    ]
    if rows:
        psycopg2.extras.execute_values(cur, _TOKEN_INSERT, rows, template=_TOKEN_TEMPLATE)
    return len(rows), ok, bad


def _mark_pending(cur, doc_id, status):
    cur.execute("UPDATE ediscovery_documents SET processing_status=%s "
                "WHERE id=CAST(%s AS uuid)", (status, str(doc_id)))


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------

def _resolve_collection(cur, tenant, collection_id):
    cur.execute("""SELECT id, storage_path, collection_name FROM ediscovery_collections
                   WHERE TRIM(tenant_id)=%s AND id=CAST(%s AS uuid)""",
                (tenant, str(collection_id)))
    row = cur.fetchone()
    if not row:
        raise ValueError("collection %s not found" % collection_id)
    return {"id": row[0], "storage_path": row[1], "collection_name": row[2]}


def _select_units(cur, tenant, collection_id, redo, limit):
    done = "" if redo else (
        " AND NOT EXISTS (SELECT 1 FROM ediscovery_stage_status s "
        "  WHERE s.tenant_id=d.tenant_id AND s.document_id=d.id AND s.stage='geometry')")
    sql = ("SELECT d.id::text, d.doc_type, d.native_path, d.file_hash "
           "FROM ediscovery_documents d "
           "WHERE TRIM(d.tenant_id)=%s AND d.collection_id=CAST(%s AS uuid) "
           "  AND d.native_path IS NOT NULL" + done +
           " ORDER BY d.family_id NULLS FIRST, d.is_attachment, d.attachment_index NULLS FIRST")
    if limit:
        sql += " LIMIT %d" % int(limit)
    cur.execute(sql, (tenant, str(collection_id)))
    return cur.fetchall()


def _route(native_path: str) -> str:
    ext = Path(native_path).suffix.lower()
    if ext in PDF_EXTS:
        return "pdf"
    if ext in IMAGE_EXTS:
        return "ocr"        # no text layer -> OCR-to-PDF lane
    if ext in RENDER_EXTS:
        return "render"     # office/email/html -> render-to-PDF lane
    return "render"         # unknown -> best-effort render lane


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------

def geometry_intake(tenant_id, collection_id, redo=False, limit=0, dry_run=False) -> dict:
    from modules.ediscovery.services.geometry_extraction import extract_pdf_with_geometry
    tenant = tenant_id.strip()
    worker_id = os.environ.get("HOSTNAME", "geometry")
    s = {"pdf_tokenized": 0, "pdf_no_text_ocr": 0, "render_pending": 0,
         "ocr_pending": 0, "errors": 0, "tokens": 0, "dry_run": dry_run}

    conn = _connect()
    try:
        cur = conn.cursor()
        coll = _resolve_collection(cur, tenant, collection_id)
        coll_root = Path(coll["storage_path"])
        units = _select_units(cur, tenant, collection_id, redo, limit)
        logger.info("geometry_intake %s (%s): %d unit(s) root=%s dry=%s",
                    coll["collection_name"], collection_id, len(units), coll_root, dry_run)

        for (doc_id, doc_type, native_path, file_hash) in units:
            t0 = time.time()
            lane = _route(native_path)
            try:
                if lane == "render":
                    if not dry_run:
                        _mark_pending(cur, doc_id, "render_pending")
                        _stage(cur, tenant, doc_id, collection_id, "render", "pending",
                               worker_id=worker_id, input_hash=file_hash)
                        conn.commit()
                    s["render_pending"] += 1
                    continue

                if lane == "ocr":
                    if not dry_run:
                        _mark_pending(cur, doc_id, "ocr_pending")
                        _stage(cur, tenant, doc_id, collection_id, "ocr", "pending",
                               worker_id=worker_id, input_hash=file_hash)
                        conn.commit()
                    s["ocr_pending"] += 1
                    continue

                # lane == pdf
                abs_path = coll_root / native_path
                if not abs_path.exists():
                    raise FileNotFoundError(str(abs_path))
                result = extract_pdf_with_geometry(str(abs_path))
                no_text = (result is None or not getattr(result, "has_text_layer", False)
                           or not (result.canonical_text or "").strip())
                if no_text:
                    # scanned PDF: defer to OCR-to-PDF lane
                    if not dry_run:
                        _mark_pending(cur, doc_id, "ocr_pending")
                        _stage(cur, tenant, doc_id, collection_id, "geometry", "skipped",
                               worker_id=worker_id, input_hash=file_hash)
                        _stage(cur, tenant, doc_id, collection_id, "ocr", "pending",
                               worker_id=worker_id, input_hash=file_hash)
                        conn.commit()
                    s["pdf_no_text_ocr"] += 1
                    continue

                if dry_run:
                    logger.info("  [DRY] %s pdf tokens=%d pages=%s chars=%d",
                                doc_id[:8], len(result.tokens),
                                getattr(result, "page_count", "?"), len(result.canonical_text))
                    s["pdf_tokenized"] += 1
                    continue

                n_tok, ok, bad = _persist_geometry(cur, tenant, doc_id, NATIVE_RENDITION, result)
                _stage(cur, tenant, doc_id, collection_id, "geometry", "done",
                       worker_id=worker_id, input_hash=file_hash,
                       duration_ms=int((time.time() - t0) * 1000))
                conn.commit()
                s["pdf_tokenized"] += 1
                s["tokens"] += n_tok

            except Exception as e:
                s["errors"] += 1
                logger.exception("unit %s (%s/%s) failed", doc_id, doc_type, lane)
                if not dry_run:
                    conn.rollback()
                    _stage(cur, tenant, doc_id, collection_id, "geometry", "failed",
                           worker_id=worker_id, input_hash=file_hash,
                           error_class=type(e).__name__, error_message=str(e))
                    conn.commit()
        return s
    finally:
        conn.close()


DMS_GATE = (" lower(file_path) LIKE '%%.pdf'"
            " AND content_text IS NOT NULL"
            " AND file_path !~* '/12[- ]?e-?discovery/'"
            " AND file_path NOT ILIKE '%%/01-Client Documents/%%'"
            " AND file_path !~* '/production/'"
            " AND file_path !~* '/load[_ ]?files?/'")


def geometry_intake_dms(tenant_id, path_like=None, redo=False, limit=0, dry_run=False) -> dict:
    """Geometry over the CURATED DMS corpus (pleadings/motions/contracts/drafts).
    Writes doc_layout_tokens corpus='dms' only -- never content_text."""
    from modules.ediscovery.services.geometry_extraction import extract_pdf_with_geometry
    tenant = tenant_id.strip()
    s = {"pdf_tokenized": 0, "no_text": 0, "errors": 0, "tokens": 0, "dry_run": dry_run}
    conn = _connect()
    try:
        cur = conn.cursor()
        done = "" if redo else (" AND NOT EXISTS (SELECT 1 FROM doc_layout_tokens t "
                                "WHERE t.doc_id=d.id AND t.corpus='dms')")
        where = ["TRIM(d.tenant_id)=%s", DMS_GATE]
        params = [tenant]
        if path_like:
            where.append("d.file_path ILIKE %s"); params.append("%" + path_like + "%")
        sql = ("SELECT d.id::text, d.file_path FROM dms_documents d WHERE "
               + " AND ".join(where) + done + " ORDER BY d.id")
        if limit:
            sql += " LIMIT %d" % int(limit)
        cur.execute(sql, params)
        units = cur.fetchall()
        logger.info("geometry_intake[dms]: %d gated pdf unit(s) path_like=%s dry=%s",
                    len(units), path_like, dry_run)
        for (doc_id, file_path) in units:
            try:
                if not file_path or not os.path.exists(file_path):
                    raise FileNotFoundError(str(file_path))
                result = extract_pdf_with_geometry(file_path)
                if (result is None or not getattr(result, "has_text_layer", False)
                        or not (result.canonical_text or "").strip()):
                    s["no_text"] += 1
                    continue
                if dry_run:
                    logger.info("  [DRY] %s dms tokens=%d", doc_id[:8], len(result.tokens))
                    s["pdf_tokenized"] += 1
                    continue
                n_tok, _, _ = _persist_geometry(cur, tenant, doc_id, NATIVE_RENDITION, result,
                                                corpus="dms", write_canonical=False)
                conn.commit()
                s["pdf_tokenized"] += 1
                s["tokens"] += n_tok
            except Exception:
                s["errors"] += 1
                conn.rollback()
                logger.exception("dms unit %s failed", doc_id)
        return s
    finally:
        conn.close()


def _lo_convert(src, outdir):
    """LibreOffice headless -> PDF. Unique profile dir avoids lock contention."""
    import subprocess, uuid as _uuid, glob
    prof = "/tmp/lo_%s" % _uuid.uuid4().hex[:8]
    subprocess.run(["soffice", "--headless", "-env:UserInstallation=file://%s" % prof,
                    "--convert-to", "pdf", "--outdir", outdir, src],
                   capture_output=True, timeout=300)
    out = os.path.join(outdir, Path(src).stem + ".pdf")
    if os.path.exists(out):
        return out
    pdfs = sorted(glob.glob(os.path.join(outdir, "*.pdf")), key=os.path.getmtime)
    return pdfs[-1] if pdfs else None


def _email_to_html(src):
    """Render an email (.eml/.msg) to standalone HTML (headers + body)."""
    import html as _html
    def esc(x): return _html.escape(x or "")
    hdr, body_html = {}, None
    ext = Path(src).suffix.lower()
    if ext == ".msg":
        try:
            import extract_msg
            m = extract_msg.Message(src)
            hdr = {"From": m.sender, "To": m.to, "Cc": getattr(m, "cc", None),
                   "Date": str(m.date or ""), "Subject": m.subject}
            b = getattr(m, "htmlBody", None)
            if isinstance(b, bytes):
                b = b.decode("utf-8", "replace")
            body_html = b or ("<pre>%s</pre>" % esc(m.body or ""))
        except Exception:
            body_html = None
    else:
        import email
        from email import policy
        with open(src, "rb") as f:
            msg = email.message_from_binary_file(f, policy=policy.default)
        hdr = {"From": msg.get("From"), "To": msg.get("To"), "Cc": msg.get("Cc"),
               "Date": msg.get("Date"), "Subject": msg.get("Subject")}
        try:
            bp = msg.get_body(preferencelist=("html", "plain"))
            if bp is not None:
                content = bp.get_content()
                body_html = ("<pre>%s</pre>" % esc(content)
                             if bp.get_content_type() == "text/plain" else content)
        except Exception:
            body_html = None
    if body_html is None:
        body_html = "<pre>(no body)</pre>"
    head = "".join("<tr><td style='font-weight:bold;padding-right:8px;vertical-align:top'>%s</td>"
                   "<td>%s</td></tr>" % (k, esc(v)) for k, v in hdr.items() if v)
    return ("<html><head><meta charset='utf-8'></head><body>"
            "<table style='font-family:sans-serif;font-size:11pt;margin-bottom:12px'>%s</table>"
            "<hr/>%s</body></html>") % (head, body_html)


def _render_to_pdf(abs_path, doc_type, out_pdf):
    """office/html/email/text -> PDF via LibreOffice."""
    import tempfile, shutil as _sh
    outdir = os.path.dirname(out_pdf)
    os.makedirs(outdir, exist_ok=True)
    ext = Path(abs_path).suffix.lower()
    src, tmp_html = abs_path, None
    if doc_type == "email" or ext in (".eml", ".msg"):
        fd, tmp_html = tempfile.mkstemp(suffix=".html"); os.close(fd)
        with open(tmp_html, "w", encoding="utf-8") as f:
            f.write(_email_to_html(abs_path))
        src = tmp_html
    try:
        produced = _lo_convert(src, outdir)
        if produced and os.path.abspath(produced) != os.path.abspath(out_pdf):
            _sh.move(produced, out_pdf)
        return out_pdf if os.path.exists(out_pdf) else None
    finally:
        if tmp_html and os.path.exists(tmp_html):
            os.remove(tmp_html)


def _ocr_to_pdf(abs_path, doc_type, out_pdf):
    """image/scanned-pdf -> searchable PDF via tesseract (pdf config)."""
    import subprocess, tempfile
    os.makedirs(os.path.dirname(out_pdf), exist_ok=True)
    stem = out_pdf[:-4] if out_pdf.lower().endswith(".pdf") else out_pdf
    ext = Path(abs_path).suffix.lower()
    if doc_type == "image" or ext in IMAGE_EXTS:
        subprocess.run(["tesseract", abs_path, stem, "pdf"], capture_output=True, timeout=300)
        return out_pdf if os.path.exists(out_pdf) else None
    import fitz
    src = fitz.open(abs_path)
    merged = fitz.open()
    try:
        with tempfile.TemporaryDirectory() as td:
            for i in range(src.page_count):
                pix = src.load_page(i).get_pixmap(dpi=250)
                png = os.path.join(td, "p%d.png" % i); pix.save(png)
                pstem = os.path.join(td, "p%d" % i)
                subprocess.run(["tesseract", png, pstem, "pdf"], capture_output=True, timeout=300)
                ppdf = pstem + ".pdf"
                if os.path.exists(ppdf):
                    sub = fitz.open(ppdf); merged.insert_pdf(sub); sub.close()
            if merged.page_count:
                merged.save(out_pdf)
    finally:
        merged.close(); src.close()
    return out_pdf if os.path.exists(out_pdf) else None


def _select_pending(cur, tenant, collection_id, status, limit):
    sql = ("SELECT d.id::text, d.doc_type, d.native_path, d.file_hash "
           "FROM ediscovery_documents d "
           "WHERE TRIM(d.tenant_id)=%s AND d.collection_id=CAST(%s AS uuid) "
           "  AND d.processing_status=%s AND d.native_path IS NOT NULL "
           "ORDER BY d.family_id NULLS FIRST, d.is_attachment")
    if limit:
        sql += " LIMIT %d" % int(limit)
    cur.execute(sql, (tenant, str(collection_id), status))
    return cur.fetchall()


def process_lane(tenant_id, collection_id, lane, redo=False, limit=0, dry_run=False) -> dict:
    """Render or OCR lane: build a PDF rendition, tokenize it (canonical+boxes),
    flip the unit to geometry_done so the segmenter (gated on tokens) picks it up."""
    from modules.ediscovery.services.geometry_extraction import extract_pdf_with_geometry
    assert lane in ("render", "ocr")
    tenant = tenant_id.strip()
    worker_id = os.environ.get("HOSTNAME", lane)
    status = "render_pending" if lane == "render" else "ocr_pending"
    rendition = RENDER_RENDITION if lane == "render" else OCR_RENDITION
    s = {"lane": lane, "tokenized": 0, "empty": 0, "errors": 0, "tokens": 0, "dry_run": dry_run}
    conn = _connect()
    try:
        cur = conn.cursor()
        coll = _resolve_collection(cur, tenant, collection_id)
        root = Path(coll["storage_path"])
        outdir = str(root / "working" / lane)
        units = _select_pending(cur, tenant, collection_id, status, limit)
        logger.info("%s lane %s: %d pending unit(s) dry=%s", lane, collection_id, len(units), dry_run)
        for (doc_id, doc_type, native_path, file_hash) in units:
            t0 = time.time()
            try:
                abs_path = root / native_path
                if not abs_path.exists():
                    raise FileNotFoundError(str(abs_path))
                out_pdf = os.path.join(outdir, "%s.pdf" % doc_id)
                made = (_render_to_pdf(str(abs_path), doc_type, out_pdf) if lane == "render"
                        else _ocr_to_pdf(str(abs_path), doc_type, out_pdf))
                if not made or not os.path.exists(out_pdf):
                    raise RuntimeError("%s produced no pdf" % lane)
                result = extract_pdf_with_geometry(out_pdf)
                dur = int((time.time() - t0) * 1000)
                if result is None or not (result.canonical_text or "").strip():
                    if not dry_run:
                        _mark_pending(cur, doc_id, "geometry_empty")
                        _stage(cur, tenant, doc_id, collection_id, lane, "done",
                               worker_id=worker_id, input_hash=file_hash, duration_ms=dur)
                        conn.commit()
                    s["empty"] += 1
                    continue
                if dry_run:
                    logger.info("  [DRY] %s %s tokens=%d", doc_id[:8], lane, len(result.tokens))
                    s["tokenized"] += 1
                    continue
                n_tok, _, _ = _persist_geometry(cur, tenant, doc_id, rendition, result,
                                                corpus="ediscovery", write_canonical=True)
                _stage(cur, tenant, doc_id, collection_id, lane, "done",
                       worker_id=worker_id, input_hash=file_hash, duration_ms=dur)
                conn.commit()
                s["tokenized"] += 1
                s["tokens"] += n_tok
            except Exception as e:
                s["errors"] += 1
                conn.rollback()
                _stage(cur, tenant, doc_id, collection_id, lane, "failed",
                       worker_id=worker_id, input_hash=file_hash,
                       error_class=type(e).__name__, error_message=str(e))
                conn.commit()
                logger.exception("%s lane unit %s failed", lane, doc_id)
        return s
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="ediscovery", choices=["ediscovery", "dms"])
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", "986c0fee-1390-43bb-ad28-8cd1db6de53f"))
    ap.add_argument("--collection", default=None, help="(ediscovery) collection id")
    ap.add_argument("--path-like", default=None, help="(dms) ILIKE filter on file_path")
    ap.add_argument("--stage", default="geometry", choices=["geometry", "render", "ocr"],
                    help="(ediscovery) which geometry-substrate stage to run")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    import json
    if args.corpus == "dms":
        out = geometry_intake_dms(args.tenant, path_like=args.path_like,
                                  redo=args.redo, limit=args.limit, dry_run=args.dry_run)
    else:
        if not args.collection:
            ap.error("--collection required for --corpus ediscovery")
        if args.stage in ("render", "ocr"):
            out = process_lane(args.tenant, args.collection, args.stage,
                               redo=args.redo, limit=args.limit, dry_run=args.dry_run)
        else:
            out = geometry_intake(args.tenant, args.collection,
                                  redo=args.redo, limit=args.limit, dry_run=args.dry_run)
    logger.info("DONE %s", json.dumps(out))


if __name__ == "__main__":
    main()
