"""
comms_jmap.py - JMAP-backed read helpers for the Comms email viewer.

Maps Stalwart JMAP envelopes -> the JSON shape the comms frontend already
consumes (legacy email_routing_queue field names), so the React pages need no
shape churn.

Phase 1: firm mailbox (admin-auth, accountId via _account_id).
Phase 2: pass a per-user connector descriptor via account_id + auth=(user,pass)
for personal boxes on other Stalwart servers; `source` tags rows firm|personal
so the combined view can color-offset personal mail.
"""
from __future__ import annotations
from typing import Optional, Any
from datetime import datetime, timedelta, timezone

from core.services import stalwart_mailbox as sm
from core.services.matter_mail_folders import _account_id

CURRENT_WINDOW_DAYS = 60


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _addr0(lst):
    if lst and isinstance(lst, list) and lst[0]:
        em = lst[0].get("email") or ""
        return em, (lst[0].get("name") or em)
    return "", ""


def _emails(lst):
    return [a.get("email") for a in (lst or []) if a.get("email")]


def envelope_to_row(e: dict, *, source: str = "firm") -> dict:
    """JMAP Email envelope -> frontend message row (legacy field names)."""
    kw = e.get("keywords") or {}
    fe, fd = _addr0(e.get("from"))
    return {
        "id": e.get("id"),
        "subject": e.get("subject") or "(no subject)",
        "from_email": fe,
        "from_display": fd,
        "to_emails": _emails(e.get("to")),
        "cc_emails": _emails(e.get("cc")),
        "received_at": e.get("receivedAt"),
        "body_preview": e.get("preview") or "",
        "has_attachments": bool(e.get("hasAttachment")),
        "attachment_names": [],
        "attachment_count": 0,
        "is_read": bool(kw.get("$seen")),
        "is_flagged": bool(kw.get("$flagged")),
        "importance": None,
        "conversation_id": e.get("threadId"),
        "conversation_topic": e.get("subject"),
        "routing_status": None,
        "matched_matter_id": None,
        "match_confidence": None,
        "match_signals": None,
        "filed_to_dms": None,
        "filing_status": None,
        "filed_matter_id": None,
        "matter_name": None,
        "client_name": None,
        "source": source,
    }


def _role_id(mailboxes, role):
    for m in mailboxes:
        if m.get("role") == role:
            return m.get("id")
    return None


def _name_id(mailboxes, name):
    for m in mailboxes:
        if (m.get("name") or "").lower() == (name or "").lower():
            return m.get("id")
    return None


async def resolve_mailbox(account_id, folder, *, auth=None, url=None):
    """Map a UI folder token -> (mailbox_id, mailboxes[]). Unknown -> by name."""
    mbs = await sm.list_mailboxes(account_id, auth=auth, url=url)
    role_map = {"inbox": "inbox", "current": "inbox", "historic": "inbox",
                "sent": "sent", "drafts": "drafts", "junk": "junk",
                "trash": "trash"}
    if folder in ("all", None, ""):
        return None, mbs
    if folder in role_map:
        return _role_id(mbs, role_map[folder]), mbs
    return _name_id(mbs, folder), mbs


async def _query(account_id, *, mailbox_id=None, since=None, before=None,
                 text=None, limit=50, position=0, auth=None, url=None):
    """JMAP Email/query (+get envelopes) with AND of all conditions."""
    conds = []
    if mailbox_id:
        conds.append({"inMailbox": mailbox_id})
    if since:
        conds.append({"after": since})
    if before:
        conds.append({"before": before})
    if text:
        conds.append({"text": text})
    if not conds:
        filt = None
    elif len(conds) == 1:
        filt = conds[0]
    else:
        filt = {"operator": "AND", "conditions": conds}
    q: dict[str, Any] = {
        "accountId": account_id,
        "sort": [{"property": "receivedAt", "isAscending": False}],
        "limit": limit, "position": position, "calculateTotal": True,
    }
    if filt:
        q["filter"] = filt
    result = await sm._jmap([
        ["Email/query", q, "q"],
        ["Email/get", {
            "accountId": account_id,
            "#ids": {"resultOf": "q", "name": "Email/query", "path": "/ids/*"},
            "properties": sm.ENVELOPE_PROPS,
        }, "g"],
    ], auth=auth, url=url)
    qr, gr = sm._resp(result, 0), sm._resp(result, 1)
    return qr.get("total") or 0, gr.get("list", [])


async def messages_view(user_email, *, folder="inbox", time="current",
                        search=None, page=1, page_size=50, sort="newest",
                        source="firm", auth=None, account_id=None, url=None):
    """Paginated message list mapped to the frontend shape."""
    acct = account_id or await _account_id(user_email)
    if not acct:
        return {"messages": [], "total": 0, "page": page,
                "page_size": page_size, "pages": 0}
    mbx_id, _mbs = await resolve_mailbox(acct, folder, auth=auth, url=url)
    since = before = None
    if folder == "historic" or time == "historic":
        before = _iso_days_ago(CURRENT_WINDOW_DAYS)
    elif folder in ("inbox", "current"):
        since = _iso_days_ago(CURRENT_WINDOW_DAYS)
    offset = (page - 1) * page_size
    total, msgs = await _query(acct, mailbox_id=mbx_id, since=since,
                               before=before, text=search, limit=page_size,
                               position=offset, auth=auth, url=url)
    rows = [envelope_to_row(m, source=source) for m in msgs]
    return {"messages": rows, "total": total, "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size}


def _body(msg, kind):
    parts = msg.get(kind) or []
    vals = msg.get("bodyValues") or {}
    out = []
    for p in parts:
        pid = p.get("partId")
        if pid and pid in vals:
            out.append(vals[pid].get("value") or "")
    return "\n".join(out)


async def message_detail(user_email, message_id, *, auth=None, account_id=None,
                         source="firm", url=None):
    acct = account_id or await _account_id(user_email)
    if not acct:
        return None
    msg = await sm.get_message(acct, message_id, auth=auth, url=url)
    if not msg:
        return None
    row = envelope_to_row(msg, source=source)
    row["body_html"] = _body(msg, "htmlBody")
    row["body_text"] = _body(msg, "textBody")
    row["attachment_names"] = [a.get("name") for a in (msg.get("attachments") or []) if a.get("name")]
    row["attachment_count"] = len(msg.get("attachments") or [])
    return row


async def thread_view(user_email, thread_id, *, auth=None, account_id=None,
                      source="firm", url=None):
    acct = account_id or await _account_id(user_email)
    if not acct:
        return {"conversation_id": thread_id, "messages": [], "count": 0}
    tr = await sm._jmap([["Thread/get", {"accountId": acct, "ids": [thread_id]}, "t"]],
                        auth=auth, url=url)
    tlist = sm._resp(tr).get("list", [])
    email_ids = tlist[0].get("emailIds", []) if tlist else []
    if not email_ids:
        return {"conversation_id": thread_id, "messages": [], "count": 0}
    gr = await sm._jmap([["Email/get", {"accountId": acct, "ids": email_ids,
                                        "properties": sm.ENVELOPE_PROPS}, "g"]],
                        auth=auth, url=url)
    msgs = sm._resp(gr).get("list", [])
    msgs.sort(key=lambda m: m.get("receivedAt") or "")
    rows = [envelope_to_row(m, source=source) for m in msgs]
    return {"conversation_id": thread_id, "messages": rows, "count": len(rows)}


async def stats(user_email, *, auth=None, account_id=None, url=None):
    acct = account_id or await _account_id(user_email)
    if not acct:
        return {"inbox_total": 0, "inbox_unread": 0, "sent_total": 0}
    mbs = await sm.list_mailboxes(acct, auth=auth, url=url)
    by_role = {m.get("role"): m for m in mbs if m.get("role")}
    inbox = by_role.get("inbox", {})
    sent = by_role.get("sent", {})
    return {
        "inbox_total": inbox.get("totalEmails", 0),
        "inbox_unread": inbox.get("unreadEmails", 0),
        "sent_total": sent.get("totalEmails", 0),
        "mailboxes": [{"id": m.get("id"), "name": m.get("name"),
                       "role": m.get("role"), "total": m.get("totalEmails", 0),
                       "unread": m.get("unreadEmails", 0)} for m in mbs],
    }


# =====================================================================
# Multi-source orchestration (firm / personal / combined)
# Added Phase 2: fans messages_view/stats/detail across MailboxConnectors.
# =====================================================================
import asyncio as _asyncio
from core.services import mail_connectors as _mc


def _row_sort_key(r):
    return r.get("received_at") or ""


async def view_messages(user_email, *, source="firm", folder="inbox",
                        time="current", search=None, page=1, page_size=50,
                        sort="newest"):
    """Unified message list across the connectors backing `source`.

    Single-source views delegate straight through. Combined view over-fetches
    page*page_size from each connector, merges by received_at desc, then slices
    the requested page. Each row carries its `source` tag (+ color for personal).
    """
    conns = await _mc.resolve_connectors(user_email, source)
    if not conns:
        return {"messages": [], "total": 0, "page": page,
                "page_size": page_size, "pages": 0, "sources": []}

    if len(conns) == 1:
        c = conns[0]
        res = await messages_view(user_email, folder=folder, time=time,
                                  search=search, page=page, page_size=page_size,
                                  sort=sort, source=c.source, auth=c.auth,
                                  account_id=c.account_id, url=c.jmap_url)
        if c.color:
            for m in res.get("messages", []):
                m["source_color"] = c.color
        res["sources"] = [c.source]
        return res

    # combined: over-fetch from each, merge, slice
    fetch_n = page * page_size
    pages_per = (fetch_n + page_size - 1) // page_size

    async def _pull(c):
        # pull enough rows from this connector to cover the requested window
        acc, total = [], 0
        for p in range(1, pages_per + 1):
            r = await messages_view(user_email, folder=folder, time=time,
                                    search=search, page=p, page_size=page_size,
                                    sort=sort, source=c.source, auth=c.auth,
                                    account_id=c.account_id, url=c.jmap_url)
            total = r.get("total", 0)
            ms = r.get("messages", [])
            if c.color:
                for m in ms:
                    m["source_color"] = c.color
            acc.extend(ms)
            if len(acc) >= fetch_n or not ms:
                break
        return total, acc

    results = await _asyncio.gather(*[_pull(c) for c in conns],
                                    return_exceptions=True)
    merged, grand_total = [], 0
    for c, res in zip(conns, results):
        if isinstance(res, Exception):
            continue
        total, rows = res
        grand_total += total
        merged.extend(rows)

    merged.sort(key=_row_sort_key, reverse=True)
    offset = (page - 1) * page_size
    window = merged[offset:offset + page_size]
    return {"messages": window, "total": grand_total, "page": page,
            "page_size": page_size,
            "pages": (grand_total + page_size - 1) // page_size,
            "sources": [c.source for c in conns]}


async def view_stats(user_email, *, source="firm"):
    """Summed inbox/sent stats across the connectors backing `source`."""
    conns = await _mc.resolve_connectors(user_email, source)
    if not conns:
        return {"inbox_total": 0, "inbox_unread": 0, "sent_total": 0,
                "by_source": {}, "sources": []}
    out = {"inbox_total": 0, "inbox_unread": 0, "sent_total": 0,
           "by_source": {}, "sources": [c.source for c in conns]}
    results = await _asyncio.gather(*[
        stats(user_email, auth=c.auth, account_id=c.account_id, url=c.jmap_url)
        for c in conns], return_exceptions=True)
    for c, st in zip(conns, results):
        if isinstance(st, Exception):
            out["by_source"][c.source] = {"error": str(st)}
            continue
        out["inbox_total"] += st.get("inbox_total", 0)
        out["inbox_unread"] += st.get("inbox_unread", 0)
        out["sent_total"] += st.get("sent_total", 0)
        out["by_source"][c.source] = st
    return out


async def _connector_for(user_email, source):
    """Resolve the single connector matching a source tag (for detail/thread)."""
    src = (source or "firm").lower()
    if src == "personal":
        return await _mc.get_personal_connector(user_email)
    return await _mc.get_firm_connector(user_email)


async def view_message_detail(user_email, message_id, *, source="firm"):
    c = await _connector_for(user_email, source)
    if not c or not c.ok:
        return None
    row = await message_detail(user_email, message_id, auth=c.auth,
                               account_id=c.account_id, source=c.source,
                               url=c.jmap_url)
    if row and c.color:
        row["source_color"] = c.color
    return row


async def view_thread(user_email, thread_id, *, source="firm"):
    c = await _connector_for(user_email, source)
    if not c or not c.ok:
        return {"conversation_id": thread_id, "messages": [], "count": 0}
    res = await thread_view(user_email, thread_id, auth=c.auth,
                            account_id=c.account_id, source=c.source,
                            url=c.jmap_url)
    if c.color:
        for m in res.get("messages", []):
            m["source_color"] = c.color
    return res
