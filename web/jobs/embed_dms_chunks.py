#!/usr/bin/env python3
"""
jobs/embed_dms_chunks.py -- embed dms_chunks via the local praesidium-embed
(ModernBERT-768) service into dms_chunk_embeddings.embedding_768.

Mirrors modules/ediscovery/jobs/embed_ediscovery_chunks.py for the DMS corpus.
Idempotent: by default only embeds chunks lacking an embedding_768 row. Ensures
the 768 HNSW index exists. Tenant- or doc-scoped (doc = dms_chunks.source_id).

  POST {EMBED_URL}/embed  {"texts":[...], "input_type":"document"}
    -> {"model","revision","dim":768,"count":N,"embeddings":[[...768],...]}

CLI (inside praesidium-web):
  python3 /app/jobs/embed_dms_chunks.py --tenant <uuid> --all
  python3 /app/jobs/embed_dms_chunks.py --doc <dms_document_id> [--doc ...]
      [--limit N] [--batch-size 64] [--force] [--skip-index] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request

sys.path.insert(0, "/app")

logger = logging.getLogger("embed_dms_chunks")

EMBED_URL_DEFAULT = os.environ.get("EMBED_URL", "http://praesidium-embed:8000")
BATCH_DEFAULT = 64
TENANT_DEFAULT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
HNSW_SQL = ("CREATE INDEX IF NOT EXISTS idx_dms_emb_768_hnsw "
            "ON dms_chunk_embeddings USING hnsw (embedding_768 vector_cosine_ops) "
            "WHERE embedding_768 IS NOT NULL")


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


def embed_dms(tenant, docs=None, limit=0, batch_size=BATCH_DEFAULT,
              embed_url=EMBED_URL_DEFAULT, force=False, skip_index=False,
              dry_run=False) -> dict:
    ten = tenant.strip()
    s = {"chunks": 0, "embedded": 0, "batches": 0, "dry_run": dry_run}
    conn = _connect()
    try:
        cur = conn.cursor()
        where = ["TRIM(ch.tenant_id)=%(t)s", "ch.source_type='dms_document'"]
        params = {"t": ten}
        if docs:
            where.append("ch.source_id = ANY(%(docs)s::uuid[])")
            params["docs"] = docs
        if not force:
            where.append("NOT EXISTS (SELECT 1 FROM dms_chunk_embeddings e "
                         "WHERE e.chunk_id=ch.id AND e.embedding_768 IS NOT NULL)")
        sql = ("SELECT ch.id::text, COALESCE(NULLIF(ch.embedded_content,''), ch.content) "
               "FROM dms_chunks ch WHERE " + " AND ".join(where) +
               " ORDER BY ch.source_id, ch.chunk_index")
        if limit:
            sql += " LIMIT %d" % int(limit)
        cur.execute(sql, params)
        rows = cur.fetchall()
        s["chunks"] = len(rows)
        logger.info("embed_dms: %d chunk(s) to embed (docs=%s force=%s dry=%s)",
                    len(rows), bool(docs), force, dry_run)
        if dry_run or not rows:
            return s

        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            ids = [r[0] for r in batch]
            texts = [(r[1] or "")[:8000] for r in batch]
            t0 = time.time()
            vecs, model, rev = _embed(embed_url, texts)
            dur = int((time.time() - t0) * 1000)
            if len(vecs) != len(batch):
                raise RuntimeError("embed count mismatch: %d != %d" % (len(vecs), len(batch)))
            model_id = (model + ("@" + rev if rev else ""))[:64]
            cur.execute("DELETE FROM dms_chunk_embeddings "
                        "WHERE chunk_id = ANY(%s::uuid[]) AND embedding_model=%s",
                        (ids, model_id))
            for cid, v in zip(ids, vecs):
                cur.execute(
                    "INSERT INTO dms_chunk_embeddings "
                    "(tenant_id, chunk_id, embedding_model, embedding_768, embedded_at, embedding_duration_ms) "
                    "VALUES (%s, CAST(%s AS uuid), %s, CAST(%s AS vector), now(), %s)",
                    (ten, cid, model_id, _vec_literal(v), dur))
            conn.commit()
            s["embedded"] += len(batch)
            s["batches"] += 1
            logger.info("  batch %d: +%d (model=%s)", s["batches"], len(batch), model_id)

        if not skip_index:
            try:
                cur.execute(HNSW_SQL)
                conn.commit()
            except Exception as e:
                logger.warning("HNSW index ensure failed (non-fatal): %s", e)
        return s
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Embed dms_chunks via ModernBERT-768")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", TENANT_DEFAULT))
    ap.add_argument("--doc", action="append", help="dms_document_id (repeatable)")
    ap.add_argument("--all", action="store_true", help="all unembedded dms chunks for tenant")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch-size", type=int, default=BATCH_DEFAULT)
    ap.add_argument("--embed-url", default=EMBED_URL_DEFAULT)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--skip-index", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.doc and not args.all:
        ap.error("provide --doc <dms_document_id> (repeatable) or --all")
    out = embed_dms(args.tenant, docs=args.doc, limit=args.limit,
                    batch_size=args.batch_size, embed_url=args.embed_url,
                    force=args.force, skip_index=args.skip_index, dry_run=args.dry_run)
    logger.info("DONE %s", json.dumps(out))


if __name__ == "__main__":
    main()
