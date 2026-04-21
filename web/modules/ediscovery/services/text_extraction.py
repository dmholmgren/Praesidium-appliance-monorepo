"""
Text extraction service — extracts text from various document formats.

Uses:
  - pdfplumber for PDFs with text layers
  - Tesseract OCR for scanned images and image-only PDFs
  - python-docx for Word documents
  - extract_msg for .msg email files
  - email stdlib for .eml files

All processing local — no data leaves the network.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def extract_text(file_path: str, doc_type: str) -> tuple[Optional[str], Optional[int]]:
    """
    Extract text from a file.
    Returns (extracted_text, page_count).
    """
    try:
        if doc_type == "pdf":
            return _extract_pdf(file_path)
        elif doc_type == "word":
            return _extract_word(file_path)
        elif doc_type == "email":
            return _extract_email_text(file_path)
        elif doc_type == "image":
            return _extract_ocr(file_path)
        elif doc_type == "text":
            return _extract_plain_text(file_path)
        elif doc_type == "spreadsheet":
            return _extract_spreadsheet(file_path)
        elif doc_type == "html":
            return _extract_html(file_path)
        else:
            logger.warning("Unsupported doc_type for extraction: %s", doc_type)
            return None, None
    except Exception as e:
        logger.error("Text extraction failed for %s: %s", file_path, str(e))
        return None, None


def _extract_pdf(file_path: str) -> tuple[Optional[str], Optional[int]]:
    """Extract text from PDF — tries native text first, falls back to OCR."""
    import pdfplumber

    text_parts = []
    page_count = 0

    with pdfplumber.open(file_path) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)

    full_text = "\n\n".join(text_parts)

    # If we got very little text, the PDF is likely scanned — fall back to OCR
    if len(full_text.strip()) < 50 and page_count > 0:
        ocr_text, _ = _extract_ocr(file_path)
        if ocr_text and len(ocr_text) > len(full_text):
            return ocr_text, page_count

    return full_text if full_text.strip() else None, page_count


def _extract_word(file_path: str) -> tuple[Optional[str], Optional[int]]:
    """Extract text from Word documents."""
    ext = Path(file_path).suffix.lower()

    if ext == ".docx":
        from docx import Document
        doc = Document(file_path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs) if paragraphs else None, None
    elif ext == ".doc":
        # Use antiword or LibreOffice for legacy .doc
        import subprocess
        try:
            result = subprocess.run(
                ["antiword", file_path],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                return result.stdout, None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None, None
    return None, None


def _extract_email_text(file_path: str) -> tuple[Optional[str], Optional[int]]:
    """Extract body text from email files."""
    ext = Path(file_path).suffix.lower()

    if ext == ".msg":
        import extract_msg
        msg = extract_msg.Message(file_path)
        body = msg.body or ""
        subject = msg.subject or ""
        return f"Subject: {subject}\n\n{body}", None

    elif ext == ".eml":
        import email
        from email import policy
        with open(file_path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=policy.default)
        body = msg.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body else ""
        subject = msg.get("Subject", "")
        return f"Subject: {subject}\n\n{text}", None

    return None, None


def _extract_ocr(file_path: str) -> tuple[Optional[str], Optional[int]]:
    """OCR using Tesseract — for images and scanned PDFs."""
    try:
        import pytesseract
        from PIL import Image

        ext = Path(file_path).suffix.lower()

        if ext == ".pdf":
            # Convert PDF pages to images first
            from pdf2image import convert_from_path
            images = convert_from_path(file_path, dpi=300)
            text_parts = []
            for img in images:
                text_parts.append(pytesseract.image_to_string(img))
            return "\n\n".join(text_parts), len(images)
        else:
            img = Image.open(file_path)
            text = pytesseract.image_to_string(img)
            return text if text.strip() else None, 1

    except Exception as e:
        logger.error("OCR failed for %s: %s", file_path, str(e))
        return None, None


def _extract_plain_text(file_path: str) -> tuple[Optional[str], Optional[int]]:
    """Read plain text files."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(), None
    except Exception:
        return None, None


def _extract_spreadsheet(file_path: str) -> tuple[Optional[str], Optional[int]]:
    """Extract text from spreadsheets — cell values concatenated."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        text_parts = []
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            text_parts.append(f"--- Sheet: {sheet} ---")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    text_parts.append(" | ".join(cells))
        return "\n".join(text_parts), None
    except Exception as e:
        logger.error("Spreadsheet extraction failed: %s", str(e))
        return None, None


def _extract_html(file_path: str) -> tuple[Optional[str], Optional[int]]:
    """Extract text from HTML files."""
    try:
        from bs4 import BeautifulSoup
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        return soup.get_text(separator="\n"), None
    except Exception:
        return None, None


def extract_email_metadata(file_path: str) -> dict:
    """
    Parse email metadata from .msg or .eml file.
    Returns dict with from, to, cc, subject, date, thread_id, etc.
    """
    ext = Path(file_path).suffix.lower()
    meta = {}

    try:
        if ext == ".msg":
            import extract_msg
            msg = extract_msg.Message(file_path)
            meta["from"] = msg.sender or ""
            meta["to"] = msg.to or ""
            meta["cc"] = msg.cc or ""
            meta["subject"] = msg.subject or ""
            meta["date"] = msg.date
            meta["message_id"] = getattr(msg, "message_id", None)
            meta["in_reply_to"] = getattr(msg, "in_reply_to", None)
            # Thread ID: use In-Reply-To or normalized subject
            meta["thread_id"] = (
                meta.get("in_reply_to")
                or _normalize_subject(meta.get("subject", ""))
            )
            meta["references"] = getattr(msg, "references", None)

        elif ext == ".eml":
            import email
            from email import policy
            with open(file_path, "rb") as f:
                msg = email.message_from_binary_file(f, policy=policy.default)
            meta["from"] = msg.get("From", "")
            meta["to"] = msg.get("To", "")
            meta["cc"] = msg.get("Cc", "")
            meta["subject"] = msg.get("Subject", "")
            date_str = msg.get("Date", "")
            if date_str:
                from email.utils import parsedate_to_datetime
                try:
                    meta["date"] = parsedate_to_datetime(date_str)
                except Exception:
                    meta["date"] = None
            meta["message_id"] = msg.get("Message-ID", "")
            meta["in_reply_to"] = msg.get("In-Reply-To", "")
            meta["references"] = msg.get("References", "")
            meta["thread_id"] = (
                meta.get("in_reply_to")
                or meta.get("references", "").split()[0] if meta.get("references") else ""
                or _normalize_subject(meta.get("subject", ""))
            )

    except Exception as e:
        logger.error("Email metadata extraction failed for %s: %s", file_path, str(e))

    return meta


def _normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd:/FW: prefixes for thread grouping."""
    import re
    cleaned = re.sub(r"^(re|fwd?|fw)\s*:\s*", "", subject.strip(), flags=re.IGNORECASE)
    return cleaned.strip().lower()
