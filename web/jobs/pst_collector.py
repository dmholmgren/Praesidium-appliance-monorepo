"""
pst_collector.py -- B1: standalone firm PST/email archive collector.

A SEPARATE tenant-admin collection point, independent of the eDiscovery pipeline
(which is left untouched). Flow per uploaded PST (one pst_import_batches row):

  pffexport (forensic, pff-tools)  ->  per-message folders
    -> parse OutlookHeaders/Recipients/InternetHeaders/Message.txt
    -> normalized_hash dedup (firm_archive=global / custodian=custodian-scoped)
    -> pst_messages rows  (the firm email archive)
    -> chunk + embed into the SHARED email_chunks / email_chunk_embeddings
       vector base (source_type='pst_message') so the archive is searchable
       alongside the rest of the email corpus.

Idempotent: a batch re-run skips messages whose hash already landed; the embed
step skips messages that already have chunks.

CLI (inside praesidium-web):
  python3 /app/jobs/pst_collector.py --batch <batch_uuid> [--no-embed]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime

logger = logging.getLogger("pst_collector")

EMBED_URL_DEFAULT = os.environ.get("EMBED_URL", "http://praesidium-embed:8000")
SOURCE_TYPE = "pst_message"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


# ----------------------------- db / embed -------------------------------- #
def _db_kwargs() -> dict:
    from urllib.parse import urlparse
    raw = os.environ.get("DATABASE_URL", "")
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://"):
        if raw.startswith(prefix):
            raw = "postgresql://" + raw[len(prefix):]
            break
    p = urlparse(raw)
    return {"dbname": p.path.lstrip("/") or "praesidium", "user": p.username or "praesidium",
            "password": p.password or "", "host": p.hostname or "172.28.0.1",
            "port": str(p.port or 5432)}


def _connect():
    import psycopg2
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _embed(base, texts):
    payload = json.dumps({"texts": texts, "input_type": "document"}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/embed", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode())
    return data["embeddings"], data.get("model", "modernbert-768"), data.get("revision", "")


def _vec_literal(v):
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


# ----------------------------- pst parsing ------------------------------- #
def extract_pst(pst_path, out_dir):
    """Forensic extract with pffexport (pff-tools). Returns the .export root."""
    base = os.path.join(out_dir, "pst")
    cmd = ["pffexport", "-q", "-t", base, pst_path]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if res.returncode != 0:
        raise RuntimeError("pffexport rc=%d: %s" % (res.returncode, (res.stderr or "")[:300]))
    return base + ".export"


def _parse_outlook_headers(path):
    """OutlookHeaders.txt is 'Label:\\t\\t\\tValue' lines -> dict keyed by label."""
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if ":" not in line:
                    continue
                label, _, val = line.partition(":")
                out[label.strip().lower()] = val.strip()
    except OSError:
        pass
    return out


def _parse_sent(val):
    """'Sep 15, 2021 22:05:49.000000000 UTC' -> datetime (naive UTC) or None."""
    if not val:
        return None
    m = re.match(r"([A-Za-z]{3} \d{1,2}, \d{4} \d{2}:\d{2}:\d{2})", val.strip())
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%b %d, %Y %H:%M:%S")
    except ValueError:
        return None


def _read(path, limit=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit) if limit else f.read()
    except OSError:
        return ""


def parse_message_dir(d):
    """Parse one pffexport Message folder into a message dict (or None if empty)."""
    oh = _parse_outlook_headers(os.path.join(d, "OutlookHeaders.txt"))
    body = _read(os.path.join(d, "Message.txt")).strip()
    recips = _read(os.path.join(d, "Recipients.txt"))
    ih = _read(os.path.join(d, "InternetHeaders.txt"), 20000)
    if not body and not oh:
        return None
    to_emails = sorted(set(_EMAIL_RE.findall(recips)))
    msgid = ""
    mid = re.search(r"^Message-ID:\s*(.+)$", ih, re.IGNORECASE | re.MULTILINE)
    if mid:
        msgid = mid.group(1).strip()[:500]
    flags = oh.get("flags", "")
    return {
        "subject": oh.get("subject") or "",
        "conversation_topic": oh.get("conversation topic") or "",
        "from_display": oh.get("sender name") or "",
        "from_email": (oh.get("sender email address") or "").lower(),
        "to_emails": ", ".join(to_emails),
        "sent_at": _parse_sent(oh.get("client submit time") or oh.get("delivery time")),
        "body_text": body,
        "has_attachments": "has attachments" in flags.lower(),
        "internet_message_id": msgid,
        "message_path": d,
    }


def _norm_hash(subject, body):
    norm = re.sub(r"\s+", " ", ((subject or "") + " " + (body or "")).lower()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


# ----------------------------- coordinator ------------------------------- #
def _set_status(cur, conn, batch_id, **fields):
    sets = ", ".join("%s=%%s" % k for k in fields) + ", updated_at=now()"
    cur.execute("UPDATE pst_import_batches SET " + sets + " WHERE id=CAST(%s AS uuid)",
                list(fields.values()) + [batch_id])
    conn.commit()


def _dedup_exists(cur, tenant, routing_tag, custodian_label, h):
    if routing_tag == "custodian" and custodian_label:
        cur.execute(
            "SELECT 1 FROM pst_messages m JOIN pst_import_batches b ON b.id=m.batch_id "
            "WHERE TRIM(m.tenant_id)=%s AND m.normalized_hash=%s "
            "AND b.custodian_label=%s LIMIT 1", (tenant, h, custodian_label))
    else:  # firm_archive -> global dedup within tenant
        cur.execute("SELECT 1 FROM pst_messages WHERE TRIM(tenant_id)=%s "
                    "AND normalized_hash=%s LIMIT 1", (tenant, h))
    return cur.fetchone() is not None


def run_batch(batch_id, do_embed=True, embed_url=EMBED_URL_DEFAULT):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT TRIM(tenant_id), storage_path, routing_tag, custodian_label "
                "FROM pst_import_batches WHERE id=CAST(%s AS uuid)", (batch_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise RuntimeError("batch not found: %s" % batch_id)
    tenant, storage_path, routing_tag, custodian_label = row
    stats = {"batch": batch_id, "total": 0, "imported": 0, "dupes": 0, "embedded": 0}
    try:
        if not storage_path or not os.path.exists(storage_path):
            raise RuntimeError("PST not found at storage_path: %s" % storage_path)
        _set_status(cur, conn, batch_id, status="extracting")
        with tempfile.TemporaryDirectory(prefix="pst_") as tmp:
            export_root = extract_pst(storage_path, tmp)
            _set_status(cur, conn, batch_id, status="parsing")
            msg_dirs = []
            for root, dirs, files in os.walk(export_root):
                if "OutlookHeaders.txt" in files or "Message.txt" in files:
                    msg_dirs.append(root)
            new_ids = []
            for d in msg_dirs:
                msg = parse_message_dir(d)
                if not msg:
                    continue
                stats["total"] += 1
                h = _norm_hash(msg["subject"], msg["body_text"])
                if _dedup_exists(cur, tenant, routing_tag, custodian_label, h):
                    stats["dupes"] += 1
                    continue
                mid = str(uuid.uuid4())
                rel = os.path.relpath(msg["message_path"], export_root)
                cur.execute(
                    "INSERT INTO pst_messages (id, tenant_id, batch_id, normalized_hash, "
                    " message_path, from_email, from_display, to_emails, subject, sent_at, "
                    " body_text, has_attachments, internet_message_id, conversation_topic) "
                    "VALUES (CAST(%s AS uuid), %s, CAST(%s AS uuid), %s, %s, %s, %s, %s, %s, %s, "
                    " %s, %s, %s, %s)",
                    (mid, tenant, batch_id, h, rel[:2000], msg["from_email"][:500],
                     msg["from_display"][:500], msg["to_emails"][:4000], msg["subject"][:2000],
                     msg["sent_at"], msg["body_text"], msg["has_attachments"],
                     msg["internet_message_id"], msg["conversation_topic"][:2000]))
                new_ids.append((mid, msg))
                stats["imported"] += 1
            conn.commit()
            cur.execute("UPDATE pst_import_batches SET total_messages=%s, imported_messages=%s, "
                        "duplicate_messages=%s, updated_at=now() WHERE id=CAST(%s AS uuid)",
                        (stats["total"], stats["imported"], stats["dupes"], batch_id))
            conn.commit()

            if do_embed and new_ids:
                _set_status(cur, conn, batch_id, status="embedding")
                stats["embedded"] = _embed_messages(cur, conn, tenant, new_ids, embed_url)

        _set_status(cur, conn, batch_id, status="done", completed_at=datetime.utcnow())
        logger.info("pst batch done: %s", stats)
        return stats
    except Exception as e:
        conn.rollback()
        try:
            _set_status(cur, conn, batch_id, status="error", error=str(e)[:1000])
        except Exception:
            pass
        logger.exception("pst batch failed: %s", e)
        raise
    finally:
        conn.close()


def _embed_messages(cur, conn, tenant, new_ids, embed_url, window=1800, batch=128):
    """Chunk each pst_message into email_chunks (source_type='pst_message') and
    embed into email_chunk_embeddings.embedding_768 (shared vector base)."""
    run_id = str(uuid.uuid4())
    pending = []  # (chunk_id, text)
    for mid, msg in new_ids:
        text = ((msg["subject"] + "\n\n") if msg["subject"] else "") + (msg["body_text"] or "")
        text = text.strip()
        if not text:
            continue
        n = len(text)
        spans = [(0, n)] if n <= window else \
            [(i, min(i + window, n)) for i in range(0, n, window - 250)]
        for ci, (cs, ce) in enumerate(spans):
            chunk_id = str(uuid.uuid4())
            ctext = text[cs:ce]
            cur.execute(
                "INSERT INTO email_chunks (id, tenant_id, source_type, source_id, run_id, "
                " chunk_index, char_start, char_end, content, embedded_content, token_count, "
                " chunk_metadata, chunked_at, from_email, subject, received_at, "
                " primitive_type, primitive_id) "
                "VALUES (CAST(%s AS uuid), %s, %s, CAST(%s AS uuid), CAST(%s AS uuid), %s, "
                " %s, %s, %s, %s, %s, CAST(%s AS jsonb), now(), %s, %s, %s, %s, CAST(%s AS uuid))",
                (chunk_id, tenant, SOURCE_TYPE, mid, run_id, ci, cs, ce, ctext, ctext,
                 max(1, len(ctext) // 4), json.dumps({"pst_message_id": mid}),
                 msg["from_email"][:500], msg["subject"][:2000], msg["sent_at"],
                 SOURCE_TYPE, mid))
            pending.append((chunk_id, ctext))
    conn.commit()
    embedded = 0
    for i in range(0, len(pending), batch):
        grp = pending[i:i + batch]
        ids = [c[0] for c in grp]
        texts = [(c[1] or "")[:8000] for c in grp]
        t0 = time.time()
        vecs, model, rev = _embed(embed_url, texts)
        dur = int((time.time() - t0) * 1000)
        model_id = (model + ("@" + rev if rev else ""))[:64]
        cur.execute("DELETE FROM email_chunk_embeddings WHERE chunk_id = ANY(%s::uuid[]) "
                    "AND embedding_model=%s", (ids, model_id))
        for cid, v in zip(ids, vecs):
            cur.execute(
                "INSERT INTO email_chunk_embeddings (id, tenant_id, chunk_id, embedding_model, "
                " embedding_768, embedded_at, embedding_duration_ms) "
                "VALUES (gen_random_uuid(), %s, CAST(%s AS uuid), %s, CAST(%s AS vector), now(), %s)",
                (tenant, cid, model_id, _vec_literal(v), dur))
        conn.commit()
        embedded += len(grp)
    return embedded


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--embed-url", default=EMBED_URL_DEFAULT)
    args = ap.parse_args()
    out = run_batch(args.batch, do_embed=not args.no_embed, embed_url=args.embed_url)
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
