#!/usr/bin/env python3
"""
extract_text.py — Text extraction pipeline for dms_documents.
Reads files from local /mnt/praesidium, extracts text, writes to content_text.
No CIFS bridge, no network downloads. Everything is local NVMe.

Usage:
  python3 /opt/praesidium-web/jobs/extract_text.py --batch 500
  python3 /opt/praesidium-web/jobs/extract_text.py --batch 500 --workers 4
  python3 /opt/praesidium-web/jobs/extract_text.py --ocr-only --batch 200

Runs inside praesidium-web container:
  docker exec praesidium-web python3 /app/jobs/extract_text.py --batch 500
"""

import os
import sys
import time
import logging
import argparse
import hashlib
import subprocess
import tempfile
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("extract_text")

# ── DB connection ──────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")

def get_conn():
    dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    return psycopg2.connect(dsn)

# ── Text extraction by file type ───────────────────────────────────────────

def extract_pdf_text(path):
    """Extract text from PDF. Try pymupdf first; if thin, fall back to tesseract."""
    import fitz
    text = ""
    try:
        doc = fitz.open(path)
        pages = len(doc)
        for page in doc:
            text += page.get_text() + "\n"
        doc.close()
    except Exception as e:
        log.warning("pymupdf failed for %s: %s", path, e)
        return _tesseract_pdf(path), "ocr_complete"

    # Quality gate: if less than 50 chars per page on average, OCR it
    avg_chars = len(text.strip()) / max(pages, 1)
    if avg_chars < 50 and pages > 0:
        log.info("Thin PDF (%d chars/%d pages), OCR fallback: %s", len(text.strip()), pages, os.path.basename(path))
        ocr_text = _tesseract_pdf(path)
        if len(ocr_text.strip()) > len(text.strip()):
            return ocr_text, "ocr_complete"
    return text, "text_native"


def _tesseract_pdf(path):
    """OCR a PDF via pdftoppm + tesseract."""
    import pytesseract
    from PIL import Image
    text_parts = []
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            subprocess.run(
                ["pdftoppm", "-png", "-r", "300", path, os.path.join(tmpdir, "page")],
                check=True, capture_output=True, timeout=300,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.warning("pdftoppm failed for %s: %s", path, e)
            return ""
        for img_path in sorted(Path(tmpdir).glob("page-*.png")):
            try:
                img = Image.open(str(img_path))
                text_parts.append(pytesseract.image_to_string(img))
            except Exception as e:
                log.warning("tesseract failed on %s: %s", img_path, e)
    return "\n".join(text_parts)


def extract_docx_text(path):
    """Extract text from .docx."""
    import docx
    try:
        doc = docx.Document(path)
        return "\n".join(p.text for p in doc.paragraphs), "text_native"
    except Exception as e:
        log.warning("python-docx failed for %s: %s", path, e)
        return "", "failed"


def extract_doc_text(path):
    """Extract text from .doc via antiword."""
    try:
        result = subprocess.run(
            ["antiword", path], capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout, "text_native"
    except Exception as e:
        log.warning("antiword failed for %s: %s", path, e)
    # Fallback: try libreoffice conversion
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(
                ["libreoffice", "--headless", "--convert-to", "txt", "--outdir", tmpdir, path],
                capture_output=True, timeout=60,
            )
            txt_file = Path(tmpdir) / (Path(path).stem + ".txt")
            if txt_file.exists():
                return txt_file.read_text(errors="replace"), "text_native"
    except Exception as e:
        log.warning("libreoffice fallback failed for %s: %s", path, e)
    return "", "failed"


def extract_xlsx_text(path):
    """Extract cell values from .xlsx/.xls."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                vals = [str(c) for c in row if c is not None]
                if vals:
                    parts.append("\t".join(vals))
        wb.close()
        return "\n".join(parts), "text_native"
    except Exception as e:
        log.warning("openpyxl failed for %s: %s", path, e)
        return "", "failed"


def extract_txt_text(path):
    """Read plain text files."""
    try:
        with open(path, "r", errors="replace") as f:
            return f.read(500_000), "text_native"  # Cap at 500KB
    except Exception as e:
        log.warning("txt read failed for %s: %s", path, e)
        return "", "failed"


def extract_email_text(path):
    """Extract text from .msg or .eml files."""
    import email
    from email import policy
    try:
        with open(path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=policy.default)
        parts = []
        subj = msg.get("Subject", "")
        if subj:
            parts.append(f"Subject: {subj}")
        frm = msg.get("From", "")
        if frm:
            parts.append(f"From: {frm}")
        to = msg.get("To", "")
        if to:
            parts.append(f"To: {to}")
        dt = msg.get("Date", "")
        if dt:
            parts.append(f"Date: {dt}")
        parts.append("")
        body = msg.get_body(preferencelist=("plain", "html"))
        if body:
            content = body.get_content()
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            # Strip HTML tags if html body
            if body.get_content_type() == "text/html":
                import re
                content = re.sub(r"<[^>]+>", " ", content)
                content = re.sub(r"\s+", " ", content).strip()
            parts.append(content[:200_000])
        return "\n".join(parts), "text_native"
    except Exception as e:
        log.warning("email parse failed for %s: %s", path, e)
        return "", "failed"


def extract_image_text(path):
    """OCR an image file."""
    import pytesseract
    from PIL import Image
    try:
        img = Image.open(path)
        text = pytesseract.image_to_string(img)
        return text, "ocr_complete"
    except Exception as e:
        log.warning("image OCR failed for %s: %s", path, e)
        return "", "failed"


# ── Dispatch by extension ──────────────────────────────────────────────────

EXTRACTORS = {
    "pdf": extract_pdf_text,
    "docx": extract_docx_text,
    "doc": extract_doc_text,
    "xlsx": extract_xlsx_text,
    "xls": extract_xlsx_text,
    "txt": extract_txt_text,
    "csv": extract_txt_text,
    "eml": extract_email_text,
    "msg": extract_email_text,
    "jpg": extract_image_text,
    "jpeg": extract_image_text,
    "png": extract_image_text,
    "tif": extract_image_text,
    "tiff": extract_image_text,
    "bmp": extract_image_text,
    "rtf": extract_txt_text,
    "wpd": extract_txt_text,
}


def extract_text(file_path):
    """Extract text from a file. Returns (text, new_ocr_status)."""
    if not os.path.isfile(file_path):
        return "", "file_not_found"
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    extractor = EXTRACTORS.get(ext)
    if not extractor:
        return "", "not_applicable"
    try:
        return extractor(file_path)
    except Exception as e:
        log.error("Extraction crashed for %s: %s", file_path, e)
        return "", "failed"


# ── Worker function (one doc) ──────────────────────────────────────────────

def process_one(doc_id, file_path):
    """Process a single document. Returns (doc_id, text_len, ocr_status, error)."""
    try:
        text, ocr_status = extract_text(file_path)
        text = text.strip() if text else ""
        extraction_status = "complete" if text else ("failed" if ocr_status == "failed" else "no_text")

        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE dms_documents
            SET content_text = %s,
                ocr_status = %s,
                extraction_status = %s,
                updated_at = NOW()
            WHERE id = %s
        """, (text[:2_000_000] if text else None, ocr_status, extraction_status, doc_id))
        conn.commit()
        cur.close()
        conn.close()
        return (doc_id, len(text), ocr_status, None)
    except Exception as e:
        return (doc_id, 0, "error", str(e))


# ── Main loop ──────────────────────────────────────────────────────────────

def fetch_batch(batch_size, ocr_only=False):
    """Fetch next batch of docs needing extraction."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where = "AND ocr_status = 'ocr_pending'" if ocr_only else ""
    cur.execute(f"""
        SELECT id::text, file_path, ocr_status
        FROM dms_documents
        WHERE extraction_status = 'pending'
          AND ocr_status != 'skipped_production'
          {where}
        ORDER BY
            CASE ocr_status
                WHEN 'ocr_pending' THEN 1
                WHEN 'text_native' THEN 2
                ELSE 3
            END,
            indexed_at
        LIMIT %s
    """, (batch_size,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def main():
    parser = argparse.ArgumentParser(description="Extract text from dms_documents")
    parser.add_argument("--batch", type=int, default=500, help="Batch size per round")
    parser.add_argument("--workers", type=int, default=1, help="Parallel workers (1 = sequential)")
    parser.add_argument("--ocr-only", action="store_true", help="Only process ocr_pending docs")
    parser.add_argument("--max-rounds", type=int, default=0, help="Max rounds (0 = unlimited)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch batch but don't process")
    args = parser.parse_args()

    log.info("Starting text extraction: batch=%d, workers=%d, ocr_only=%s",
             args.batch, args.workers, args.ocr_only)

    round_num = 0
    total_processed = 0
    total_extracted = 0
    start = time.time()

    while True:
        round_num += 1
        if args.max_rounds and round_num > args.max_rounds:
            break

        batch = fetch_batch(args.batch, args.ocr_only)
        if not batch:
            log.info("No more documents to process. Total: %d processed, %d with text.",
                     total_processed, total_extracted)
            break

        log.info("Round %d: %d documents", round_num, len(batch))

        if args.dry_run:
            for row in batch[:5]:
                log.info("  [DRY] %s (%s)", os.path.basename(row["file_path"]), row["ocr_status"])
            continue

        results = []
        if args.workers <= 1:
            for row in batch:
                result = process_one(row["id"], row["file_path"])
                results.append(result)
                if result[1] > 0:
                    total_extracted += 1
                total_processed += 1
                if total_processed % 50 == 0:
                    elapsed = time.time() - start
                    rate = total_processed / elapsed * 3600
                    log.info("  Progress: %d processed, %d with text (%.0f/hr)",
                             total_processed, total_extracted, rate)
        else:
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                futures = {
                    pool.submit(process_one, row["id"], row["file_path"]): row
                    for row in batch
                }
                for future in as_completed(futures):
                    result = future.result()
                    results.append(result)
                    if result[1] > 0:
                        total_extracted += 1
                    total_processed += 1

        # Log round summary
        ok = sum(1 for r in results if r[3] is None)
        fail = sum(1 for r in results if r[3] is not None)
        chars = sum(r[1] for r in results)
        log.info("Round %d complete: %d ok, %d fail, %d total chars", round_num, ok, fail, chars)

        # Log errors
        for r in results:
            if r[3]:
                log.error("  FAIL %s: %s", r[0][:8], r[3])

    elapsed = time.time() - start
    log.info("DONE. %d processed, %d with text, %.1f minutes",
             total_processed, total_extracted, elapsed / 60)


if __name__ == "__main__":
    main()
