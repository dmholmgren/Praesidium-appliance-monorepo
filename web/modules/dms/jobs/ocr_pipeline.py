from sqlalchemy import text as sa_text
"""
COMP 3 — OCR Pipeline
RQ job running on MAIN-PRD-PROC-01 (10.10.60.12).
- Tesseract for image PDFs and images
- pdfplumber for text-layer PDFs
- python-docx for Word documents
- Stores extracted text in documents.ocr_text
- Enqueues Meilisearch index job on completion
"""

import os
import io
import logging
import tempfile
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def ocr_document(tenant_id: str, document_id: str):
    """
    Main OCR entry point. Dispatched as RQ job.
    Downloads file via StorageService, extracts text based on type,
    stores in documents.ocr_text, enqueues search indexing.
    """
    from core.db.base import TenantSession, get_session_factory
    from core.audit import write_audit
    import httpx

    session = TenantSession(get_session_factory()(), tenant_id)

    # Get document record
    doc = session.execute(
        sa_text("SELECT id, storage_path, doc_type, filename FROM documents "
        "WHERE id = :id AND tenant_id = :tid"),
        {"id": document_id, "tid": tenant_id},
    ).fetchone()

    if not doc:
        logger.error(f"Document {document_id} not found for tenant {tenant_id}")
        return

    storage_path = doc["storage_path"]
    file_type = (os.path.splitext(storage_path or "")[1].lstrip(".").lower()
                 or (doc["doc_type"] or "").lower())

    logger.info(f"OCR starting: doc={document_id} type={file_type} path={storage_path}")

    # Read the file from the locally-mounted share. The platform migrated off
    # the FBRG-01 / CIFS_URL HTTP bridge to direct mount reads
    # (modules/dms/adapters/local_mount_storage.py); the old bridge host is gone.
    try:
        with open(storage_path, "rb") as _fh:
            content = _fh.read()
    except Exception as e:
        logger.error(f"Failed to read {storage_path}: {e}")
        _update_ocr_status(session, tenant_id, document_id, "download_failed", str(e))
        return

    # Extract text based on file type
    try:
        if file_type in ("pdf",):
            extracted = _process_pdf(content)
        elif file_type in ("docx",):
            extracted = _process_docx(content)
        elif file_type in ("doc",):
            extracted = _process_doc(content)
        elif file_type in ("xlsx", "xls", "csv"):
            extracted = _process_spreadsheet(content, file_type)
        elif file_type in ("msg", "eml"):
            extracted = _process_email(content, file_type)
        elif file_type in ("jpg", "jpeg", "png", "tiff", "tif"):
            extracted = _process_image(content)
        elif file_type in ("txt", "rtf"):
            extracted = content.decode("utf-8", errors="replace")
        else:
            extracted = ""
            logger.warning(f"Unsupported file type: {file_type}")
    except Exception as e:
        logger.error(f"OCR extraction failed for {document_id}: {e}")
        _update_ocr_status(session, tenant_id, document_id, "ocr_failed", str(e))
        return

    # Store extracted text
    session.execute(
        sa_text("UPDATE documents SET ocr_text = :text, ocr_status = :status, "
        "ocr_completed_at = :completed WHERE id = :id AND tenant_id = :tid"),
        {
            "text": extracted[:5_000_000] if extracted else "",  # Cap at 5MB
            "status": "completed",
            "completed": datetime.now(timezone.utc).isoformat(),
            "id": document_id,
            "tid": tenant_id,
        },
    )

    write_audit(
        tenant_id=tenant_id,
        user_id="system",
        action="ocr_complete",
        module="dms",
        table_name="documents",
        record_id=document_id,
        new_value={"ocr_text_length": len(extracted) if extracted else 0},
        source="ocr_pipeline",
    )
    session.commit()

    # Enqueue Meilisearch indexing
    from modules.dms.jobs.file_crawler import get_rq_queue
    q = get_rq_queue("search")
    q.enqueue(
        "modules.dms.jobs.search_indexer.index_document",
        tenant_id, document_id,
    )

    logger.info(
        f"OCR complete: doc={document_id} "
        f"chars={len(extracted) if extracted else 0}"
    )


def _process_pdf(content: bytes) -> str:
    """
    Process PDF: try pdfplumber first (text layer),
    fall back to Tesseract (image/scanned).
    """
    import pdfplumber

    text_parts = []
    is_image_pdf = True

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                is_image_pdf = False
                text_parts.append(page_text)

    # If we got reasonable text, return it
    if not is_image_pdf and text_parts:
        return "\n\n".join(text_parts)

    # Fall back to Tesseract OCR for image PDFs
    return _tesseract_pdf(content)


def _tesseract_pdf(content: bytes) -> str:
    """Run Tesseract on a PDF by converting pages to images first."""
    import subprocess

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = Path(tmpdir) / "input.pdf"
        pdf_path.write_bytes(content)

        # Convert PDF to images using pdftoppm
        subprocess.run(
            ["pdftoppm", "-jpeg", "-r", "300", str(pdf_path), f"{tmpdir}/page"],
            check=True,
            timeout=300,
        )

        # OCR each page image
        text_parts = []
        for img_path in sorted(Path(tmpdir).glob("page-*.jpg")):
            text = _tesseract_image(img_path.read_bytes())
            if text:
                text_parts.append(text)

    return "\n\n".join(text_parts)


def _tesseract_image(content: bytes) -> str:
    """Run Tesseract OCR on an image."""
    import pytesseract
    from PIL import Image

    img = Image.open(io.BytesIO(content))
    return pytesseract.image_to_string(img, lang="eng")


def _process_image(content: bytes) -> str:
    """Process a standalone image file with Tesseract."""
    return _tesseract_image(content)


def _process_docx(content: bytes) -> str:
    """Extract text from a .docx file using python-docx."""
    from docx import Document

    doc = Document(io.BytesIO(content))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]

    # Also extract table content
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                paragraphs.append(" | ".join(cells))

    return "\n\n".join(paragraphs)


def _process_doc(content: bytes) -> str:
    """Extract text from a legacy .doc file using antiword or LibreOffice."""
    import subprocess

    with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as f:
        f.write(content)
        f.flush()

        try:
            result = subprocess.run(
                ["antiword", f.name],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                return result.stdout
        except FileNotFoundError:
            pass

        # Fallback: convert to text via LibreOffice
        try:
            outdir = tempfile.mkdtemp()
            subprocess.run(
                ["libreoffice", "--headless", "--convert-to", "txt", f.name,
                 "--outdir", outdir],
                timeout=120, check=True,
            )
            txt_path = Path(outdir) / (Path(f.name).stem + ".txt")
            if txt_path.exists():
                return txt_path.read_text(errors="replace")
        except Exception:
            pass

        os.unlink(f.name)
    return ""


def _process_spreadsheet(content: bytes, file_type: str) -> str:
    """Extract text content from spreadsheet files."""
    import csv

    if file_type == "csv":
        text = content.decode("utf-8", errors="replace")
        return text

    # For xlsx/xls, use openpyxl
    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            sheet_text = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                if any(cells):
                    sheet_text.append(" | ".join(cells))
            if sheet_text:
                parts.append(f"[Sheet: {ws.title}]\n" + "\n".join(sheet_text))
        return "\n\n".join(parts)
    except Exception as e:
        logger.error(f"Spreadsheet extraction failed: {e}")
        return ""


def _process_email(content: bytes, file_type: str) -> str:
    """Extract text from email files (.msg, .eml)."""
    if file_type == "eml":
        import email
        msg = email.message_from_bytes(content)
        parts = [
            f"From: {msg.get('From', '')}",
            f"To: {msg.get('To', '')}",
            f"Subject: {msg.get('Subject', '')}",
            f"Date: {msg.get('Date', '')}",
            "",
        ]
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    parts.append(payload.decode("utf-8", errors="replace"))
        return "\n".join(parts)

    # For .msg files, try extract_msg
    try:
        import extract_msg
        with tempfile.NamedTemporaryFile(suffix=".msg", delete=False) as f:
            f.write(content)
            f.flush()
            msg = extract_msg.Message(f.name)
            parts = [
                f"From: {msg.sender or ''}",
                f"To: {msg.to or ''}",
                f"Subject: {msg.subject or ''}",
                f"Date: {msg.date or ''}",
                "",
                msg.body or "",
            ]
            os.unlink(f.name)
            return "\n".join(parts)
    except Exception as e:
        logger.error(f"MSG extraction failed: {e}")
        return ""


def _update_ocr_status(session, tenant_id, document_id, status, error_msg=""):
    """Update OCR status on failure."""
    session.execute(
        sa_text("UPDATE documents SET ocr_status = :status, ocr_error = :err "
        "WHERE id = :id AND tenant_id = :tid"),
        {
            "status": status,
            "err": error_msg[:1000],
            "id": document_id,
            "tid": tenant_id,
        },
    )
    session.commit()
