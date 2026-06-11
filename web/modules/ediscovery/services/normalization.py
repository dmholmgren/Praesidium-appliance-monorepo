"""
Normalization service -- transforms raw extracted text into clean,
chunk-ready content with provenance metadata.

Never modifies extracted_text (the forensic original).
Writes to normalized_text / normalized_metadata / email_segments.

Uses:
  - html2text for HTML->plaintext conversion
  - email_reply_parser for quote boundary detection
  - langdetect for language identification
  - Regex-based signature stripping (English + Spanish)

Design principles (from PRAESIDIUM_EDISCOVERY_PIPELINE_REWORK.md):
  1. Never destroy the raw
  2. Provenance on every text field
  3. Segment is the atomic unit, not the message
"""

import hashlib
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


def _detect_language(text: str) -> str:
    """
    Detect language of text. Returns ISO 639-1 code ('en', 'es', 'pt', etc.)
    Falls back to 'en' on short text or detection failure.
    """
    if not text or len(text.strip()) < 40:
        return "en"
    try:
        from langdetect import detect, DetectorFactory
        DetectorFactory.seed = 0
        return detect(text[:2000])
    except Exception:
        return "en"


# -- Signature patterns -------------------------------------------------------
SIG_PATTERNS = [
    re.compile(r'^-- \s*$', re.MULTILINE),
    re.compile(r'^_{3,}\s*$', re.MULTILINE),
    re.compile(r'^-{3,}\s*$', re.MULTILINE),
    # English sign-offs
    re.compile(
        r'^(?:Thanks|Thank you|Regards|Best regards|Best|Sincerely|Cheers|'
        r'Kind regards|Respectfully|Warm regards|V/r|Sent from my)'
        r'[,.]?\s*$',
        re.MULTILINE | re.IGNORECASE,
    ),
    # Spanish sign-offs
    re.compile(
        r'^(?:Gracias|Saludos|Atentamente|Cordialmente|Un saludo|'
        r'Quedo a sus ordenes|Quedo atento|Reciba un cordial saludo|'
        r'Enviado desde mi)'
        r'[,.]?\s*$',
        re.MULTILINE | re.IGNORECASE,
    ),
    # Confidentiality boilerplate (English + Spanish)
    re.compile(
        r'^(?:CONFIDENTIAL|PRIVILEGED|This email|This message|'
        r'The information contained|NOTICE:?\s|DISCLAIMER|'
        r'AVISO DE CONFIDENCIALIDAD|Este mensaje|Este correo|'
        r'La informacion contenida|AVISO LEGAL)',
        re.MULTILINE | re.IGNORECASE,
    ),
]

QUOTE_HEADER_RE = re.compile(
    r'^-{2,}\s*(?:Original Message|Forwarded message|'
    r'On .+ wrote:|From:\s|Sent:\s|Date:\s)',
    re.MULTILINE | re.IGNORECASE,
)


def normalize(raw_text, doc_type, email_meta=None):
    if not raw_text:
        return {"normalized_text": None, "normalized_metadata": {}, "segments": []}
    meta = email_meta or {}
    if doc_type == "email":
        return _normalize_email(raw_text, meta)
    else:
        return _normalize_document(raw_text, doc_type)


def _normalize_document(raw_text, doc_type):
    text = raw_text
    if _looks_like_html(text):
        text = _strip_html(text)
    text = _collapse_whitespace(text)
    if not text.strip():
        return {"normalized_text": None, "normalized_metadata": {"word_count": 0}, "segments": []}
    return {
        "normalized_text": text.strip(),
        "normalized_metadata": {
            "word_count": len(text.split()),
            "html_stripped": _looks_like_html(raw_text),
            "detected_language": _detect_language(text),
        },
        "segments": [],
    }


def _normalize_email(raw_text, meta):
    text = raw_text
    subject_prefix_match = re.match(r'^Subject:\s*.*?\n\n', text, re.DOTALL)
    if subject_prefix_match:
        text = text[subject_prefix_match.end():]
    if _looks_like_html(text):
        text = _strip_html(text)
    text = _collapse_whitespace(text)
    if not text.strip():
        return {"normalized_text": None, "normalized_metadata": {"word_count": 0}, "segments": []}

    segments = _split_email_segments(text, meta)
    top_segment = next((s for s in segments if s["is_top"]), None)
    top_body = top_segment["content"] if top_segment else text
    top_clean, sig_text = _strip_signature(top_body)
    if top_segment:
        top_segment["content"] = top_clean

    envelope = _build_envelope(meta)
    normalized = f"{envelope}\n\n{top_clean}" if envelope else top_clean
    detected_lang = _detect_language(top_clean)

    normalized_metadata = {
        "word_count": len(normalized.split()),
        "has_quoted_tail": len(segments) > 1,
        "segment_count": len(segments),
        "sig_stripped": bool(sig_text),
        "sig_text": sig_text[:500] if sig_text else None,
        "html_stripped": _looks_like_html(raw_text),
        "participants": _extract_participants(meta),
        "detected_language": detected_lang,
    }
    return {"normalized_text": normalized.strip(), "normalized_metadata": normalized_metadata, "segments": segments}


def _looks_like_html(text):
    if not text:
        return False
    return bool(re.search(r'<(?:html|head|body|div|p|span|table|br)\b', text[:2000], re.IGNORECASE))


def _strip_html(text):
    try:
        import html2text
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.ignore_emphasis = False
        h.body_width = 0
        h.unicode_snob = True
        return h.handle(text)
    except ImportError:
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(text, "html.parser").get_text(separator="\n")
        except ImportError:
            return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', text)).strip()


def _collapse_whitespace(text):
    lines = text.splitlines()
    result = []
    blank_count = 0
    for line in lines:
        line = line.rstrip()
        if not line.strip():
            blank_count += 1
            if blank_count <= 2:
                result.append(line)
        else:
            blank_count = 0
            result.append(line)
    return "\n".join(result)


def _split_email_segments(text, meta):
    segments = []
    try:
        from email_reply_parser import EmailReplyParser
        reply = EmailReplyParser.read(text)
        for idx, fragment in enumerate(reply.fragments):
            content = fragment.content.strip()
            if not content:
                continue
            is_top = (idx == 0 and not fragment.quoted and not fragment.hidden)
            segments.append({
                "segment_index": idx,
                "author": meta.get("from", "") if is_top else None,
                "sent_date": meta.get("date") if is_top else None,
                "content": content,
                "is_top": is_top,
                "is_quoted": fragment.quoted,
                "is_hidden": fragment.hidden,
                "normalized_hash": _hash_segment(content),
            })
    except Exception as e:
        logger.warning("email_reply_parser failed: %s", e)
        segments.append({
            "segment_index": 0, "author": meta.get("from", ""),
            "sent_date": meta.get("date"), "content": text.strip(),
            "is_top": True, "is_quoted": False, "is_hidden": False,
            "normalized_hash": _hash_segment(text),
        })
    return segments


def _strip_signature(text):
    if not text:
        return text, None
    best_pos = None
    for pattern in SIG_PATTERNS:
        match = pattern.search(text)
        if match and match.start() > len(text) * 0.6:
            if best_pos is None or match.start() < best_pos:
                best_pos = match.start()
    if best_pos is not None:
        clean = text[:best_pos].rstrip()
        sig = text[best_pos:].strip()
        if len(clean) > len(text) * 0.4:
            return clean, sig
    return text, None


def _build_envelope(meta):
    parts = []
    for key, label in [("from", "From"), ("to", "To"), ("cc", "Cc"), ("date", "Date"), ("subject", "Subject")]:
        if meta.get(key):
            parts.append(f"{label}: {meta[key]}")
    return "\n".join(parts)


def _extract_participants(meta):
    email_re = re.compile(r'[\w.+-]+@[\w.-]+\.\w+')
    all_addrs = set()
    for field in ("from", "to", "cc", "bcc"):
        val = meta.get(field, "")
        if val:
            all_addrs.update(email_re.findall(str(val).lower()))
    return sorted(all_addrs)


def _hash_segment(content):
    if not content:
        return ""
    lines = content.splitlines()
    cleaned = []
    for line in lines:
        stripped = re.sub(r'^[>\s]+', '', line).strip()
        if re.match(r'^(On .+ wrote:|From:\s|Sent:\s|Date:\s|To:\s|Subject:\s|-{2,})', stripped, re.IGNORECASE):
            continue
        if stripped:
            cleaned.append(stripped.lower())
    canonical = "\n".join(cleaned)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
