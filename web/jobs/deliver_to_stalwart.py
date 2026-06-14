#!/usr/bin/env python3
"""
jobs/deliver_to_stalwart.py

Delivery stage: drop connector-staged mail (email_routing_queue) into the
user's Stalwart mailbox. For each mapped user we open ONE EWS connection,
re-fetch each message's raw MIME by message_id, and JMAP Email/import it into
their Stalwart INBOX. Idempotent via email_routing_queue.stalwart_delivered.

Usage:  run(tenant_id, only_user=None, limit=None)
"""
import os, sys, json, base64, logging, urllib.request
sys.path.insert(0, "/app")
from jobs.exchange_sync import _get_db_conn, _get_credentials, _get_config

log = logging.getLogger("deliver_to_stalwart")

ST_H = os.getenv("STALWART_JMAP_URL", "http://10.10.0.10:8080/jmap").replace("/jmap", "")
ST_AUTH = "Basic " + base64.b64encode(
    (os.getenv("STALWART_ADMIN_USER", "admin") + ":" +
     os.getenv("STALWART_ADMIN_PASS", "Praesidium2026!")).encode()).decode()
_M = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"]
_P = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:principals", "urn:stalwart:jmap"]


def _st(url, data=None, ctype="application/json"):
    r = urllib.request.Request(url, data=data,
        headers={"Authorization": ST_AUTH, "Content-Type": ctype})
    return urllib.request.urlopen(r, timeout=30)


def _jmap(using, calls):
    return json.load(_st(ST_H + "/jmap",
        json.dumps({"using": using, "methodCalls": calls}).encode()))


def _stalwart_ctx(local_part):
    sess = json.load(_st(ST_H + "/jmap/session"))
    upl = sess["uploadUrl"]
    accts = {a["name"]: a["id"] for a in
             _jmap(_P, [["x:Account/get", {}, "t"]])["methodResponses"][0][1]["list"]}
    aid = accts.get(local_part.lower())
    if not aid:
        return None
    boxes = _jmap(_M, [["Mailbox/get", {"accountId": aid, "properties": ["id", "name", "role"]}, "t"]])["methodResponses"][0][1]["list"]
    inbox = next((b["id"] for b in boxes if b.get("role") == "inbox" or b["name"] == "Inbox"), None)
    return {"aid": aid, "inbox": inbox, "upload": upl.replace("{accountId}", aid)}


def _import(ctx, mime_bytes, received_iso, seen=True):
    blob = json.load(_st(ctx["upload"], mime_bytes, ctype="message/rfc822"))
    email = {"blobId": blob["blobId"], "mailboxIds": {ctx["inbox"]: True}}
    if seen:
        email["keywords"] = {"$seen": True}
    if received_iso:
        email["receivedAt"] = received_iso
    mr = _jmap(_M, [["Email/import", {"accountId": ctx["aid"], "emails": {"e": email}}, "t"]])["methodResponses"][0][1]
    return "e" in (mr.get("created") or {}), mr.get("notCreated")


def _ews_account(creds, ews_url, mailbox_email):
    from exchangelib import Credentials, Configuration, Account, IMPERSONATION
    from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter
    import urllib3; urllib3.disable_warnings()
    BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
    dom = creds.get("domain", "")
    full = f"{dom}\\{creds['username']}" if dom else creds["username"]
    cfg = Configuration(service_endpoint=ews_url,
                        credentials=Credentials(username=full, password=creds["password"]),
                        auth_type="NTLM")
    return Account(primary_smtp_address=mailbox_email, config=cfg,
                   autodiscover=False, access_type=IMPERSONATION)


def run(tenant_id, only_user=None, limit=None):
    conn = _get_db_conn()
    creds = _get_credentials(conn, tenant_id)
    cfg = _get_config(conn, tenant_id)
    ews_url = cfg.get("ews_url", "")
    cur = conn.cursor()

    cur.execute("""
        SELECT u.id uid, u.email, cem.entity_email
        FROM users u
        JOIN connector_entity_map cem ON cem.mapped_user_id = u.id
         AND cem.connector_type='exchange' AND cem.is_active
        WHERE trim(u.tenant_id)=trim(%s) AND u.email LIKE %s
        GROUP BY u.id, u.email, cem.entity_email
    """, (tenant_id, "%@hjmmlegal.com"))
    targets = cur.fetchall()
    if only_user:
        targets = [t for t in targets if t["email"] == only_user]

    grand = 0
    for t in targets:
        local = t["email"].split("@")[0]
        ctx = _stalwart_ctx(local)
        if not ctx or not ctx["inbox"]:
            log.warning("no stalwart mailbox for %s", t["email"]); continue
        c2 = conn.cursor()
        c2.execute("""SELECT message_id, received_at, COALESCE(is_read,true) is_read
                      FROM email_routing_queue
                      WHERE attorney_user_id=%s AND NOT stalwart_delivered
                        AND message_id IS NOT NULL AND message_id<>''
                      ORDER BY received_at DESC %s""" %
                   ("%s", ("LIMIT %d" % limit) if limit else ""), (t["uid"],))
        rows = c2.fetchall(); c2.close()
        if not rows:
            continue
        acct = _ews_account(creds, ews_url, t["entity_email"])
        done = 0
        for r in rows:
            mid = r["message_id"]
            try:
                items = list(acct.inbox.filter(message_id=mid).only("mime_content")[:1])
                if not items:
                    items = list(acct.sent.filter(message_id=mid).only("mime_content")[:1])
                if not items:
                    cur.execute("UPDATE email_routing_queue SET stalwart_delivered=true, stalwart_delivered_at=NOW(), error='mime-not-found' WHERE message_id=%s", (mid,)); conn.commit(); continue
                mime = items[0].mime_content
                if isinstance(mime, str): mime = mime.encode("utf-8", "replace")
                ok, nc = _import(ctx, mime,
                                 r["received_at"].isoformat() if r["received_at"] else None,
                                 seen=bool(r["is_read"]))
                if ok:
                    cur.execute("UPDATE email_routing_queue SET stalwart_delivered=true, stalwart_delivered_at=NOW() WHERE message_id=%s", (mid,))
                    conn.commit(); done += 1
                else:
                    log.warning("import failed %s: %s", mid, nc)
            except Exception as e:
                log.warning("deliver error %s: %s", mid, e)
                conn.rollback()
        log.info("[deliver] %s: delivered %d/%d", t["email"], done, len(rows))
        grand += done
    log.info("[deliver] DONE tenant=%s total=%d", tenant_id, grand)
    return grand


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    tid = sys.argv[1]
    ou = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "-" else None
    lim = int(sys.argv[3]) if len(sys.argv) > 3 else None
    run(tid, only_user=ou, limit=lim)
