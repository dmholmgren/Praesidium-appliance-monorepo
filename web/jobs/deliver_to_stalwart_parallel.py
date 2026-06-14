#!/usr/bin/env python3
"""
jobs/deliver_to_stalwart_parallel.py

Sharded-parallel Exchange->Stalwart migration. Splits each mailbox's history
into monthly date-range shards processed by N worker threads (each its own EWS
connection + DB connection). Disjoint date ranges => no cross-worker dupes;
a pre-built Message-ID set skips anything already in Stalwart. Idempotent.

Usage: run(tenant_id, only_user=None, workers=6)
"""
import sys, threading, queue, logging
from datetime import datetime, timezone
sys.path.insert(0, "/app")
from jobs.exchange_sync import _get_db_conn, _get_credentials, _get_config
from jobs.deliver_to_stalwart import _ews_account
from jobs.deliver_to_stalwart_bulk import _ctx, _existing_msgids, _flush, _norm, BATCH_SIZE

log = logging.getLogger("deliver_parallel")
WORKERS = 6


class _SeqPool:
    """serial map so _flush uploads in-thread (the 6 workers provide parallelism)"""
    def map(self, fn, it):
        return [fn(x) for x in it]


def _add_month(dt):
    return datetime(dt.year + (dt.month // 12), (dt.month % 12) + 1, 1, tzinfo=timezone.utc)


def _earliest(acct):
    dts = []
    for f in (acct.inbox, acct.sent):
        try:
            it = list(f.all().only("datetime_received").order_by("datetime_received")[:1])
            if it and it[0].datetime_received:
                d = it[0].datetime_received
                dts.append(datetime(d.year, d.month, 1, tzinfo=timezone.utc))
        except Exception as e:
            log.warning("earliest probe failed: %s", e)
    return min(dts) if dts else None


def _shards(start):
    now = datetime.now(timezone.utc)
    out, cur = [], start
    while cur < now:
        nxt = _add_month(cur)
        out.append((cur, nxt))
        cur = nxt
    return out


def _worker(wid, creds, ews_url, entity_email, ctx, seen, q, results):
    try:
        acct = _ews_account(creds, ews_url, entity_email)
    except Exception as e:
        log.warning("[w%d] EWS connect failed: %s", wid, e); results[wid] = 0; return
    conn = _get_db_conn(); cur = conn.cursor()
    pool = _SeqPool(); delivered = 0
    while True:
        try:
            s, e = q.get_nowait()
        except queue.Empty:
            break
        for fname in ("inbox", "sent"):
            target = ctx.get(fname); src = getattr(acct, fname, None)
            if not target or src is None:
                continue
            try:
                batch = []
                for msg in src.filter(datetime_received__gte=s, datetime_received__lt=e).only("mime_content", "message_id", "datetime_received", "is_read"):
                    nmid = _norm(getattr(msg, "message_id", None))
                    if nmid and nmid in seen:
                        continue
                    mime = getattr(msg, "mime_content", None)
                    if mime is None:
                        continue
                    if isinstance(mime, str):
                        mime = mime.encode("utf-8", "replace")
                    batch.append({"nmid": nmid, "mime": mime,
                                  "recv": msg.datetime_received.isoformat() if getattr(msg, "datetime_received", None) else None,
                                  "seen_flag": bool(getattr(msg, "is_read", True)),
                                  "raw_mid": getattr(msg, "message_id", "") or ""})
                    if len(batch) >= BATCH_SIZE:
                        delivered += _flush(ctx, target, batch, conn, cur, seen, pool); batch = []
                delivered += _flush(ctx, target, batch, conn, cur, seen, pool)
            except Exception as ex:
                log.warning("[w%d] shard %s..%s/%s error: %s", wid, s.date(), e.date(), fname, ex)
        q.task_done()
    results[wid] = delivered
    log.info("[w%d] done: %d delivered", wid, delivered)


def run(tenant_id, only_user=None, workers=WORKERS):
    conn = _get_db_conn(); creds = _get_credentials(conn, tenant_id); cfg = _get_config(conn, tenant_id)
    ews_url = cfg.get("ews_url", ""); cur = conn.cursor()
    cur.execute("""SELECT u.email, cem.entity_email FROM users u
        JOIN connector_entity_map cem ON cem.mapped_user_id=u.id
         AND cem.connector_type='exchange' AND cem.is_active
        WHERE trim(u.tenant_id)=trim(%s) AND u.email LIKE %s
        GROUP BY u.email, cem.entity_email""", (tenant_id, "%@hjmmlegal.com"))
    targets = cur.fetchall()
    if only_user:
        targets = [t for t in targets if t["email"] == only_user]

    grand = 0
    for t in targets:
        ctx = _ctx(t["email"].split("@")[0])
        if not ctx or not ctx["inbox"]:
            log.warning("no stalwart mailbox for %s", t["email"]); continue
        try:
            probe = _ews_account(creds, ews_url, t["entity_email"])
        except Exception as e:
            log.warning("EWS connect failed %s: %s", t["entity_email"], e); continue
        earliest = _earliest(probe)
        if not earliest:
            log.info("[parallel] %s: empty mailbox", t["email"]); continue
        seen = _existing_msgids(ctx["aid"])
        shards = _shards(earliest)
        log.info("[parallel] %s: %d shards (since %s), %d already in Stalwart, %d workers",
                 t["email"], len(shards), earliest.date(), len(seen), workers)
        q = queue.Queue()
        for sh in shards:
            q.put(sh)
        results = {}
        threads = [threading.Thread(target=_worker, args=(i, creds, ews_url, t["entity_email"], ctx, seen, q, results)) for i in range(workers)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        sub = sum(results.values())
        log.info("[parallel] %s DONE: %d delivered", t["email"], sub)
        grand += sub
    log.info("[parallel] ALL DONE tenant=%s total=%d", tenant_id, grand)
    return grand


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    tid = sys.argv[1]
    ou = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "-" else None
    w = int(sys.argv[3]) if len(sys.argv) > 3 else WORKERS
    run(tid, only_user=ou, workers=w)
