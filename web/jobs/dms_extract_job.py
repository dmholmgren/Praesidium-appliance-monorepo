"""
jobs/dms_extract_job.py
RQ background job: Text Extraction — Two-Pass Architecture

Pass 1 (fast):  Extract text from native-text formats (pdf, docx, txt, csv,
                xlsx, xls, msg, eml, rtf). Sub-second per file. Runs serially.
Pass 2 (OCR):   Extract text from image files (jpg, jpeg, png, tif, tiff)
                using pytesseract via a multiprocessing pool. CPU-bound, ~2-10s
                per image. Pool size adapts to available cores.

Entry points:
  run_extract(job_id)        — Full pipeline: fast pass + OCR pass. Manual trigger.
  run_extract_fast(job_id)   — Fast pass only. Auto-chains OCR + parse on completion.
  run_extract_ocr(job_id)    — OCR pass only. Auto-chains parse on completion.
  run_extract_single(tenant_id, file_path, doc_id)
                             — Single-file extraction for upload triggers.

Chain behavior: fast → ocr → parse_analyze (all auto-enqueued).

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import email
import logging
import multiprocessing
import os
import uuid as _uuid

import psycopg2
import psycopg2.extras

log = logging.getLogger("praesidium.jobs.dms_extract_job")

BATCH_SIZE = 200
MAX_TEXT_LEN = 200000
OCR_POOL_SIZE = min(multiprocessing.cpu_count() - 1, 24) or 1
OCR_TIMEOUT_SECONDS = 120  # per-file timeout for OCR


# ═══════════════════════════════════════════════════════════════════════════════
# Database connection helper
# ═══════════════════════════════════════════════════════════════════════════════

def _get_db_conn():
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "")
    at_idx = url.rfind("@")
    userinfo = url[:at_idx]
    hostinfo = url[at_idx + 1:]
    user, password = userinfo.split(":", 1)
    host_port, dbname = hostinfo.split("/", 1)
    host, port = host_port.rsplit(":", 1)
    return psycopg2.connect(
        host=host, port=int(port),
        dbname=dbname.split("?")[0],
        user=user, password=password,
    )


def _is_cancelled(cur, job_id):
    cur.execute("SELECT status FROM dms_scan_jobs WHERE id = %s", (job_id,))
    row = cur.fetchone()
    return row and row["status"] == "cancelled"


# ═══════════════════════════════════════════════════════════════════════════════
# Extractors — Native Text Formats
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_pdf(filepath):
    try:
        import fitz
        doc = fitz.open(filepath)
        pages = []
        for page in doc:
            text = page.get_text()
            if text and text.strip():
                pages.append(text.strip())
        doc.close()
        if pages:
            return "\n\n".join(pages)
    except Exception:
        pass
    try:
        import pdfplumber
        with pdfplumber.open(filepath) as pdf:
            pages = []
            for page in pdf.pages:
                text = page.extract_text()
                if text and text.strip():
                    pages.append(text.strip())
            if pages:
                return "\n\n".join(pages)
    except Exception:
        pass
    return ""


def _extract_text_file(filepath):
    try:
        with open(filepath, "rb") as f:
            raw = f.read(MAX_TEXT_LEN + 1000)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
        try:
            return raw.decode("latin-1")
        except UnicodeDecodeError:
            pass
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_docx(filepath):
    try:
        from docx import Document
        doc = Document(filepath)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception:
        return ""


def _extract_xlsx(filepath):
    try:
        from openpyxl import load_workbook
        wb = load_workbook(filepath, read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                vals = [str(c) for c in row if c is not None]
                if vals:
                    parts.append("\t".join(vals))
        wb.close()
        return "\n".join(parts)
    except Exception:
        return ""


def _extract_xls(filepath):
    try:
        import xlrd
        wb = xlrd.open_workbook(filepath)
        parts = []
        for ws in wb.sheets():
            for rx in range(ws.nrows):
                vals = [str(ws.cell_value(rx, cx)) for cx in range(ws.ncols) if ws.cell_value(rx, cx)]
                if vals:
                    parts.append("\t".join(vals))
        return "\n".join(parts)
    except Exception:
        return _extract_xlsx(filepath)


def _extract_msg(filepath):
    try:
        import extract_msg
        msg = extract_msg.Message(filepath)
        parts = []
        if msg.subject: parts.append("Subject: " + msg.subject)
        if msg.sender: parts.append("From: " + msg.sender)
        if msg.to: parts.append("To: " + msg.to)
        if msg.date: parts.append("Date: " + str(msg.date))
        if msg.body: parts.append(msg.body)
        msg.close()
        return "\n".join(parts)
    except Exception:
        return ""


def _extract_eml(filepath):
    try:
        with open(filepath, "rb") as f:
            msg = email.message_from_bytes(f.read())
        parts = []
        if msg["subject"]: parts.append("Subject: " + msg["subject"])
        if msg["from"]: parts.append("From: " + msg["from"])
        if msg["to"]: parts.append("To: " + msg["to"])
        if msg["date"]: parts.append("Date: " + msg["date"])
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    parts.append(payload.decode("utf-8", errors="replace"))
        return "\n".join(parts)
    except Exception:
        return ""


def _extract_rtf(filepath):
    try:
        with open(filepath, "rb") as f:
            raw = f.read()
        try:
            from striprtf.striprtf import rtf_to_text
            return rtf_to_text(raw.decode("utf-8", errors="replace"))
        except ImportError:
            import re
            text = raw.decode("utf-8", errors="replace")
            text = re.sub(r'\\[a-z]+\d*\s?', '', text)
            text = re.sub(r'[{}]', '', text)
            return text.strip()
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# Extractor — OCR (images) — designed to run in a subprocess
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_image_ocr(filepath):
    """OCR extraction. Runs in worker subprocess via multiprocessing pool."""
    try:
        from PIL import Image
        import pytesseract
        img = Image.open(filepath)
        # Downsample very large images to avoid memory/time blowout
        max_dim = 4000
        if img.width > max_dim or img.height > max_dim:
            ratio = min(max_dim / img.width, max_dim / img.height)
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        text = pytesseract.image_to_string(img)
        img.close()
        return text.strip() if text else ""
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# Extension → extractor mapping
# ═══════════════════════════════════════════════════════════════════════════════

FAST_EXTRACTORS = {
    "pdf": _extract_pdf, "txt": _extract_text_file, "csv": _extract_text_file,
    "log": _extract_text_file, "docx": _extract_docx, "xlsx": _extract_xlsx,
    "xls": _extract_xls, "msg": _extract_msg, "eml": _extract_eml,
    "rtf": _extract_rtf,
}
FAST_EXTS = list(FAST_EXTRACTORS.keys())

OCR_EXTRACTORS = {
    "jpg": _extract_image_ocr, "jpeg": _extract_image_ocr,
    "png": _extract_image_ocr, "tif": _extract_image_ocr,
    "tiff": _extract_image_ocr,
}
OCR_EXTS = list(OCR_EXTRACTORS.keys())

ALL_EXTRACTORS = {**FAST_EXTRACTORS, **OCR_EXTRACTORS}
SUPPORTED_EXTS = list(ALL_EXTRACTORS.keys())


def _get_ext(filepath):
    idx = filepath.rfind(".")
    if idx < 0:
        return ""
    return filepath[idx + 1:].lower().strip()


def _extract(filepath, extractors=None):
    """Extract text from a file using the appropriate extractor."""
    ext = _get_ext(filepath)
    if extractors is None:
        extractors = ALL_EXTRACTORS
    fn = extractors.get(ext)
    if not fn:
        return ""
    try:
        text = fn(filepath)
        if text and len(text) > MAX_TEXT_LEN:
            text = text[:MAX_TEXT_LEN]
        return text
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# SQL helpers
# ═══════════════════════════════════════════════════════════════════════════════

EXT_MATCH_SQL = "lower(reverse(split_part(reverse(file_path), '.', 1))) = ANY(%s)"


def _get_rq_queue():
    from redis import Redis
    from rq import Queue
    redis_url = os.environ.get("REDIS_URL", "redis://praesidium-redis:6379/0")
    parts = redis_url.replace("redis://", "").split("/")
    host_port = parts[0].split(":")
    host = host_port[0]
    port = int(host_port[1]) if len(host_port) > 1 else 6379
    db = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    conn = Redis(host=host, port=port, db=db)
    return Queue("default", connection=conn)


def _chain_next_job(tenant_id, next_job_type, next_func):
    """Create a dms_scan_jobs row and enqueue the next pipeline stage."""
    conn = _get_db_conn()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # Skip if same type already queued/running
        cur.execute(
            "SELECT id FROM dms_scan_jobs "
            "WHERE TRIM(tenant_id)=%s AND job_type=%s AND status IN ('queued','running') "
            "LIMIT 1",
            (tenant_id, next_job_type)
        )
        if cur.fetchone():
            log.info("Chain: %s already queued/running for tenant %s — skipping",
                     next_job_type, tenant_id[:8])
            return
        job_id = str(_uuid.uuid4())
        cur.execute(
            "INSERT INTO dms_scan_jobs (id, tenant_id, job_type, status, queued_at) "
            "VALUES (CAST(%s AS uuid), %s, %s, 'queued', NOW())",
            (job_id, tenant_id, next_job_type)
        )
        timeout = {"extract_ocr": 86400, "parse": 14400}.get(next_job_type, 7200)
        q = _get_rq_queue()
        q.enqueue(next_func, job_id, job_timeout=timeout, result_ttl=86400)
        log.info("Chained %s job %s for tenant %s", next_job_type, job_id, tenant_id[:8])
    except Exception as e:
        log.error("Chain %s failed: %s", next_job_type, e)
    finally:
        cur.close()
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Worker function for OCR multiprocessing pool
# ═══════════════════════════════════════════════════════════════════════════════

def _ocr_worker(args):
    """Process a single image file. Called in subprocess.
    Returns (doc_id, text, error_str_or_None)."""
    doc_id, filepath = args
    try:
        text = _extract_image_ocr(filepath)
        return (doc_id, text, None)
    except Exception as e:
        return (doc_id, "", str(e)[:200])


# ═══════════════════════════════════════════════════════════════════════════════
# Pass 1: Fast extraction (native text formats)
# ═══════════════════════════════════════════════════════════════════════════════

def _run_batch_extract(job_id, ext_list, extractors, pass_name):
    """Generic batch extractor for a set of file extensions."""
    conn = _get_db_conn()
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute("SELECT tenant_id, status FROM dms_scan_jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        if not row:
            log.error("%s job %s not found", pass_name, job_id)
            return None
        tenant_id = row["tenant_id"].strip()
        if row["status"] not in ("queued", "running"):
            return tenant_id

        cur.execute("UPDATE dms_scan_jobs SET status='running', started_at=NOW() WHERE id=%s", (job_id,))
        conn.commit()

        # Count pending
        cur.execute(
            "SELECT COUNT(*) as cnt FROM dms_documents "
            "WHERE TRIM(tenant_id) = %s "
            "AND (content_text IS NULL OR content_text = '') "
            "AND extraction_status = 'pending' "
            "AND " + EXT_MATCH_SQL,
            (tenant_id, ext_list)
        )
        total = cur.fetchone()["cnt"]

        cur.execute("UPDATE dms_scan_jobs SET files_discovered=%s WHERE id=%s", (total, job_id))
        conn.commit()

        if total == 0:
            cur.execute(
                "UPDATE dms_scan_jobs SET status='complete', completed_at=NOW(), "
                "files_discovered=0, files_indexed=0, files_skipped=0 WHERE id=%s",
                (job_id,)
            )
            conn.commit()
            log.info("%s job %s — nothing to process", pass_name, job_id)
            return tenant_id

        extracted = 0
        skipped = 0
        processed = 0
        last_id = '00000000-0000-0000-0000-000000000000'

        while processed < total:
            if _is_cancelled(cur, job_id):
                log.info("%s job %s cancelled after %d files", pass_name, job_id, processed)
                return tenant_id

            cur.execute(
                "SELECT id::text as doc_id, file_path FROM dms_documents "
                "WHERE TRIM(tenant_id) = %s "
                "AND (content_text IS NULL OR content_text = '') "
                "AND extraction_status = 'pending' "
                "AND " + EXT_MATCH_SQL + " "
                "AND id > CAST(%s AS uuid) "
                "ORDER BY id LIMIT %s",
                (tenant_id, ext_list, last_id, BATCH_SIZE)
            )
            rows = cur.fetchall()
            if not rows:
                break

            for doc in rows:
                doc_id = doc["doc_id"]
                filepath = doc["file_path"]
                last_id = doc_id
                processed += 1

                if not os.path.isfile(filepath):
                    cur.execute(
                        "UPDATE dms_documents SET extraction_status='failed', updated_at=NOW() "
                        "WHERE id = CAST(%s AS uuid)", (doc_id,)
                    )
                    skipped += 1
                    continue

                try:
                    text = _extract(filepath, extractors)
                    if text and text.strip():
                        cur.execute(
                            "UPDATE dms_documents SET content_text=%s, extraction_status='complete', "
                            "updated_at=NOW() WHERE id = CAST(%s AS uuid)",
                            (text[:MAX_TEXT_LEN], doc_id)
                        )
                        extracted += 1
                    else:
                        cur.execute(
                            "UPDATE dms_documents SET extraction_status='no_text', updated_at=NOW() "
                            "WHERE id = CAST(%s AS uuid)", (doc_id,)
                        )
                        skipped += 1
                except Exception as e:
                    log.warning("%s failed for %s: %s", pass_name, filepath, str(e)[:200])
                    cur.execute(
                        "UPDATE dms_documents SET extraction_status='failed', updated_at=NOW() "
                        "WHERE id = CAST(%s AS uuid)", (doc_id,)
                    )
                    skipped += 1

            conn.commit()
            cur.execute(
                "UPDATE dms_scan_jobs SET files_indexed=%s, files_skipped=%s WHERE id=%s",
                (extracted, skipped, job_id)
            )
            conn.commit()

            if processed % 500 == 0:
                log.info("%s %s: %d/%d done, %d extracted, %d skipped",
                         pass_name, job_id, processed, total, extracted, skipped)

        cur.execute(
            "UPDATE dms_scan_jobs SET status='complete', completed_at=NOW(), "
            "files_discovered=%s, files_indexed=%s, files_skipped=%s WHERE id=%s",
            (total, extracted, skipped, job_id)
        )
        conn.commit()
        log.info("%s job %s complete — %d extracted, %d skipped of %d",
                 pass_name, job_id, extracted, skipped, total)
        return tenant_id

    except Exception as exc:
        log.exception("%s job %s failed: %s", pass_name, job_id, exc)
        try:
            conn.rollback()
            cur.execute(
                "UPDATE dms_scan_jobs SET status='failed', completed_at=NOW(), error_message=%s WHERE id=%s",
                (str(exc)[:500], job_id)
            )
            conn.commit()
        except Exception:
            pass
        return None
    finally:
        cur.close()
        conn.close()


def run_extract_fast(job_id):
    """Pass 1: Extract native-text files only (fast). Auto-chains parse (not OCR).
    Pipeline: extract_fast → parse → extract_ocr → parse (second pass)."""
    tenant_id = _run_batch_extract(job_id, FAST_EXTS, FAST_EXTRACTORS, "FastExtract")
    if tenant_id:
        _chain_next_job(tenant_id, "parse",
                        "jobs.dms_parse_analyze_job.run_parse_analyze")


# ═══════════════════════════════════════════════════════════════════════════════
# Pass 2: OCR extraction (image files) — parallel via multiprocessing
# ═══════════════════════════════════════════════════════════════════════════════

def run_extract_ocr(job_id):
    """Pass 2: OCR image files using a multiprocessing pool. Auto-chains parse."""
    conn = _get_db_conn()
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    tenant_id = None
    try:
        cur.execute("SELECT tenant_id, status FROM dms_scan_jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        if not row:
            log.error("OCR job %s not found", job_id)
            return
        tenant_id = row["tenant_id"].strip()
        if row["status"] not in ("queued", "running"):
            return

        cur.execute("UPDATE dms_scan_jobs SET status='running', started_at=NOW() WHERE id=%s", (job_id,))
        conn.commit()

        # Count pending OCR files
        cur.execute(
            "SELECT COUNT(*) as cnt FROM dms_documents "
            "WHERE TRIM(tenant_id) = %s "
            "AND (content_text IS NULL OR content_text = '') "
            "AND extraction_status = 'pending' "
            "AND " + EXT_MATCH_SQL,
            (tenant_id, OCR_EXTS)
        )
        total = cur.fetchone()["cnt"]

        cur.execute("UPDATE dms_scan_jobs SET files_discovered=%s WHERE id=%s", (total, job_id))
        conn.commit()

        if total == 0:
            cur.execute(
                "UPDATE dms_scan_jobs SET status='complete', completed_at=NOW(), "
                "files_discovered=0, files_indexed=0, files_skipped=0 WHERE id=%s",
                (job_id,)
            )
            conn.commit()
            log.info("OCR job %s — no images to process", job_id)
            if tenant_id:
                _chain_next_job(tenant_id, "parse",
                                "jobs.dms_parse_analyze_job.run_parse_analyze")
            return

        extracted = 0
        skipped = 0
        processed = 0
        last_id = '00000000-0000-0000-0000-000000000000'
        pool_size = OCR_POOL_SIZE
        log.info("OCR job %s starting — %d images, pool size %d", job_id, total, pool_size)

        pool = multiprocessing.Pool(processes=pool_size)

        try:
            while processed < total:
                if _is_cancelled(cur, job_id):
                    log.info("OCR job %s cancelled after %d files", job_id, processed)
                    break

                cur.execute(
                    "SELECT id::text as doc_id, file_path FROM dms_documents "
                    "WHERE TRIM(tenant_id) = %s "
                    "AND (content_text IS NULL OR content_text = '') "
                    "AND extraction_status = 'pending' "
                    "AND " + EXT_MATCH_SQL + " "
                    "AND id > CAST(%s AS uuid) "
                    "ORDER BY id LIMIT %s",
                    (tenant_id, OCR_EXTS, last_id, BATCH_SIZE)
                )
                rows = cur.fetchall()
                if not rows:
                    break

                # Split into files that exist vs don't exist
                work_items = []
                for doc in rows:
                    doc_id = doc["doc_id"]
                    filepath = doc["file_path"]
                    last_id = doc_id
                    processed += 1

                    if not os.path.isfile(filepath):
                        cur.execute(
                            "UPDATE dms_documents SET extraction_status='failed', updated_at=NOW() "
                            "WHERE id = CAST(%s AS uuid)", (doc_id,)
                        )
                        skipped += 1
                    else:
                        work_items.append((doc_id, filepath))

                # Process batch through multiprocessing pool
                if work_items:
                    results = pool.map(_ocr_worker, work_items, chunksize=4)
                    for doc_id, text, err in results:
                        if err:
                            log.warning("OCR failed for %s: %s", doc_id, err)
                            cur.execute(
                                "UPDATE dms_documents SET extraction_status='failed', updated_at=NOW() "
                                "WHERE id = CAST(%s AS uuid)", (doc_id,)
                            )
                            skipped += 1
                        elif text and text.strip():
                            cur.execute(
                                "UPDATE dms_documents SET content_text=%s, extraction_status='complete', "
                                "updated_at=NOW() WHERE id = CAST(%s AS uuid)",
                                (text[:MAX_TEXT_LEN], doc_id)
                            )
                            extracted += 1
                        else:
                            cur.execute(
                                "UPDATE dms_documents SET extraction_status='no_text', updated_at=NOW() "
                                "WHERE id = CAST(%s AS uuid)", (doc_id,)
                            )
                            skipped += 1

                conn.commit()
                cur.execute(
                    "UPDATE dms_scan_jobs SET files_indexed=%s, files_skipped=%s WHERE id=%s",
                    (extracted, skipped, job_id)
                )
                conn.commit()

                if processed % 500 == 0:
                    log.info("OCR %s: %d/%d done, %d extracted, %d skipped",
                             job_id, processed, total, extracted, skipped)

        finally:
            pool.close()
            pool.join()

        cur.execute(
            "UPDATE dms_scan_jobs SET status='complete', completed_at=NOW(), "
            "files_discovered=%s, files_indexed=%s, files_skipped=%s WHERE id=%s",
            (total, extracted, skipped, job_id)
        )
        conn.commit()
        log.info("OCR job %s complete — %d extracted, %d skipped of %d",
                 job_id, extracted, skipped, total)

    except Exception as exc:
        log.exception("OCR job %s failed: %s", job_id, exc)
        try:
            conn.rollback()
            cur.execute(
                "UPDATE dms_scan_jobs SET status='failed', completed_at=NOW(), error_message=%s WHERE id=%s",
                (str(exc)[:500], job_id)
            )
            conn.commit()
        except Exception:
            pass
    finally:
        cur.close()
        conn.close()

    # Chain: parse+analyze
    if tenant_id:
        _chain_next_job(tenant_id, "parse",
                        "jobs.dms_parse_analyze_job.run_parse_analyze")


# ═══════════════════════════════════════════════════════════════════════════════
# Full pipeline: fast + OCR (manual trigger, runs both passes in one job)
# ═══════════════════════════════════════════════════════════════════════════════

def run_extract(job_id):
    """Full extraction: fast pass then OCR pass, single dms_scan_jobs row.
    Does NOT auto-chain parse — use run_extract_fast for auto-chain behavior."""
    conn = _get_db_conn()
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute("SELECT tenant_id, status FROM dms_scan_jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        if not row:
            log.error("Extract job %s not found", job_id)
            return
        tenant_id = row["tenant_id"].strip()
        if row["status"] not in ("queued", "running"):
            return

        cur.execute("UPDATE dms_scan_jobs SET status='running', started_at=NOW() WHERE id=%s", (job_id,))
        conn.commit()

        # Count ALL pending
        cur.execute(
            "SELECT COUNT(*) as cnt FROM dms_documents "
            "WHERE TRIM(tenant_id) = %s "
            "AND (content_text IS NULL OR content_text = '') "
            "AND extraction_status = 'pending' "
            "AND " + EXT_MATCH_SQL,
            (tenant_id, SUPPORTED_EXTS)
        )
        total = cur.fetchone()["cnt"]
        cur.execute("UPDATE dms_scan_jobs SET files_discovered=%s WHERE id=%s", (total, job_id))
        conn.commit()

        if total == 0:
            cur.execute(
                "UPDATE dms_scan_jobs SET status='complete', completed_at=NOW(), "
                "files_discovered=0, files_indexed=0, files_skipped=0 WHERE id=%s",
                (job_id,)
            )
            conn.commit()
            return

        extracted = 0
        skipped = 0
        processed = 0
        last_id = '00000000-0000-0000-0000-000000000000'

        # ── Phase 1: Fast extraction ─────────────────────────────────
        log.info("Extract %s Phase 1: fast text extraction", job_id)
        while True:
            if _is_cancelled(cur, job_id):
                return

            cur.execute(
                "SELECT id::text as doc_id, file_path FROM dms_documents "
                "WHERE TRIM(tenant_id) = %s "
                "AND (content_text IS NULL OR content_text = '') "
                "AND extraction_status = 'pending' "
                "AND " + EXT_MATCH_SQL + " "
                "AND id > CAST(%s AS uuid) "
                "ORDER BY id LIMIT %s",
                (tenant_id, FAST_EXTS, last_id, BATCH_SIZE)
            )
            rows = cur.fetchall()
            if not rows:
                break

            for doc in rows:
                doc_id = doc["doc_id"]
                filepath = doc["file_path"]
                last_id = doc_id
                processed += 1

                if not os.path.isfile(filepath):
                    cur.execute(
                        "UPDATE dms_documents SET extraction_status='failed', updated_at=NOW() "
                        "WHERE id = CAST(%s AS uuid)", (doc_id,)
                    )
                    skipped += 1
                    continue

                try:
                    text = _extract(filepath, FAST_EXTRACTORS)
                    if text and text.strip():
                        cur.execute(
                            "UPDATE dms_documents SET content_text=%s, extraction_status='complete', "
                            "updated_at=NOW() WHERE id = CAST(%s AS uuid)",
                            (text[:MAX_TEXT_LEN], doc_id)
                        )
                        extracted += 1
                    else:
                        cur.execute(
                            "UPDATE dms_documents SET extraction_status='no_text', updated_at=NOW() "
                            "WHERE id = CAST(%s AS uuid)", (doc_id,)
                        )
                        skipped += 1
                except Exception as e:
                    cur.execute(
                        "UPDATE dms_documents SET extraction_status='failed', updated_at=NOW() "
                        "WHERE id = CAST(%s AS uuid)", (doc_id,)
                    )
                    skipped += 1

            conn.commit()
            cur.execute(
                "UPDATE dms_scan_jobs SET files_indexed=%s, files_skipped=%s WHERE id=%s",
                (extracted, skipped, job_id)
            )
            conn.commit()

        log.info("Extract %s Phase 1 complete: %d extracted, %d skipped", job_id, extracted, skipped)

        # ── Phase 2: OCR extraction ──────────────────────────────────
        log.info("Extract %s Phase 2: OCR image extraction (pool=%d)", job_id, OCR_POOL_SIZE)
        last_id = '00000000-0000-0000-0000-000000000000'

        pool = multiprocessing.Pool(processes=OCR_POOL_SIZE)
        try:
            while True:
                if _is_cancelled(cur, job_id):
                    break

                cur.execute(
                    "SELECT id::text as doc_id, file_path FROM dms_documents "
                    "WHERE TRIM(tenant_id) = %s "
                    "AND (content_text IS NULL OR content_text = '') "
                    "AND extraction_status = 'pending' "
                    "AND " + EXT_MATCH_SQL + " "
                    "AND id > CAST(%s AS uuid) "
                    "ORDER BY id LIMIT %s",
                    (tenant_id, OCR_EXTS, last_id, BATCH_SIZE)
                )
                rows = cur.fetchall()
                if not rows:
                    break

                work_items = []
                for doc in rows:
                    doc_id = doc["doc_id"]
                    filepath = doc["file_path"]
                    last_id = doc_id
                    processed += 1

                    if not os.path.isfile(filepath):
                        cur.execute(
                            "UPDATE dms_documents SET extraction_status='failed', updated_at=NOW() "
                            "WHERE id = CAST(%s AS uuid)", (doc_id,)
                        )
                        skipped += 1
                    else:
                        work_items.append((doc_id, filepath))

                if work_items:
                    results = pool.map(_ocr_worker, work_items, chunksize=4)
                    for doc_id, text, err in results:
                        if err:
                            cur.execute(
                                "UPDATE dms_documents SET extraction_status='failed', updated_at=NOW() "
                                "WHERE id = CAST(%s AS uuid)", (doc_id,)
                            )
                            skipped += 1
                        elif text and text.strip():
                            cur.execute(
                                "UPDATE dms_documents SET content_text=%s, extraction_status='complete', "
                                "updated_at=NOW() WHERE id = CAST(%s AS uuid)",
                                (text[:MAX_TEXT_LEN], doc_id)
                            )
                            extracted += 1
                        else:
                            cur.execute(
                                "UPDATE dms_documents SET extraction_status='no_text', updated_at=NOW() "
                                "WHERE id = CAST(%s AS uuid)", (doc_id,)
                            )
                            skipped += 1

                conn.commit()
                cur.execute(
                    "UPDATE dms_scan_jobs SET files_indexed=%s, files_skipped=%s WHERE id=%s",
                    (extracted, skipped, job_id)
                )
                conn.commit()

                if processed % 500 == 0:
                    log.info("Extract %s: %d/%d done, %d extracted, %d skipped",
                             job_id, processed, total, extracted, skipped)
        finally:
            pool.close()
            pool.join()

        cur.execute(
            "UPDATE dms_scan_jobs SET status='complete', completed_at=NOW(), "
            "files_discovered=%s, files_indexed=%s, files_skipped=%s WHERE id=%s",
            (total, extracted, skipped, job_id)
        )
        conn.commit()
        log.info("Extract job %s complete — %d extracted, %d skipped of %d",
                 job_id, extracted, skipped, total)

    except Exception as exc:
        log.exception("Extract job %s failed: %s", job_id, exc)
        try:
            conn.rollback()
            cur.execute(
                "UPDATE dms_scan_jobs SET status='failed', completed_at=NOW(), error_message=%s WHERE id=%s",
                (str(exc)[:500], job_id)
            )
            conn.commit()
        except Exception:
            pass
    finally:
        cur.close()
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Single-file extraction (upload trigger)
# ═══════════════════════════════════════════════════════════════════════════════



def _run_intelligence_hook(tenant_id, dms_doc_id, file_path, text):
    """Post-extraction intelligence hook. Resolves matter from folder path,
    runs Claude extraction, writes structured primitives to DB.
    
    Sync wrapper around the async matter_extract functions.
    Only fires if the document can be mapped to a matter via matter_folders.disk_root.
    """
    import asyncio
    
    conn = _get_db_conn()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    try:
        # Find the matter this file belongs to via matter_folders.disk_root
        cur.execute(
            "SELECT mf.matter_id::text, m.matter_type "
            "FROM matter_folders mf "
            "JOIN matters m ON m.id = mf.matter_id "
            "WHERE TRIM(mf.tenant_id) = %s "
            "AND mf.disk_root IS NOT NULL AND mf.disk_root != '' "
            "AND %s LIKE CONCAT(mf.disk_root, '%%') "
            "LIMIT 1",
            (tenant_id, file_path)
        )
        row = cur.fetchone()
        if not row:
            return  # Not linked to a matter — skip
        
        matter_id = row["matter_id"]
        matter_type = row["matter_type"] or "transactional"
        
        log.info("Intelligence hook: %s → matter %s (%s)",
                 file_path.rsplit("/", 1)[-1], matter_id[:8], matter_type)
        
    finally:
        cur.close()
        conn.close()
    
    # Run the async extraction in a new event loop
    async def _do_extract():
        from modules.intelligence.matter_extract import (
            _get_api_key, _call_claude, _write_matter_extraction,
            _write_litigation_extraction,
            SYSTEM_MATTER_PROVISION, SYSTEM_LITIGATION_EXTRACT,
        )
        
        api_key = await _get_api_key(tenant_id)
        if not api_key:
            log.warning("Intelligence hook: no API key for tenant %s", tenant_id[:8])
            return
        
        filename = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
        header = f"[Document: {filename}]\n\n"
        
        is_litigation = matter_type == "litigation"
        prompt = SYSTEM_LITIGATION_EXTRACT if is_litigation else SYSTEM_MATTER_PROVISION
        
        result = await _call_claude(api_key, prompt, header + text[:50000])
        if not result:
            log.warning("Intelligence hook: Claude returned None for %s", filename)
            return
        
        if is_litigation:
            stats = await _write_litigation_extraction(
                tenant_id, matter_id, None, result, 0)
        else:
            stats = await _write_matter_extraction(
                tenant_id, matter_id, None, result, 0)
        
        log.info("Intelligence hook: %s → %d contacts, %d deal_points, %d identifiers",
                 filename, stats.get("contacts", 0), stats.get("deal_points", 0),
                 stats.get("identifiers", 0))
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_do_extract())
        loop.close()
    except Exception as e:
        log.warning("Intelligence hook async failed: %s", str(e)[:200])


def run_extract_single(tenant_id, file_path, dms_doc_id):
    """Extract text from a single newly-uploaded file and update dms_documents.
    Also copies into documents.extracted_text if a matching documents row exists.
    Then pushes that single doc to Elasticsearch.
    Called as a lightweight RQ job on file upload — no dms_scan_jobs row needed."""
    conn = _get_db_conn()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        tenant_id = tenant_id.strip()
        ext = _get_ext(file_path)

        if ext not in ALL_EXTRACTORS:
            log.info("Single extract: unsupported ext '%s' for %s", ext, file_path)
            return

        if not os.path.isfile(file_path):
            log.warning("Single extract: file not found %s", file_path)
            return

        text = _extract(file_path, ALL_EXTRACTORS)

        if text and text.strip():
            cur.execute(
                "UPDATE dms_documents SET content_text=%s, extraction_status='complete', "
                "updated_at=NOW() WHERE id = CAST(%s AS uuid)",
                (text[:MAX_TEXT_LEN], dms_doc_id)
            )
        else:
            cur.execute(
                "UPDATE dms_documents SET extraction_status='no_text', updated_at=NOW() "
                "WHERE id = CAST(%s AS uuid)", (dms_doc_id,)
            )
            return  # nothing to propagate

        # Propagate to documents table
        cur.execute(
            "UPDATE documents SET extracted_text=%s, ocr_status='complete', updated_at=NOW() "
            "WHERE TRIM(tenant_id)=%s AND storage_path=%s "
            "AND (extracted_text IS NULL OR extracted_text = '')",
            (text[:MAX_TEXT_LEN], tenant_id, file_path)
        )

        # Push to ES
        try:
            cur.execute(
                "SELECT id::text AS doc_id, tenant_id, matter_id::text, "
                "filename, title, mime_type, document_type, file_size, "
                "storage_path, extracted_text, checksum, status, "
                "created_at, updated_at "
                "FROM documents WHERE TRIM(tenant_id)=%s AND storage_path=%s LIMIT 1",
                (tenant_id, file_path)
            )
            doc = cur.fetchone()
            if doc and doc.get("extracted_text"):
                from jobs.dms_parse_analyze_job import _get_es, _ensure_index, _es_bulk_index
                es = _get_es()
                _ensure_index(es)
                ok, err = _es_bulk_index(es, [doc], tenant_id)
                if ok:
                    log.info("Single extract: indexed %s into ES", file_path)
                if err:
                    log.warning("Single extract: ES index error for %s", file_path)
        except Exception as e:
            log.warning("Single extract: ES push failed for %s: %s", file_path, str(e)[:200])

        
        # ── Post-extraction hooks ──
        try:
            from modules.property.post_extraction_hook import run_property_extraction_hook
            run_property_extraction_hook(tenant_id, dms_doc_id, file_path, text)
        except Exception as hook_err:
            log.warning("Post-extraction hook failed for %s: %s", file_path, str(hook_err)[:200])

        # ── Matter intelligence extraction hook ──
        # Fires after text extraction on single-file uploads.
        # Resolves matter from disk_root in matter_folders, then runs Claude
        # extraction to populate role-constrained parties, GF numbers, identifiers.
        if text and len(text) > 500:
            try:
                _run_intelligence_hook(tenant_id, dms_doc_id, file_path, text)
            except Exception as intel_err:
                log.warning("Intelligence extraction hook failed for %s: %s",
                            file_path, str(intel_err)[:200])

        log.info("Single extract complete: %s (%d chars)", file_path, len(text))

    except Exception as exc:
        log.exception("Single extract failed for %s: %s", file_path, exc)
    finally:
        cur.close()
        conn.close()
