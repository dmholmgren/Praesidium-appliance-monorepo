#!/usr/bin/env python3
"""
jobs/deliver_to_stalwart_bulk.py  (batched + concurrent uploads)

Bulk Exchange->Stalwart migration. Per mailbox: one EWS connection, stream each
folder with batched GetItem (mime_content). Imports in BATCHES: blobs uploaded
concurrently (thread pool), then ONE Email/import per batch. Dedup by Message-ID
against the Stalwart account -> idempotent / re-runnable.

Usage: run(tenant_id, only_user=None, folders=('inbox','sent'), per_mailbox_limit=None)
"""
import sys, json, logging
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/app")
from jobs.exchange_sync import _get_db_conn, _get_credentials, _get_config
from jobs.deliver_to_stalwart import _jmap, _st, _ews_account, _M, _P, ST_H

log = logging.getLogger("deliver_to_stalwart_bulk")

BATCH_SIZE = 50
UPLOAD_WORKERS = 8


def _norm(mid):
    return (mid or "").strip().strip("<>").strip().lower()


def _upload(upload_url, mime_bytes):
    try:
        return json.load(_st(upload_url, mime_bytes, ctype="message/rfc822")).get("blobId")
    except Exception as e:
        log.warning("blob upload failed: %s", e)
        return None


def _ctx(local_part):
    sess = json.load(_st(ST_H + "/jmap/session"))
    upl = sess["uploadUrl"]
    accts = {a["name"]: a["id"] for a in _jmap(_P, [["x:Account/get", {}, "t"]])["methodResponses"][0][1]["list"]}
    aid = accts.get(local_part.lower())
    if not aid:
        return None
    boxes = _jmap(_M, [["Mailbox/get", {"accountId": aid, "properties": ["id", "name", "role"]}, "t"]])["methodResponses"][0][1]["list"]
    def find(roles, names):
        return next((b["id"] for b in boxes if b.get("role") in roles or b["name"] in names), None)
    return {"aid": aid, "upload": upl.replace("{accountId}", aid),
            "inbox": find(("inbox",), ("Inbox",)),
            "sent": find(("sent",), ("Sent Items", "Sent"))}


def _existing_msgids(aid):
    ids, pos = [], 0
    while True:
        r = _jmap(_M, [["Email/query", {"accountId": aid, "limit": 2000, "position": pos, "calculateTotal": True}, "t"]])["methodResponses"][0][1]
        batch = r.get("ids", [])
        ids += batch; pos += len(batch)
        if not batch or pos >= r.get("total", 0):
            break
    seen = set()
    for i in range(0, len(ids), 500):
        got = _jmap(_M, [["Email/get", {"accountId": aid, "ids": ids[i:i + 500], "properties": ["messageId"]}, "t"]])["methodResponses"][0][1]["list"]
        for e in got:
            for m in (e.get("messageId") or []):
                seen.add(_norm(m))
    return seen


def _flush(ctx, target, batch, conn, cur, seen, pool):
    if not batch:
        return 0
    blobs = list(pool.map(lambda it: _upload(ctx["upload"], it["mime"]), batch))
    emails, idx_map = {}, {}
    for i, (it, blob) in enumerate(zip(batch, blobs)):
        if not blob:
            continue
        cid = "e%d" % i
        em = {"blobId": blob, "mailboxIds": {target: True}}
        if it["seen_flag"]:
            em["keywords"] = {"$seen": True}
        if it["recv"]:
            em["receivedAt"] = it["recv"]
        emails[cid] = em
        idx_map[cid] = it
    if not emails:
        return 0
    resp = _jmap(_M, [["Email/import", {"accountId": ctx["aid"], "emails": emails}, "t"]])["methodResponses"][0][1]
    created = resp.get("created") or {}
    nc = resp.get("notCreated") or {}
    if nc:
        log.warning("notCreated %d (e.g. %s)", len(nc), list(nc.items())[:1])
    for cid in created:
        it = idx_map.get(cid)
        if not it:
            continue
        if it["nmid"]:
            seen.add(it["nmid"])
        if it["raw_mid"]:
            cur.execute("UPDATE email_routing_queue SET stalwart_delivered=true, stalwart_delivered_at=NOW() WHERE message_id=%s OR internet_message_id=%s", (it["raw_mid"], it["raw_mid"]))
    conn.commit()
    return len(created)


def run(tenant_id, only_user=None, folders=("inbox", "sent"), per_mailbox_limit=None):
    conn = _get_db_conn()
    creds = _get_credentials(conn, tenant_id)
    cfg = _get_config(conn, tenant_id)
    ews_url = cfg.get("ews_url", "")
    cur = conn.cursor()
    cur.execute("""
        SELECT u.id uid, u.email, cem.entity_email FROM users u
        JOIN connector_entity_map cem ON cem.mapped_user_id=u.id
         AND cem.connector_type='exchange' AND cem.is_active
        WHERE trim(u.tenant_id)=trim(%s) AND u.email LIKE %s
        GROUP BY u.id,u.email,cem.entity_email
    """, (tenant_id, "%@hjmmlegal.com"))
    targets = cur.fetchall()
    if only_user:
        targets = [t for t in targets if t["email"] == only_user]

    grand = 0
    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
        for t in targets:
            ctx = _ctx(t["email"].split("@")[0])
            if not ctx or not ctx["inbox"]:
                log.warning("no stalwart mailbox for %s", t["email"]); continue
            try:
                acct = _ews_account(creds, ews_url, t["entity_email"])
            except Exception as e:
                log.warning("EWS connect failed %s: %s", t["entity_email"], e); continue
            seen = _existing_msgids(ctx["aid"])
            log.info("[bulk] %s: %d already in Stalwart", t["email"], len(seen))
            done = 0
            for fname in folders:
                target = ctx.get(fname)
                src = getattr(acct, fname, None)
                if not target or src is None:
                    continue
                qs = src.all().only("mime_content", "message_id", "datetime_received", "is_read").order_by("-datetime_received")
                if per_mailbox_limit:
                    qs = qs[:per_mailbox_limit]
                batch = []
                for msg in qs:
                    try:
                        nmid = _norm(getattr(msg, "message_id", None))
                        if nmid and nmid in seen:
                            continue
                        mime = msg.mime_content
                        if mime is None:
                            continue
                        if isinstance(mime, str):
                            mime = mime.encode("utf-8", "replace")
                        batch.append({
                            "nmid": nmid, "mime": mime,
                            "recv": msg.datetime_received.isoformat() if getattr(msg, "datetime_received", None) else None,
                            "seen_flag": bool(getattr(msg, "is_read", True)),
                            "raw_mid": getattr(msg, "message_id", "") or "",
                        })
                        if len(batch) >= BATCH_SIZE:
                            done += _flush(ctx, target, batch, conn, cur, seen, pool); batch = []
                    except Exception as e:
                        log.warning("msg error: %s", e)
                done += _flush(ctx, target, batch, conn, cur, seen, pool)
                log.info("[bulk] %s/%s: delivered %d so far", t["email"], fname, done)
            log.info("[bulk] %s DONE: %d delivered", t["email"], done)
            grand += done
    log.info("[bulk] ALL DONE tenant=%s total=%d", tenant_id, grand)
    return grand


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    tid = sys.argv[1]
    ou = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "-" else None
    lim = int(sys.argv[3]) if len(sys.argv) > 3 else None
    run(tid, only_user=ou, per_mailbox_limit=lim)
