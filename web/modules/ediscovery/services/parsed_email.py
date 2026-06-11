"""
parsed_email.py -- the single email-ingestion boundary for the eDiscovery lane.

Every email source collapses to ONE canonical structure:

    PST  (pypff, primary)  -.
    .msg (extract_msg)      |--> ParsedEmail{headers, body_text, body_html, attachments[]}
    .eml (stdlib email)     |
    mbox (stdlib mailbox)  -'

and ONE operation:

    explode(ParsedEmail) -> [ExplodedUnit]   # body + one unit per attachment

explode() is PURE: no database, no filesystem. It returns in-memory units with
family-linkage hints (parent_local_id / attachment_index / is_attachment) by
local index. The explode-and-map STAGE (ingest) assigns UUIDs, hashes bytes,
writes native/text copies, and resolves local ids -> family_id/parent_id.

Threading is fixed at the source here:
  * thread_root  = References[0]            (the ROOT of the thread)
  * in_reply_to  = immediate parent edge    (NOT the thread id)
  * references   = full ordered chain       (reference-edge material for stage 8)
  * conversation_index captured where available (.msg MAPI / PST record set)

This module imports cleanly even where pypff/extract_msg are absent: every
third-party import is lazy, inside the adapter that needs it.
"""

from __future__ import annotations

import hashlib
import logging
import mailbox
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# MAPI property tags we read out of PST record sets.
_PR_ATTACH_LONG_FILENAME = 0x3707
_PR_ATTACH_FILENAME = 0x3704
_PR_ATTACH_MIME_TAG = 0x370E
_PR_ATTACH_METHOD = 0x3705
_PR_CONVERSATION_INDEX = 0x0071
_ATTACH_METHOD_EMBEDDED_MSG = 5

_RE_SUBJ_PREFIX = re.compile(r"^\s*(re|fw|fwd|aw|wg|sv|vs)\s*:\s*", re.IGNORECASE)
_RE_MSGID = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# canonical structures
# ---------------------------------------------------------------------------

@dataclass
class ParsedAttachment:
    filename: str
    content_type: str
    data: Optional[bytes] = None          # None iff this is a nested message
    is_nested_message: bool = False
    nested: Optional["ParsedEmail"] = None
    source_index: int = 0                 # ordinal within the parent


@dataclass
class ParsedEmail:
    source_kind: str                      # 'pst' | 'msg' | 'eml' | 'mbox'
    headers: dict = field(default_factory=dict)
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    attachments: list = field(default_factory=list)   # list[ParsedAttachment]
    native_bytes: Optional[bytes] = None  # re-serialized RFC822 for preservation
    native_ext: str = "eml"
    message_id_synthetic: bool = False

    # ---- convenience accessors over headers -----------------------------
    @property
    def message_id(self) -> Optional[str]:
        return self.headers.get("message_id")

    @property
    def in_reply_to(self) -> Optional[str]:
        return self.headers.get("in_reply_to")

    @property
    def references(self) -> list:
        return self.headers.get("references") or []

    @property
    def thread_root(self) -> Optional[str]:
        """Root of the thread = References[0]; else self if a root; else None."""
        refs = self.references
        if refs:
            return refs[0]
        return self.message_id

    @property
    def conversation_index(self) -> Optional[str]:
        return self.headers.get("conversation_index")

    @property
    def subject(self) -> str:
        return self.headers.get("subject") or ""

    @property
    def normalized_subject(self) -> str:
        return _normalize_subject(self.subject)

    @property
    def sent_time(self) -> Optional[datetime]:
        return self.headers.get("date")

    def dedup_message_hash(self) -> str:
        """
        Normalized message identity for the dedup VIEW (handoff sec.3.2):
        from/to/cc/subject/sent-time/body. Re-serialized PST/MSG/EML differ
        byte-for-byte, so we never byte-dedup email -- we hash the semantics.
        """
        h = hashlib.sha256()
        for k in ("from", "to", "cc"):
            h.update((self.headers.get(k) or "").strip().lower().encode("utf-8", "replace"))
            h.update(b"\x1f")
        h.update(self.normalized_subject.encode("utf-8", "replace"))
        h.update(b"\x1f")
        st = self.sent_time
        h.update((st.isoformat() if st else "").encode("ascii", "replace"))
        h.update(b"\x1f")
        body = (self.body_text or "").strip()
        if not body and self.body_html:
            body = _collapse_ws(self.body_html)
        h.update(_collapse_ws(body).encode("utf-8", "replace"))
        return h.hexdigest()


@dataclass
class ExplodedUnit:
    """In-memory pre-persistence unit. Stage 3 turns these into rows + files."""
    role: str                              # 'email_body' | 'attachment'
    local_id: int
    parent_local_id: Optional[int]
    is_attachment: bool
    attachment_index: Optional[int]
    filename: Optional[str]
    content_type: Optional[str]
    data: Optional[bytes]                  # native bytes for attachments / body
    body_text: Optional[str] = None        # email_body only
    body_html: Optional[str] = None
    headers: Optional[dict] = None         # email_body only


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _normalize_subject(subject: str) -> str:
    if not subject:
        return ""
    s = subject
    prev = None
    while prev != s:
        prev = s
        s = _RE_SUBJ_PREFIX.sub("", s)
    return s.strip().lower()


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _decode(b) -> Optional[str]:
    """Bytes -> str, charset-detected, NUL-stripped. Tolerant of junk encodings."""
    if b is None:
        return None
    if isinstance(b, str):
        return b.replace("\x00", "")
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(b).best()
        if best is not None:
            return str(best).replace("\x00", "")
    except Exception:
        pass
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return b.decode(enc).replace("\x00", "")
        except Exception:
            continue
    return b.decode("utf-8", "replace").replace("\x00", "")


def _split_references(raw: str) -> list:
    if not raw:
        return []
    return _RE_MSGID.findall(raw)


def _parse_rfc822_headers(raw_or_msg) -> dict:
    """
    Parse an RFC822 header block (str) OR an already-parsed email.message.Message
    into our canonical header dict. Shared by .eml, PST transport_headers, and
    .msg .header -- this is what makes the boundary single.
    """
    import email as _email
    from email import policy as _policy
    from email.utils import parsedate_to_datetime

    if isinstance(raw_or_msg, str):
        msg = _email.message_from_string(raw_or_msg, policy=_policy.default)
    else:
        msg = raw_or_msg

    out: dict = {}
    out["from"] = str(msg.get("From", "") or "")
    out["to"] = str(msg.get("To", "") or "")
    out["cc"] = str(msg.get("Cc", "") or "")
    out["subject"] = str(msg.get("Subject", "") or "")
    out["message_id"] = (str(msg.get("Message-ID", "") or "").strip() or None)
    out["in_reply_to"] = (str(msg.get("In-Reply-To", "") or "").strip() or None)
    out["references"] = _split_references(str(msg.get("References", "") or ""))

    date_str = msg.get("Date", "")
    parsed_date = None
    if date_str:
        try:
            parsed_date = parsedate_to_datetime(str(date_str))
        except Exception:
            parsed_date = None
    out["date"] = parsed_date
    return out


def _synthesize_message_id(headers: dict, kind: str) -> str:
    """Stable surrogate Message-ID for sources with no transport Message-ID
    (PST Sent Items / drafts). Deterministic so re-ingest stays idempotent."""
    h = hashlib.sha1()
    for k in ("from", "to", "subject"):
        h.update((headers.get(k) or "").strip().lower().encode("utf-8", "replace"))
        h.update(b"|")
    d = headers.get("date")
    h.update((d.isoformat() if d else "").encode("ascii", "replace"))
    return "<synth-%s-%s@praesidium.local>" % (kind, h.hexdigest()[:24])


# ---------------------------------------------------------------------------
# .eml / mbox  (stdlib)
# ---------------------------------------------------------------------------

def parse_eml_bytes(raw: bytes, source_kind: str = "eml") -> ParsedEmail:
    import email as _email
    from email import policy as _policy

    msg = _email.message_from_bytes(raw, policy=_policy.default)
    headers = _parse_rfc822_headers(msg)

    body_text = None
    body_html = None
    try:
        tpart = msg.get_body(preferencelist=("plain",))
        if tpart is not None:
            body_text = _decode(tpart.get_content() if isinstance(tpart.get_content(), (bytes, bytearray))
                                 else tpart.get_content())
    except Exception:
        pass
    try:
        hpart = msg.get_body(preferencelist=("html",))
        if hpart is not None:
            content = hpart.get_content()
            body_html = _decode(content) if isinstance(content, (bytes, bytearray)) else content
    except Exception:
        pass

    attachments = _eml_attachments(msg)
    pe = ParsedEmail(
        source_kind=source_kind, headers=headers,
        body_text=body_text, body_html=body_html,
        attachments=attachments, native_bytes=raw, native_ext="eml",
    )
    _finalize(pe, kind=source_kind)
    return pe


def parse_eml(path: str) -> ParsedEmail:
    with open(path, "rb") as f:
        return parse_eml_bytes(f.read(), source_kind="eml")


def _eml_attachments(msg) -> list:
    """Top-level attachments via the modern iter_attachments() API.

    iter_attachments() yields THIS message's attachment parts (including
    message/rfc822) without recursing into them -- unlike walk(), which both
    skips the rfc822 container (is_multipart) and mangles nested-message bodies.
    """
    MIN_SIZE = 512
    SKIP_TYPES = {"application/pgp-signature", "application/pkcs7-signature"}
    out = []
    try:
        parts = list(msg.iter_attachments())
    except Exception:
        parts = [p for p in msg.walk()
                 if not p.is_multipart() and
                 (p.get_filename() or "attachment" in str(p.get("Content-Disposition") or "").lower())]

    for part in parts:
        ct = part.get_content_type()
        if ct in SKIP_TYPES:
            continue
        fname = part.get_filename()

        if ct == "message/rfc822":
            try:
                inner = part.get_content()      # -> email.message.EmailMessage
                nested_raw = inner.as_bytes()
                out.append(ParsedAttachment(
                    filename=(fname or "attached_message.eml"),
                    content_type="message/rfc822",
                    is_nested_message=True,
                    nested=parse_eml_bytes(nested_raw, source_kind="eml"),
                    source_index=len(out),
                ))
                continue
            except Exception:
                pass

        try:
            payload = part.get_content()
        except Exception:
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
        if isinstance(payload, str):
            payload = payload.encode("utf-8", "replace")
        if not isinstance(payload, (bytes, bytearray)) or len(payload) < MIN_SIZE:
            continue
        clean = (fname or "attachment_%d" % len(out))
        clean = clean.replace("/", "_").replace("\\", "_").replace("\x00", "")
        out.append(ParsedAttachment(
            filename=clean, content_type=ct, data=bytes(payload), source_index=len(out),
        ))
    return out


def iter_mbox(path: str) -> Iterator[ParsedEmail]:
    box = mailbox.mbox(path)
    try:
        for key in box.iterkeys():
            try:
                raw = box.get_bytes(key)
                yield parse_eml_bytes(raw, source_kind="mbox")
            except Exception as e:
                logger.error("mbox message %s failed in %s: %s", key, path, e)
    finally:
        box.close()


# ---------------------------------------------------------------------------
# .msg  (extract_msg)  -- route transport headers through the shared parser
# ---------------------------------------------------------------------------

def parse_msg(path: str) -> ParsedEmail:
    import extract_msg
    m = extract_msg.Message(path)

    headers = None
    hdr = getattr(m, "header", None)          # email.message.Message of transport hdrs
    if hdr is not None:
        try:
            headers = _parse_rfc822_headers(hdr)
        except Exception:
            headers = None
    if not headers or not any(headers.get(k) for k in ("from", "subject", "message_id")):
        # MAPI fallback: extract_msg uses camelCase (the old snake_case getattr
        # in _meta_from_msg silently returned None -- that was the .msg threading bug).
        headers = {
            "from": getattr(m, "sender", "") or "",
            "to": getattr(m, "to", "") or "",
            "cc": getattr(m, "cc", "") or "",
            "subject": getattr(m, "subject", "") or "",
            "message_id": (getattr(m, "messageId", None) or None),
            "in_reply_to": (getattr(m, "inReplyTo", None) or None),
            "references": _split_references(getattr(m, "references", "") or ""),
            "date": _msg_date(m),
        }

    # conversation_index: MAPI prop, extract_msg exposes it under varied names.
    ci = (getattr(m, "conversationIndex", None)
          or getattr(m, "conversation_index", None))
    if ci:
        headers["conversation_index"] = ci.hex() if isinstance(ci, (bytes, bytearray)) else str(ci)

    body_text = _decode(getattr(m, "body", None))
    body_html = _decode(getattr(m, "htmlBody", None))

    attachments = []
    for att in getattr(m, "attachments", []) or []:
        fname = (getattr(att, "longFilename", None)
                 or getattr(att, "shortFilename", None)
                 or "attachment_%d" % len(attachments))
        data = getattr(att, "data", None)
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        if not data or len(data) < 512:
            continue
        ct = getattr(att, "mimetype", None) or "application/octet-stream"
        attachments.append(ParsedAttachment(
            filename=str(fname).replace("/", "_").replace("\x00", ""),
            content_type=ct, data=bytes(data), source_index=len(attachments),
        ))

    pe = ParsedEmail(
        source_kind="msg", headers=headers,
        body_text=body_text, body_html=body_html,
        attachments=attachments, native_ext="msg",
    )
    try:
        pe.native_bytes = Path(path).read_bytes()
    except Exception:
        pe.native_bytes = None
    _finalize(pe, kind="msg")
    return pe


def _msg_date(m):
    d = getattr(m, "date", None)
    if isinstance(d, datetime):
        return d
    if isinstance(d, str) and d:
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(d)
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# PST / OST  (pypff, primary)
# ---------------------------------------------------------------------------

def iter_pst(path: str) -> Iterator[ParsedEmail]:
    """Walk every folder/message in a PST/OST. One ParsedEmail per message."""
    import pypff
    pff = pypff.file()
    pff.open(path)
    try:
        root = pff.get_root_folder()
        yield from _walk_folder(root)
    finally:
        pff.close()


def _walk_folder(folder) -> Iterator[ParsedEmail]:
    n_msgs = folder.get_number_of_sub_messages()
    for i in range(n_msgs):
        try:
            msg = folder.get_sub_message(i)
            yield _pst_message_to_parsed(msg)
        except Exception as e:
            logger.error("PST message %d in folder '%s' failed: %s",
                         i, _safe_folder_name(folder), e)
    for j in range(folder.get_number_of_sub_folders()):
        try:
            yield from _walk_folder(folder.get_sub_folder(j))
        except Exception as e:
            logger.error("PST subfolder %d failed: %s", j, e)


def _safe_folder_name(folder) -> str:
    try:
        return folder.get_name() or "?"
    except Exception:
        return "?"


def _pst_message_to_parsed(msg) -> ParsedEmail:
    raw_hdrs = None
    try:
        raw_hdrs = msg.get_transport_headers()
    except Exception:
        raw_hdrs = None

    if raw_hdrs and raw_hdrs.strip():
        headers = _parse_rfc822_headers(raw_hdrs)
    else:
        headers = {
            "from": _safe(msg.get_sender_name),
            "to": "", "cc": "",
            "subject": _safe(msg.get_subject) or _safe(msg.get_conversation_topic),
            "message_id": None, "in_reply_to": None, "references": [],
            "date": _pst_time(msg),
        }
    if not headers.get("date"):
        headers["date"] = _pst_time(msg)
    if not headers.get("subject"):
        headers["subject"] = _safe(msg.get_subject) or _safe(msg.get_conversation_topic)

    # conversation_index from the record set, if present.
    ci = _pst_record_bytes(msg, _PR_CONVERSATION_INDEX)
    if ci:
        headers["conversation_index"] = ci.hex()

    body_text = _decode(_safe(msg.get_plain_text_body, as_bytes=True))
    body_html = _decode(_safe(msg.get_html_body, as_bytes=True))
    if not body_text and not body_html:
        body_text = _decode(_safe(msg.get_rtf_body, as_bytes=True))

    attachments = _pst_attachments(msg)

    pe = ParsedEmail(
        source_kind="pst", headers=headers,
        body_text=body_text, body_html=body_html,
        attachments=attachments, native_ext="eml",
    )
    _finalize(pe, kind="pst")
    return pe


def _safe(getter, as_bytes: bool = False):
    try:
        v = getter()
    except Exception:
        return None
    return v


def _pst_time(msg):
    for getter in ("get_delivery_time", "get_client_submit_time", "get_creation_time"):
        try:
            v = getattr(msg, getter)()
            if isinstance(v, datetime):
                return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _pst_record_bytes(obj, target_entry_type: int) -> Optional[bytes]:
    """Pull a raw MAPI property value out of a pypff object's record sets."""
    try:
        for rs in obj.record_sets:
            for entry in rs.entries:
                try:
                    if entry.entry_type == target_entry_type:
                        return entry.data
                except Exception:
                    continue
    except Exception:
        return None
    return None


def _pst_record_string(obj, target_entry_type: int) -> Optional[str]:
    try:
        for rs in obj.record_sets:
            for entry in rs.entries:
                try:
                    if entry.entry_type == target_entry_type:
                        return entry.get_data_as_string()
                except Exception:
                    continue
    except Exception:
        return None
    return None


def _pst_record_int(obj, target_entry_type: int) -> Optional[int]:
    try:
        for rs in obj.record_sets:
            for entry in rs.entries:
                try:
                    if entry.entry_type == target_entry_type:
                        return entry.get_data_as_integer()
                except Exception:
                    continue
    except Exception:
        return None
    return None


def _pst_attachments(msg) -> list:
    out = []
    try:
        n = msg.get_number_of_attachments()
    except Exception:
        return out
    for i in range(n):
        try:
            att = msg.get_attachment(i)
        except Exception as e:
            logger.error("PST attachment %d unreadable: %s", i, e)
            continue

        method = _pst_record_int(att, _PR_ATTACH_METHOD)
        fname = (_pst_record_string(att, _PR_ATTACH_LONG_FILENAME)
                 or _pst_record_string(att, _PR_ATTACH_FILENAME)
                 or "attachment_%d" % i)
        fname = str(fname).replace("/", "_").replace("\\", "_").replace("\x00", "")
        ctype = _pst_record_string(att, _PR_ATTACH_MIME_TAG) or "application/octet-stream"

        # Embedded message (ATTACH_METHOD == 5): recurse via sub-item message.
        if method == _ATTACH_METHOD_EMBEDDED_MSG:
            nested_msg = None
            try:
                if att.get_number_of_sub_items() > 0:
                    nested_msg = att.get_sub_item(0)
            except Exception:
                nested_msg = None
            if nested_msg is not None:
                try:
                    out.append(ParsedAttachment(
                        filename=fname if fname.lower().endswith((".msg", ".eml"))
                                 else (fname + ".eml"),
                        content_type="message/rfc822",
                        is_nested_message=True,
                        nested=_pst_message_to_parsed(nested_msg),
                        source_index=i,
                    ))
                    continue
                except Exception as e:
                    logger.error("embedded PST message %d failed: %s", i, e)

        # Regular binary attachment.
        try:
            size = att.get_size()
            data = att.read_buffer(size) if size else b""
        except Exception as e:
            logger.error("PST attachment %d read failed: %s", i, e)
            continue
        if not data or len(data) < 512:
            continue
        out.append(ParsedAttachment(
            filename=fname, content_type=ctype, data=bytes(data), source_index=i,
        ))
    return out


# ---------------------------------------------------------------------------
# finalize + dispatch + explode
# ---------------------------------------------------------------------------

def _finalize(pe: ParsedEmail, kind: str) -> None:
    """Guarantee a Message-ID (synthesize if missing) so threading/dedup hold."""
    if not pe.headers.get("message_id"):
        pe.headers["message_id"] = _synthesize_message_id(pe.headers, kind)
        pe.message_id_synthetic = True


_EMAIL_DISPATCH = {
    ".eml": lambda p: iter([parse_eml(p)]),
    ".msg": lambda p: iter([parse_msg(p)]),
    ".pst": iter_pst,
    ".ost": iter_pst,
    ".mbox": iter_mbox,
}


def parse_email_source(path: str) -> Iterator[ParsedEmail]:
    """Dispatch by extension. PST/OST/mbox yield many; .eml/.msg yield one."""
    ext = Path(path).suffix.lower()
    handler = _EMAIL_DISPATCH.get(ext)
    if handler is None:
        raise ValueError("Unsupported email source extension: %s" % ext)
    yield from handler(path)


def explode(pe: ParsedEmail, _local_counter=None, _parent_id=None) -> list:
    """
    Flatten a ParsedEmail into ExplodedUnits: one email_body root, then one unit
    per attachment, recursing into nested messages. PURE -- stage 3 assigns
    UUIDs and resolves parent_local_id -> family_id/parent_id.

    Returns a list; element [0] of a top-level call is the thread/family root.
    """
    counter = _local_counter if _local_counter is not None else _Counter()
    units: list = []

    body_id = counter.next()
    units.append(ExplodedUnit(
        role="email_body", local_id=body_id, parent_local_id=_parent_id,
        is_attachment=(_parent_id is not None),  # nested message body IS an attachment
        attachment_index=None, filename=None,
        content_type="message/rfc822",
        data=pe.native_bytes, body_text=pe.body_text, body_html=pe.body_html,
        headers=pe.headers,
    ))

    for att in pe.attachments:
        if att.is_nested_message and att.nested is not None:
            child = explode(att.nested, _local_counter=counter, _parent_id=body_id)
            # tag the nested root with its attachment ordinal
            child[0].attachment_index = att.source_index
            child[0].filename = att.filename
            units.extend(child)
        else:
            units.append(ExplodedUnit(
                role="attachment", local_id=counter.next(), parent_local_id=body_id,
                is_attachment=True, attachment_index=att.source_index,
                filename=att.filename, content_type=att.content_type,
                data=att.data,
            ))
    return units


class _Counter:
    __slots__ = ("_n",)

    def __init__(self):
        self._n = -1

    def next(self) -> int:
        self._n += 1
        return self._n
