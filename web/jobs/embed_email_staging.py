"""
jobs/embed_email_staging.py

Parallel extract -> chunk -> embed (LOCAL modernbert) of email bodies + staged
attachments into email_chunks / email_chunk_embeddings (staging space).
Idempotent per (source_type, source_id). The kNN match vs DMS is a separate
step, pending DMS matter-attribution. RunPod is reserved for bulk; this is local.

run(tenant_id, only_email='dennis@hjmmlegal.com', limit=25, workers=6)
"""
from __future__ import annotations
import sys, json, uuid, logging, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, "/app")
from jobs.exchange_sync import _get_db_conn
from modules.ediscovery.services.text_extraction import extract_text

log = logging.getLogger("embed_email_staging")
EMBED_URL = "http://praesidium-embed:8000/embed"
CHUNK, OVERLAP, MAX_CHUNKS, BATCH = 1800, 200, 300, 64


def _doc_type(filename, ct):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    ct = (ct or "").lower()
    if ext == "pdf" or "pdf" in ct: return "pdf"
    if ext in ("doc", "docx") or "word" in ct or "msword" in ct: return "word"
    if ext in ("xls", "xlsx", "csv") or "excel" in ct or "spreadsheet" in ct or ct == "text/csv": return "spreadsheet"
    if ext in ("eml", "msg") or ct == "message/rfc822": return "email"
    if ext in ("txt", "rtf") or ct in ("text/plain", "text/rtf", "application/rtf"): return "text"
    if ext in ("png", "jpg", "jpeg", "tif", "tiff", "gif", "bmp") or ct.startswith("image/"): return "image"
    return None


def _chunk(text):
    text = (text or "").strip()
    if not text: return []
    out, i, n, idx = [], 0, len(text), 0
    while i < n and idx < MAX_CHUNKS:
        end = min(i + CHUNK, n)
        out.append((i, end, text[i:end])); idx += 1
        if end >= n: break
        i = end - OVERLAP
    return out


def _embed(texts):
    vecs, model, rev = [], None, None
    for k in range(0, len(texts), BATCH):
        req = urllib.request.Request(
            EMBED_URL,
            data=json.dumps({"texts": texts[k:k+BATCH], "input_type": "document"}).encode(),
            headers={"Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req, timeout=180))
        vecs.extend(r["embeddings"]); model = r.get("model"); rev = r.get("revision")
    return vecs, "%s@%s" % (model, (rev or "")[:12])


def _veclit(v):
    return "[" + ",".join("%.6f" % x for x in v) + "]"


def _already(cur, st, sid):
    cur.execute("SELECT 1 FROM email_chunks WHERE source_type=%s AND source_id=%s LIMIT 1", (st, sid))
    return cur.fetchone() is not None


def _process_email(tenant_id, run_id, em):
    conn = _get_db_conn(); cur = conn.cursor()
    eid = em["id"]
    res = {"body_chunks": 0, "attach_chunks": 0, "attach_docs": 0, "errors": 0}
    pending = []
    try:
        if em.get("body_text") and not _already(cur, "email", eid):
            base = "Subject: %s\n\n%s" % (em.get("subject") or "", em["body_text"])
            for ci, (s, e, c) in enumerate(_chunk(base)):
                pending.append(("email", eid, ci, s, e, c))
        cur.execute("SELECT id, filename, content_type, staging_path FROM email_attachments WHERE email_id=%s", (eid,))
        for a in cur.fetchall():
            if _already(cur, "email_attachment", a["id"]):
                continue
            dt = _doc_type(a["filename"], a["content_type"])
            if not dt:
                continue
            try:
                text, _ = extract_text(a["staging_path"], dt)
            except Exception:
                text = None
            chunks = _chunk(text)
            if chunks:
                res["attach_docs"] += 1
            for ci, (s, e, c) in enumerate(chunks):
                pending.append(("email_attachment", a["id"], ci, s, e, c))
        if not pending:
            return res
        vecs, model = _embed([p[5] for p in pending])
        for (st, sid, ci, s, e, c), vec in zip(pending, vecs):
            cur.execute("""
                INSERT INTO email_chunks
                  (tenant_id, source_type, source_id, run_id, chunk_index, char_start, char_end,
                   content, embedded_content, token_count, subject, from_email, received_at,
                   conversation_id, attorney_user_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (tenant_id, st, sid, run_id, ci, s, e, c, c, len(c)//4,
                  em.get("subject"), em.get("from_email"), em.get("received_at"),
                  em.get("conversation_id"), em.get("attorney_user_id")))
            cid = cur.fetchone()["id"]
            cur.execute("INSERT INTO email_chunk_embeddings (tenant_id, chunk_id, embedding_model, embedding_768) "
                        "VALUES (%s,%s,%s,%s::vector)", (tenant_id, cid, model, _veclit(vec)))
            if st == "email": res["body_chunks"] += 1
            else: res["attach_chunks"] += 1
        conn.commit()
    except Exception as ex:
        conn.rollback(); res["errors"] += 1
        log.warning("embed email %s failed: %s", eid, ex)
    finally:
        cur.close(); conn.close()
    return res


def run(tenant_id, only_email="dennis@hjmmlegal.com", limit=25, workers=6):
    conn = _get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT mapped_user_id FROM connector_entity_map "
                "WHERE connector_type='exchange' AND lower(entity_email)=lower(%s) LIMIT 1", (only_email,))
    row = cur.fetchone(); uid = row["mapped_user_id"] if row else None
    if not uid:
        conn.close(); raise ValueError("no uid for %s" % only_email)
    cur.execute("""
        SELECT DISTINCT eq.id, eq.subject, eq.from_email, eq.received_at, eq.conversation_id,
               eq.body_text, eq.attorney_user_id
        FROM email_routing_queue eq
        JOIN email_attachments ea ON ea.email_id = eq.id
        WHERE eq.attorney_user_id=%s
        ORDER BY eq.received_at DESC
        LIMIT %s
    """, (uid, limit))
    emails = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()

    run_id = str(uuid.uuid4())
    agg = {"emails": len(emails), "body_chunks": 0, "attach_chunks": 0,
           "attach_docs": 0, "errors": 0, "run_id": run_id}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_process_email, tenant_id, run_id, em) for em in emails]
        for f in as_completed(futs):
            r = f.result()
            for k in ("body_chunks", "attach_chunks", "attach_docs", "errors"):
                agg[k] += r[k]
    return agg
