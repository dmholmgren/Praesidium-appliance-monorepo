"""
Text extraction service -- extracts text from various document formats.

Uses:
  - PyMuPDF (fitz) for PDFs -- faster, better Unicode/CIDFont for Spanish
  - python-docx for Word documents
  - extract_msg for .msg email files (single-open: text + metadata together)
  - email stdlib for .eml files (single-open: text + metadata together)
  - html2text for HTML->plaintext on email bodies

OCR is NOT inline -- thin-text PDFs are flagged ocr_status='queued'
for the Pass-2 backfill lane. This keeps Pass 1 fast so review can start.

All processing local -- no data leaves the network.
"""

import hashlib
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _sanitize_text(text):
    """Strip NUL bytes that break PostgreSQL text columns."""
    if text is None:
        return None
    return text.replace("\x00", "")


def extract_text(file_path: str, doc_type: str) -> tuple[Optional[str], Optional[int]]:
    """
    Extract text from a file.
    Returns (extracted_text, page_count).
    """
    try:
        if doc_type == "pdf":
            text, pc = _extract_pdf(file_path)
        elif doc_type == "word":
            text, pc = _extract_word(file_path)
        elif doc_type == "email":
            text, pc = _extract_email_text(file_path)
        elif doc_type == "image":
            text, pc = _extract_ocr(file_path)
        elif doc_type == "text":
            text, pc = _extract_plain_text(file_path)
        elif doc_type == "spreadsheet":
            text, pc = _extract_spreadsheet(file_path)
        elif doc_type == "html":
            text, pc = _extract_html(file_path)
        else:
            logger.warning("Unsupported doc_type for extraction: %s", doc_type)
            return None, None
        return _sanitize_text(text), pc
    except Exception as e:
        logger.error("Text extraction failed for %s: %s", file_path, str(e))
        return None, None


def _extract_pdf(file_path: str) -> tuple[Optional[str], Optional[int]]:
    """
    Extract text from PDF using PyMuPDF (fitz).
    Faster than pdfplumber, better Unicode/CIDFont handling (critical for
    Spanish docs), preserves reading order.

    OCR is NOT triggered here. Thin-text PDFs return what they have;
    the caller sets ocr_status='queued' for Pass-2 backfill.
    """
    import fitz  # PyMuPDF

    text_parts = []
    try:
        doc = fitz.open(file_path)
    except Exception as e:
        logger.error("PyMuPDF failed to open %s: %s", file_path, e)
        return None, None

    page_count = len(doc)
    for page in doc:
        page_text = page.get_text("text")
        if page_text and page_text.strip():
            text_parts.append(page_text)
    doc.close()

    full_text = "\n\n".join(text_parts)
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


def _strip_html_to_text(html_content: str) -> str:
    """Convert HTML to clean plaintext."""
    try:
        import html2text
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0
        h.unicode_snob = True
        return h.handle(html_content)
    except ImportError:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content, "html.parser")
            return soup.get_text(separator="\n")
        except ImportError:
            clean = re.sub(r'<[^>]+>', ' ', html_content)
            return re.sub(r'\s+', ' ', clean).strip()


def _extract_email_text(file_path: str) -> tuple[Optional[str], Optional[int]]:
    """
    Extract body text from email files.
    Prefers plain text body; falls back to HTML body with strip.
    Always returns clean plaintext, never raw HTML/RTF.
    """
    ext = Path(file_path).suffix.lower()

    if ext == ".msg":
        import extract_msg
        msg = extract_msg.Message(file_path)
        subject = msg.subject or ""

        # Prefer plain body; fall back to HTML body with strip
        body = msg.body
        if not body or not body.strip():
            html_body = getattr(msg, 'htmlBody', None)
            if html_body:
                if isinstance(html_body, bytes):
                    html_body = html_body.decode("utf-8", errors="replace")
                body = _strip_html_to_text(html_body)

        if not body or not body.strip():
            body = ""

        return f"Subject: {subject}\n\n{body}", None

    elif ext == ".eml":
        import email
        from email import policy
        with open(file_path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=policy.default)

        subject = msg.get("Subject", "")

        # Prefer plain text; fall back to HTML with strip
        body_part = msg.get_body(preferencelist=("plain",))
        if body_part:
            body = body_part.get_content()
        else:
            body_part = msg.get_body(preferencelist=("html",))
            if body_part:
                html_content = body_part.get_content()
                body = _strip_html_to_text(html_content)
            else:
                body = ""

        return f"Subject: {subject}\n\n{body}", None

    return None, None


def _extract_ocr(file_path: str) -> tuple[Optional[str], Optional[int]]:
    """OCR using Tesseract -- for images and scanned PDFs."""
    try:
        import pytesseract
        from PIL import Image

        ext = Path(file_path).suffix.lower()

        if ext == ".pdf":
            from pdf2image import convert_from_path
            images = convert_from_path(file_path, dpi=150)
            text_parts = []
            for img in images:
                text_parts.append(pytesseract.image_to_string(img, lang='spa+eng'))
            return "\n\n".join(text_parts), len(images)
        else:
            img = Image.open(file_path)
            text = pytesseract.image_to_string(img, lang='spa+eng')
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
    """Extract text from spreadsheets -- cell values concatenated."""
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
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return _strip_html_to_text(f.read()), None
    except Exception:
        return None, None


def _normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd:/FW: prefixes for thread grouping."""
    cleaned = re.sub(r"^(re|fwd?|fw)\s*:\s*", "", subject.strip(), flags=re.IGNORECASE)
    return cleaned.strip().lower()


def extract_email_metadata(file_path: str) -> dict:
    """
    Parse email metadata from .msg or .eml file.
    Returns dict with from, to, cc, subject, date, thread_id, etc.

    SINGLE-OPEN: For new ingests, use extract_email_complete() instead.
    This remains for backward compat with code that calls it separately.
    """
    ext = Path(file_path).suffix.lower()
    meta = {}

    try:
        if ext == ".msg":
            import extract_msg
            msg = extract_msg.Message(file_path)
            meta = _meta_from_msg(msg)

        elif ext == ".eml":
            import email
            from email import policy
            with open(file_path, "rb") as f:
                msg = email.message_from_binary_file(f, policy=policy.default)
            meta = _meta_from_eml(msg)

    except Exception as e:
        logger.error("Email metadata extraction failed for %s: %s", file_path, str(e))

    return meta


def extract_email_complete(file_path: str) -> dict:
    """
    Single-open email extraction -- returns text, metadata, and attachment info
    from ONE parse of the file. Eliminates the double-open race condition.

    Returns dict:
        text: str -- extracted body text (plaintext, HTML stripped)
        page_count: None
        metadata: dict -- email metadata (from, to, cc, subject, date, etc.)
        attachment_count: int
        attachment_names: list[str]
    """
    ext = Path(file_path).suffix.lower()

    if ext == ".msg":
        return _complete_msg(file_path)
    elif ext == ".eml":
        return _complete_eml(file_path)
    else:
        return {
            "text": None, "page_count": None, "metadata": {},
            "attachment_count": 0, "attachment_names": [],
        }


def _complete_msg(file_path: str) -> dict:
    """Single-open .msg extraction."""
    import extract_msg
    msg = extract_msg.Message(file_path)

    subject = msg.subject or ""
    body = msg.body
    if not body or not body.strip():
        html_body = getattr(msg, 'htmlBody', None)
        if html_body:
            if isinstance(html_body, bytes):
                html_body = html_body.decode("utf-8", errors="replace")
            body = _strip_html_to_text(html_body)
    if not body:
        body = ""

    text = _sanitize_text(f"Subject: {subject}\n\n{body}")
    meta = _meta_from_msg(msg)

    att_names = []
    for att in getattr(msg, "attachments", []):
        fname = getattr(att, "longFilename", None) or getattr(att, "shortFilename", None)
        if fname:
            att_names.append(fname)

    return {
        "text": text, "page_count": None, "metadata": meta,
        "attachment_count": len(att_names), "attachment_names": att_names,
    }


def _complete_eml(file_path: str) -> dict:
    """Single-open .eml extraction."""
    import email
    from email import policy

    with open(file_path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)

    subject = msg.get("Subject", "")

    body_part = msg.get_body(preferencelist=("plain",))
    if body_part:
        body = body_part.get_content()
    else:
        body_part = msg.get_body(preferencelist=("html",))
        if body_part:
            body = _strip_html_to_text(body_part.get_content())
        else:
            body = ""

    text = _sanitize_text(f"Subject: {subject}\n\n{body}")
    meta = _meta_from_eml(msg)

    att_names = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fname = part.get_filename()
        cd = str(part.get("Content-Disposition") or "")
        if fname or "attachment" in cd.lower():
            if fname:
                att_names.append(fname)

    return {
        "text": text, "page_count": None, "metadata": meta,
        "attachment_count": len(att_names), "attachment_names": att_names,
    }


def _meta_from_msg(msg) -> dict:
    """Extract metadata dict from an extract_msg.Message object."""
    meta = {}
    meta["from"] = msg.sender or ""
    meta["to"] = msg.to or ""
    meta["cc"] = msg.cc or ""
    meta["subject"] = msg.subject or ""
    meta["date"] = msg.date
    meta["message_id"] = getattr(msg, "message_id", None)
    meta["in_reply_to"] = getattr(msg, "in_reply_to", None)
    meta["references"] = getattr(msg, "references", None)

    meta["thread_id"] = (
        meta.get("in_reply_to")
        or _normalize_subject(meta.get("subject", ""))
    )
    return meta


def _meta_from_eml(msg) -> dict:
    """Extract metadata dict from an email.message.EmailMessage object."""
    meta = {}
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
    else:
        meta["date"] = None

    meta["message_id"] = msg.get("Message-ID", "")
    meta["in_reply_to"] = msg.get("In-Reply-To", "")

    refs = msg.get("References", "")
    meta["references"] = refs

    # FIXED: thread_id precedence bug
    # Old: meta.get("in_reply_to") or refs.split()[0] if refs else "" or ...
    # The or mixed with ternary caused operator precedence issues.
    if meta.get("in_reply_to"):
        meta["thread_id"] = meta["in_reply_to"]
    elif refs and refs.strip():
        meta["thread_id"] = refs.strip().split()[0]
    else:
        meta["thread_id"] = _normalize_subject(meta.get("subject", ""))

    return meta


def extract_email_attachments(file_path: str) -> list:
    """
    Parse an .eml or .msg file and extract MIME attachments.
    Returns list of dicts: {filename, content_type, data}
    Skips body parts, crypto signatures, and tiny inline images (<512 bytes).
    """
    MIN_SIZE = 512
    SKIP_TYPES = {"application/pgp-signature", "application/pkcs7-signature"}
    ext = Path(file_path).suffix.lower()
    attachments = []

    try:
        if ext == ".eml":
            import email as _email
            from email import policy as _policy
            with open(file_path, "rb") as f:
                msg = _email.message_from_binary_file(f, policy=_policy.default)
            for part in msg.walk():
                ct = part.get_content_type()
                cd = str(part.get("Content-Disposition") or "")
                if part.is_multipart() or ct in SKIP_TYPES:
                    continue
                has_filename = part.get_filename() is not None
                is_attachment = "attachment" in cd.lower()
                if not (is_attachment or has_filename):
                    if ct in ("text/plain", "text/html"):
                        continue
                    continue
                try:
                    payload = part.get_content()
                except Exception:
                    try:
                        payload = part.get_payload(decode=True)
                    except Exception:
                        continue
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")
                if not isinstance(payload, (bytes, bytearray)) or len(payload) < MIN_SIZE:
                    continue
                fname = part.get_filename() or f"attachment_{len(attachments)}"
                fname = fname.replace("/", "_").replace("\\", "_").replace("\x00", "")
                attachments.append({"filename": fname, "content_type": ct, "data": payload})

        elif ext == ".msg":
            import extract_msg
            msg = extract_msg.Message(file_path)
            for att in getattr(msg, "attachments", []):
                fname = (
                    getattr(att, "longFilename", None)
                    or getattr(att, "shortFilename", None)
                    or f"attachment_{len(attachments)}"
                )
                data = getattr(att, "data", None)
                if not data or len(data) < MIN_SIZE:
                    continue
                ct = getattr(att, "mimetype", None) or "application/octet-stream"
                attachments.append({"filename": fname, "content_type": ct, "data": data})

    except Exception as e:
        logger.error("Attachment extraction failed for %s: %s", file_path, str(e))

    return attachments
