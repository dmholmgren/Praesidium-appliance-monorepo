"""
email_chunker.py -- B2: turn the email chunker ON.

Reads the already-segmented email corpus (email_segments) and produces
email_chunks + email_chunk_embeddings in the SHARED ModernBERT-768 vector
space (same /embed contract + model as the DMS and eDiscovery corpora), so
email becomes a first-class witness for record/matter search and for the
hearing-seeding (A4) email signal collector.

WHY A DEDICATED CHUNKER (not the eDiscovery one):
  * Email's natural unit is the message turn: email_segments already split a
    thread into per-author turns (segmenter + normalized_hash dedup). Each
    segment IS the semantic unit; we only window the long ones.
  * Header context (from / date) lives on the segment, not in extracted_text,
    so we carry it onto every chunk for faceted search. subject/to/conversation
    are not reliably stored yet (the .eml headers were not persisted to
    documents.title); they stay NULL and are a header-enrichment follow-up.

GUARANTEES:
  * Non-destructive: only writes email_chunks + email_chunk_embeddings.
  * Idempotent: skips segments that already have a chunk; embed step is
    delete-then-insert per (chunk_id, model).

CLI (inside praesidium-web):
  python3 /app/jobs/email_chunker.py --tenant <uuid> [--limit N]
      [--batch 128] [--window 1800] [--overlap 250] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.request
import uuid

logger = logging.getLogger(__name__)

TENANT_DEFAULT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
EMBED_URL_DEFAULT = os.environ.get("EMBED_URL", "http://praesidium-embed:8000")
SOURCE_TYPE = "email_segment"


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


def _windows(text, window, overlap):
    """Yield (char_start, char_end, chunk_text). One window for short messages;
    sliding window with overlap for long ones."""
    text = text or ""
    n = len(text)
    if n <= window:
        yield 0, n, text
        return
    step = max(1, window - overlap)
    i = 0
    while i < n:
        end = min(i + window, n)
        yield i, end, text[i:end]
        if end >= n:
            break
        i += step


def _segments_needing_chunks(cur, tenant, limit):
    cur.execute(
        "SELECT es.id::text, es.document_id::text, es.segment_index, es.author, "
        "       es.sent_date, es.is_top, es.content "
        "FROM email_segments es "
        "WHERE TRIM(es.tenant_id)=%(t)s AND COALESCE(es.content,'')<>'' "
        "  AND NOT EXISTS (SELECT 1 FROM email_chunks ec "
        "                  WHERE ec.source_type=%(st)s AND ec.source_id=es.id) "
        "ORDER BY es.document_id, es.segment_index" + (" LIMIT %d" % int(limit) if limit else ""),
        {"t": tenant, "st": SOURCE_TYPE})
    return cur.fetchall()


def run(tenant, limit=0, batch=128, window=1800, overlap=250,
        embed_url=EMBED_URL_DEFAULT, dry_run=False):
    ten = (tenant or TENANT_DEFAULT).strip()
    run_id = str(uuid.uuid4())
    conn = _connect()
    stats = {"segments": 0, "chunks": 0, "embedded": 0, "batches": 0,
             "run_id": run_id, "dry_run": dry_run}
    try:
        cur = conn.cursor()
        segs = _segments_needing_chunks(cur, ten, limit)
        stats["segments"] = len(segs)
        logger.info("email_chunker: %d segment(s) need chunks (tenant=%s)", len(segs), ten[:8])
        if dry_run or not segs:
            return stats

        # 1) build email_chunks rows, buffering (chunk_id, text) for embedding
        pending = []  # (chunk_id, embed_text)
        for seg_id, doc_id, seg_index, author, sent_date, is_top, content in segs:
            meta = json.dumps({"document_id": doc_id, "segment_index": seg_index,
                               "is_top": bool(is_top)})
            for ci, (cs, ce, ctext) in enumerate(_windows(content, window, overlap)):
                chunk_id = str(uuid.uuid4())
                token_count = max(1, len(ctext) // 4)
                cur.execute(
                    "INSERT INTO email_chunks "
                    "(id, tenant_id, source_type, source_id, run_id, chunk_index, "
                    " char_start, char_end, content, embedded_content, token_count, "
                    " chunk_metadata, chunked_at, from_email, received_at, "
                    " primitive_type, primitive_id) "
                    "VALUES (CAST(%s AS uuid), %s, %s, CAST(%s AS uuid), CAST(%s AS uuid), %s, "
                    " %s, %s, %s, %s, %s, CAST(%s AS jsonb), now(), %s, %s, %s, CAST(%s AS uuid))",
                    (chunk_id, ten, SOURCE_TYPE, seg_id, run_id, ci,
                     cs, ce, ctext, ctext, token_count,
                     meta, author, sent_date, SOURCE_TYPE, seg_id))
                pending.append((chunk_id, ctext))
            stats["chunks"] += 1  # counts segments that produced >=1 chunk below; fix after
        # accurate chunk count
        stats["chunks"] = len(pending)
        conn.commit()
        logger.info("email_chunker: inserted %d chunk(s); embedding...", len(pending))

        # 2) embed in batches into email_chunk_embeddings.embedding_768
        for i in range(0, len(pending), batch):
            grp = pending[i:i + batch]
            ids = [c[0] for c in grp]
            texts = [(c[1] or "")[:8000] for c in grp]
            t0 = time.time()
            vecs, model, rev = _embed(embed_url, texts)
            dur = int((time.time() - t0) * 1000)
            if len(vecs) != len(grp):
                raise RuntimeError("embed count mismatch: %d != %d" % (len(vecs), len(grp)))
            model_id = (model + ("@" + rev if rev else ""))[:64]
            cur.execute("DELETE FROM email_chunk_embeddings "
                        "WHERE chunk_id = ANY(%s::uuid[]) AND embedding_model=%s",
                        (ids, model_id))
            for cid, v in zip(ids, vecs):
                cur.execute(
                    "INSERT INTO email_chunk_embeddings "
                    "(id, tenant_id, chunk_id, embedding_model, embedding_768, "
                    " embedded_at, embedding_duration_ms) "
                    "VALUES (gen_random_uuid(), %s, CAST(%s AS uuid), %s, CAST(%s AS vector), now(), %s)",
                    (ten, cid, model_id, _vec_literal(v), dur))
            conn.commit()
            stats["embedded"] += len(grp)
            stats["batches"] += 1
            logger.info("  embed batch %d: +%d (model=%s)", stats["batches"], len(grp), model_id)
        return stats
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", TENANT_DEFAULT))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--window", type=int, default=1800)
    ap.add_argument("--overlap", type=int, default=250)
    ap.add_argument("--embed-url", default=EMBED_URL_DEFAULT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = run(args.tenant, limit=args.limit, batch=args.batch, window=args.window,
              overlap=args.overlap, embed_url=args.embed_url, dry_run=args.dry_run)
    logger.info("DONE %s", json.dumps(out))


if __name__ == "__main__":
    main()
