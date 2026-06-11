"""
core/services/stalwart_mailbox.py
Stalwart Mail — user-side mailbox client (JMAP)

Read mailboxes/threads/messages and manage folder membership for an account.
Foundation for (a) the platform webmail read view and (b) the matter -> IMAP
folder projection (shared matter mailboxes).

Distinct from stalwart_service.py, which manages *account* provisioning via the
x: management methods. This module speaks the standard JMAP *mail* methods:
  - Mailbox/get, Mailbox/set        (folders)
  - Email/query, Email/get, Email/set (messages + mailboxIds membership)

using: core + mail + mail:share  (share enables shared matter mailboxes)
Auth: admin basic auth by default; pass `auth=(user,pass)` for per-account /
shared-principal access once SSO/credential resolution is wired (step 2).

Author: Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Optional

import httpx

log = logging.getLogger("praesidium.services.stalwart_mailbox")

STALWART_JMAP_URL = os.getenv("STALWART_JMAP_URL", "http://10.10.0.10:8080/jmap")
STALWART_ADMIN_USER = os.getenv("STALWART_ADMIN_USER", "admin")
STALWART_ADMIN_PASS = os.getenv("STALWART_ADMIN_PASS", "Praesidium2026!")

MAIL_USING = [
    "urn:ietf:params:jmap:core",
    "urn:ietf:params:jmap:mail",
    "urn:ietf:params:jmap:mail:share",
]

ENVELOPE_PROPS = [
    "id", "threadId", "mailboxIds", "keywords", "from", "to", "cc",
    "subject", "receivedAt", "sentAt", "size", "preview", "hasAttachment",
]


async def _jmap(method_calls: list[list], *, auth=None, using=None,
                timeout: float = 15.0) -> dict:
    """Execute a JMAP mail request against Stalwart."""
    payload = {"using": using or MAIL_USING, "methodCalls": method_calls}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            STALWART_JMAP_URL,
            json=payload,
            auth=auth or (STALWART_ADMIN_USER, STALWART_ADMIN_PASS),
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


def _resp(result: dict, idx: int = 0) -> dict:
    """Extract method response args at index; raise on JMAP error."""
    mr = result.get("methodResponses", [])
    if len(mr) <= idx:
        return {}
    name, args, _tag = mr[idx]
    if name == "error":
        raise RuntimeError(f"JMAP error: {args}")
    return args


# -- Mailboxes (folders) --

async def list_mailboxes(account_id: str, *, auth=None) -> list[dict]:
    """Return the full mailbox (folder) tree for an account."""
    result = await _jmap([
        ["Mailbox/get", {
            "accountId": account_id,
            "properties": ["id", "name", "role", "parentId", "sortOrder",
                           "totalEmails", "unreadEmails"],
        }, "m"],
    ], auth=auth)
    return _resp(result).get("list", [])


async def create_mailbox(account_id: str, name: str, *,
                         parent_id: Optional[str] = None,
                         role: Optional[str] = None, auth=None) -> dict:
    """Create a folder. Returns {'success': True, 'id': ...} or error."""
    obj: dict[str, Any] = {"name": name, "parentId": parent_id}
    if role:
        obj["role"] = role
    result = await _jmap([
        ["Mailbox/set", {"accountId": account_id, "create": {"new": obj}}, "c"],
    ], auth=auth)
    args = _resp(result)
    if "new" in args.get("created", {}):
        return {"success": True, "id": args["created"]["new"]["id"]}
    return {"success": False, "error": args.get("notCreated", {}).get("new")}


async def find_or_create_mailbox(account_id: str, name: str, *,
                                 parent_id: Optional[str] = None,
                                 role: Optional[str] = None, auth=None) -> str:
    """Idempotent: return existing folder id matching (name, parent) or create it."""
    for b in await list_mailboxes(account_id, auth=auth):
        if b.get("name") == name and b.get("parentId") == parent_id:
            return b["id"]
    res = await create_mailbox(account_id, name, parent_id=parent_id,
                               role=role, auth=auth)
    if not res.get("success"):
        raise RuntimeError(f"create_mailbox({name}) failed: {res.get('error')}")
    return res["id"]


# -- Messages --

async def query_messages(account_id: str, *, mailbox_id: Optional[str] = None,
                         since: Optional[str] = None, before: Optional[str] = None,
                         limit: int = 50, position: int = 0, auth=None) -> dict:
    """
    List message envelopes newest-first, optionally scoped to a mailbox and a
    received-date window (since/before = ISO-8601 UTC). Drives the 60-90 day view.
    Returns {"total": int, "ids": [...], "messages": [envelope, ...]}.
    """
    filt: dict[str, Any] = {}
    if mailbox_id:
        filt["inMailbox"] = mailbox_id
    if since:
        filt["after"] = since
    if before:
        filt["before"] = before
    q: dict[str, Any] = {
        "accountId": account_id,
        "sort": [{"property": "receivedAt", "isAscending": False}],
        "limit": limit, "position": position, "calculateTotal": True,
    }
    if filt:
        q["filter"] = filt
    result = await _jmap([
        ["Email/query", q, "q"],
        ["Email/get", {
            "accountId": account_id,
            "#ids": {"resultOf": "q", "name": "Email/query", "path": "/ids/*"},
            "properties": ENVELOPE_PROPS,
        }, "g"],
    ], auth=auth)
    qr, gr = _resp(result, 0), _resp(result, 1)
    return {"total": qr.get("total"), "ids": qr.get("ids", []),
            "messages": gr.get("list", [])}


async def get_message(account_id: str, email_id: str, *, auth=None) -> Optional[dict]:
    """Fetch a single message including body values."""
    result = await _jmap([
        ["Email/get", {
            "accountId": account_id, "ids": [email_id],
            "properties": ENVELOPE_PROPS + ["bodyValues", "textBody", "htmlBody",
                                            "attachments"],
            "fetchAllBodyValues": True, "maxBodyValueBytes": 500000,
        }, "g"],
    ], auth=auth)
    lst = _resp(result).get("list", [])
    return lst[0] if lst else None


# -- Folder membership (matter-projection primitive) --

async def add_message_to_mailbox(account_id: str, email_id: str,
                                 mailbox_id: str, *, auth=None) -> dict:
    """
    Add a mailbox to a message WITHOUT removing existing ones (JMAP patch), so a
    message can live in INBOX *and* Matters/<name> at once. This is the projection
    primitive: file an email into a matter folder while leaving it in place.
    """
    result = await _jmap([
        ["Email/set", {"accountId": account_id,
                       "update": {email_id: {f"mailboxIds/{mailbox_id}": True}}}, "u"],
    ], auth=auth)
    args = _resp(result)
    if email_id in (args.get("updated") or {}):
        return {"success": True}
    return {"success": False, "error": (args.get("notUpdated") or {}).get(email_id)}


async def set_message_mailboxes(account_id: str, email_id: str,
                                mailbox_ids: Iterable[str], *, auth=None) -> dict:
    """Replace the full set of mailboxes a message belongs to."""
    mids = {mid: True for mid in mailbox_ids}
    result = await _jmap([
        ["Email/set", {"accountId": account_id,
                       "update": {email_id: {"mailboxIds": mids}}}, "u"],
    ], auth=auth)
    args = _resp(result)
    if email_id in (args.get("updated") or {}):
        return {"success": True}
    return {"success": False, "error": (args.get("notUpdated") or {}).get(email_id)}


# -- Sending (JMAP EmailSubmission -> outbound smarthost) --

SUBMISSION_USING = MAIL_USING + [
    "urn:ietf:params:jmap:submission",
    "urn:ietf:params:jmap:blob",
]


def _addr_list(s: Optional[str]) -> list[dict]:
    """'a@x.com, b@y.com' -> [{'email': 'a@x.com'}, ...]"""
    if not s:
        return []
    return [{"email": a.strip()} for a in s.split(",") if a.strip()]


async def _upload_blob(account_id: str, content: bytes, content_type: str,
                       *, auth=None) -> str:
    """Upload an attachment blob; returns blobId."""
    base = STALWART_JMAP_URL.rsplit("/jmap", 1)[0]
    url = f"{base}/jmap/upload/{account_id}/"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url, content=content,
            auth=auth or (STALWART_ADMIN_USER, STALWART_ADMIN_PASS),
            headers={"Content-Type": content_type},
        )
        resp.raise_for_status()
        return resp.json()["blobId"]


async def send_message(account_id: str, *, from_addr: str, to: str, cc: str = "",
                       bcc: str = "", subject: str = "", text: str = "",
                       html: str = "", attachments: Optional[list] = None,
                       in_reply_to: Optional[str] = None,
                       references: Optional[str] = None, auth=None) -> dict:
    """
    Send from `account_id` via Stalwart: build the email in Drafts, submit it
    (Stalwart queues -> outbound strategy -> Vipre MX1 smarthost), move it to Sent
    on success. attachments = list of (filename, content_type, bytes).
    Returns {"success": True, "email_id", "submission_id"} or an error dict.
    """
    boxes = await list_mailboxes(account_id, auth=auth)
    by_role = {b.get("role"): b["id"] for b in boxes if b.get("role")}
    drafts_id, sent_id = by_role.get("drafts"), by_role.get("sent")
    if not drafts_id or not sent_id:
        return {"success": False, "error": f"missing drafts/sent mailbox: {by_role}"}

    to_l, cc_l, bcc_l = _addr_list(to), _addr_list(cc), _addr_list(bcc)

    email_obj: dict[str, Any] = {
        "mailboxIds": {drafts_id: True},
        "keywords": {"$draft": True},
        "from": [{"email": from_addr}],
        "to": to_l,
        "subject": subject,
    }
    if cc_l:
        email_obj["cc"] = cc_l
    if bcc_l:
        email_obj["bcc"] = bcc_l
    if in_reply_to:
        email_obj["inReplyTo"] = [in_reply_to]
        email_obj["references"] = references.split() if references else [in_reply_to]

    body_values: dict[str, Any] = {}
    if text or not html:
        body_values["t"] = {"value": text or ""}
        email_obj["textBody"] = [{"partId": "t", "type": "text/plain"}]
    if html:
        body_values["h"] = {"value": html}
        email_obj["htmlBody"] = [{"partId": "h", "type": "text/html"}]
    email_obj["bodyValues"] = body_values

    if attachments:
        att_objs = []
        for (fname, ctype, data) in attachments:
            blob_id = await _upload_blob(account_id, data, ctype, auth=auth)
            att_objs.append({"blobId": blob_id, "type": ctype, "name": fname,
                             "disposition": "attachment"})
        if att_objs:
            email_obj["attachments"] = att_objs

    rcpt = [{"email": a["email"]} for a in (to_l + cc_l + bcc_l)]

    identity_id = await _resolve_identity(account_id, from_addr, auth=auth)
    if not identity_id:
        return {"success": False, "stage": "identity",
                "error": "no sending identity available"}

    result = await _jmap([
        ["Email/set", {"accountId": account_id, "create": {"draft": email_obj}}, "c"],
        ["EmailSubmission/set", {
            "accountId": account_id,
            "create": {"sub": {
                "emailId": "#draft",
                "identityId": identity_id,
                "envelope": {"mailFrom": {"email": from_addr}, "rcptTo": rcpt},
            }},
            "onSuccessUpdateEmail": {"#sub": {
                f"mailboxIds/{sent_id}": True,
                f"mailboxIds/{drafts_id}": None,
                "keywords/$draft": None,
                "keywords/$seen": True,
            }},
        }, "s"],
    ], using=SUBMISSION_USING, auth=auth, timeout=30.0)

    set_args, sub_args = _resp(result, 0), _resp(result, 1)
    if "draft" not in set_args.get("created", {}):
        return {"success": False, "stage": "email_create",
                "error": set_args.get("notCreated", {}).get("draft")}
    email_id = set_args["created"]["draft"]["id"]
    if "sub" not in sub_args.get("created", {}):
        return {"success": False, "stage": "submission", "email_id": email_id,
                "error": sub_args.get("notCreated", {}).get("sub")}
    return {"success": True, "email_id": email_id,
            "submission_id": sub_args["created"]["sub"].get("id")}


async def _resolve_identity(account_id: str, from_addr: str, *, auth=None):
    """Return an Identity id for from_addr, creating one if none exists."""
    result = await _jmap([["Identity/get", {"accountId": account_id}, "i"]],
                         using=SUBMISSION_USING, auth=auth)
    identities = _resp(result).get("list", [])
    for idn in identities:
        if (idn.get("email") or "").lower() == from_addr.lower():
            return idn["id"]
    if identities:
        return identities[0]["id"]
    cr = await _jmap([["Identity/set", {"accountId": account_id,
            "create": {"id1": {"name": from_addr.split("@")[0], "email": from_addr}}}, "ic"]],
            using=SUBMISSION_USING, auth=auth)
    return _resp(cr).get("created", {}).get("id1", {}).get("id")
