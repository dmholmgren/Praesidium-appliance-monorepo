"""
embed_ediscovery_chunks.py -- embed ediscovery_chunks via the local
praesidium-embed (ModernBERT-768) service into
ediscovery_chunk_embeddings.embedding_768.

Mirrors embed_document_chunks.py (DMS) for the ediscovery corpus. Idempotent:
by default only embeds chunks lacking an embedding_768 row. Collection-scoped.

  POST {EMBED_URL}/embed  {"texts":[...], "input_type":"document"}
    -> {"model","revision","dim":768,"count":N,"embeddings":[[...768],...]}

CLI (inside praesidium-web):
  python -m modules.ediscovery.jobs.embed_ediscovery_chunks \
      --tenant <uuid> --collection <uuid> [--doc <uuid>...] [--limit N]
      [--batch-size 64] [--force] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger(__name__)

EMBED_URL_DEFAULT = os.environ.get("EMBED_URL", "http://praesidium-embed:8000")
BATCH_DEFAULT = 64


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


def embed_collection(tenant, collection=None, docs=None, limit=0,
                     batch_size=BATCH_DEFAULT, embed_url=EMBED_URL_DEFAULT,
                     force=False, dry_run=False) -> dict:
    ten = tenant.strip()
    s = {"chunks": 0, "embedded": 0, "batches": 0, "dry_run": dry_run}
    conn = _connect()
    try:
        cur = conn.cursor()
        where = ["TRIM(ch.tenant_id)=%(t)s"]
        params = {"t": ten}
        if docs:
            where.append("ch.document_id = ANY(%(docs)s::uuid[])")
            params["docs"] = docs
        if collection:
            where.append("d.collection_id = %(c)s::uuid")
            params["c"] = collection
        if not force:
            where.append("NOT EXISTS (SELECT 1 FROM ediscovery_chunk_embeddings e "
                         "WHERE e.chunk_id=ch.id AND e.embedding_768 IS NOT NULL)")
        sql = ("SELECT ch.id::text, COALESCE(NULLIF(ch.embedded_content,''), ch.content) "
               "FROM ediscovery_chunks ch JOIN ediscovery_documents d ON d.id=ch.document_id "
               "WHERE " + " AND ".join(where) + " ORDER BY ch.document_id, ch.chunk_index")
        if limit:
            sql += " LIMIT %d" % int(limit)
        cur.execute(sql, params)
        rows = cur.fetchall()
        s["chunks"] = len(rows)
        logger.info("embed_ediscovery: %d chunk(s) to embed (collection=%s force=%s dry=%s)",
                    len(rows), collection, force, dry_run)
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
            # delete-then-insert keeps it idempotent regardless of the unique constraint shape
            cur.execute("DELETE FROM ediscovery_chunk_embeddings "
                        "WHERE chunk_id = ANY(%s::uuid[]) AND embedding_model=%s",
                        (ids, model_id))
            for cid, v in zip(ids, vecs):
                cur.execute(
                    "INSERT INTO ediscovery_chunk_embeddings "
                    "(tenant_id, chunk_id, embedding_model, embedding_768, embedded_at, embedding_duration_ms) "
                    "VALUES (%s, CAST(%s AS uuid), %s, CAST(%s AS vector), now(), %s)",
                    (ten, cid, model_id, _vec_literal(v), dur))
            conn.commit()
            s["embedded"] += len(batch)
            s["batches"] += 1
            logger.info("  batch %d: +%d (model=%s)", s["batches"], len(batch), model_id)
        return s
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", "986c0fee-1390-43bb-ad28-8cd1db6de53f"))
    ap.add_argument("--collection", default=None)
    ap.add_argument("--doc", action="append", help="ediscovery_document_id (repeatable)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch-size", type=int, default=BATCH_DEFAULT)
    ap.add_argument("--embed-url", default=EMBED_URL_DEFAULT)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = embed_collection(args.tenant, collection=args.collection, docs=args.doc,
                           limit=args.limit, batch_size=args.batch_size,
                           embed_url=args.embed_url, force=args.force, dry_run=args.dry_run)
    logger.info("DONE %s", json.dumps(out))


if __name__ == "__main__":
    main()
