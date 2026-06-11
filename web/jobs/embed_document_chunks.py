#!/usr/bin/env python3
"""jobs/embed_document_chunks.py -- embed document_chunks.embedded_content via the
local praesidium-embed (ModernBERT-768) service into document_chunk_embeddings.

Idempotent: by default only embeds chunks that have NO document_chunk_embeddings
row yet (LEFT JOIN guard), so it resumes after interruption and is safe to re-run
during bulk ingest. --force re-embeds the selected scope (delete-then-insert).

Embed wire (confirmed against the live service):
    POST {base}/embed  {"texts":[...], "input_type":"document"}
      -> {"model","revision","dim":768,"count",N,"embeddings":[[...768],...]}
Vectors come back parallel to `texts`. We assert dim==768 and that the service's
reported model+revision stay constant across the whole run.

Target table document_chunk_embeddings (verified): id uuid DEFAULT gen_random_uuid(),
chunk_id uuid NOT NULL (FK->document_chunks ON DELETE CASCADE), tenant_id char(36)
NOT NULL, embedding_model varchar NOT NULL, embedded_at timestamptz DEFAULT now(),
embedding vector(768). chunk_id is indexed but NOT unique -> idempotency via the
LEFT JOIN guard, not ON CONFLICT.

embedding_model recorded = the service's own reported model string (authoritative
identity of the vectors). The run aborts if the service swaps model/revision midway.

Embed-service URL resolution (this runs INSIDE praesidium-web): EMBED_URL /
--embed-url override first, else probe candidates and use the first whose /health
answers. Override with --embed-url if your service lives elsewhere.

Usage (inside the praesidium-web container):
    docker exec -i -w /app praesidium-web python - --doc <dms_document_id> --dry-run < /tmp/embed_document_chunks.py
    docker exec -i -w /app praesidium-web python - --doc <dms_document_id> < /tmp/embed_document_chunks.py
    docker exec -i -w /app praesidium-web python - --all < /tmp/embed_document_chunks.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

TENANT_DEFAULT = os.environ.get("TENANT_ID", "986c0fee-1390-43bb-ad28-8cd1db6de53f")
BATCH_DEFAULT = 64
HEALTH_TIMEOUT = 3.0
EMBED_TIMEOUT = 180.0
EXPECT_DIM = 768

EMBED_CANDIDATES = [
    "http://praesidium-embed:8000",
    "http://host.docker.internal:8100",
    "http://127.0.0.1:8100",
]

HNSW_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_dce_embedding_hnsw "
    "ON document_chunk_embeddings USING hnsw (embedding vector_cosine_ops)"
)


def get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return "host=172.28.0.1 port=5432 dbname=praesidium user=praesidium"
    for p in ("postgresql+asyncpg://", "postgresql+psycopg2://"):
        if url.startswith(p):
            return "postgresql://" + url[len(p):]
    return url


def _probe(base: str) -> bool:
    """GET {base}/health -- True on any 2xx, tolerant of non-JSON bodies."""
    try:
        req = urllib.request.Request(base.rstrip("/") + "/health", method="GET")
        with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def resolve_embed_base(override):
    cands = [override] if override else list(EMBED_CANDIDATES)
    for base in cands:
        if not base:
            continue
        base = base.rstrip("/")
        if _probe(base):
            return base
        print(f"  embed probe: {base} -> no /health")
    return None


def embed_batch(base, texts):
    payload = json.dumps({"texts": texts, "input_type": "document"}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/embed", data=payload,
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as r:
        return json.loads(r.read().decode())


def vec_literal(v):
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def main():
    ap = argparse.ArgumentParser(description="Embed document_chunks via praesidium-embed (ModernBERT-768)")
    ap.add_argument("--doc", action="append", help="dms_document_id to embed (repeatable)")
    ap.add_argument("--all", action="store_true", help="all unembedded chunks for tenant")
    ap.add_argument("--tenant", default=TENANT_DEFAULT)
    ap.add_argument("--limit", type=int, default=0, help="cap chunks this run (0=all)")
    ap.add_argument("--batch-size", type=int, default=BATCH_DEFAULT)
    ap.add_argument("--embed-url", default=os.environ.get("EMBED_URL"),
                    help="override embed base URL (else EMBED_URL env, else probe)")
    ap.add_argument("--force", action="store_true",
                    help="re-embed selected scope (delete existing embeddings first)")
    ap.add_argument("--skip-index", action="store_true", help="do not build the HNSW index")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tenant = args.tenant.strip()
    if not args.doc and not args.all:
        ap.error("provide --doc <dms_document_id> (repeatable) or --all")

    where = ["TRIM(c.tenant_id) = %(t)s"]
    params = {"t": tenant}
    if args.doc:
        where.append("c.dms_document_id = ANY(%(docs)s::uuid[])")
        params["docs"] = list(args.doc)
    scope = " AND ".join(where)

    conn = psycopg2.connect(get_db_url())
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            print(f"=== embed_document_chunks  tenant={tenant} ===")
            if args.doc:
                print(f"scope: --doc {list(args.doc)}")
            else:
                print("scope: --all (every unembedded chunk for tenant)")

            if args.force and not args.dry_run:
                cur.execute(f"""
                    DELETE FROM document_chunk_embeddings e
                    USING document_chunks c
                    WHERE e.chunk_id = c.id AND {scope}
                """, params)
                print(f"  --force: deleted {cur.rowcount} existing embeddings in scope")

            limit_clause = f"LIMIT {int(args.limit)}" if args.limit else ""
            cur.execute(f"""
                SELECT c.id::text AS id, c.tenant_id AS tid, c.embedded_content AS txt
                FROM document_chunks c
                LEFT JOIN document_chunk_embeddings e ON e.chunk_id = c.id
                WHERE {scope} AND e.id IS NULL
                ORDER BY c.dms_document_id, c.chunk_index
                {limit_clause}
            """, params)
            rows = cur.fetchall()

            todo = [r for r in rows if (r["txt"] or "").strip()]
            empty = len(rows) - len(todo)
            print(f"chunks needing embedding: {len(rows)}  "
                  f"(embeddable={len(todo)}, empty_embedded_content_skipped={empty})")

            # Probe the service in every mode so a dry-run also proves connectivity.
            base = resolve_embed_base(args.embed_url)
            if base:
                print(f"embed service: {base}")
            else:
                print("WARNING: no reachable embed service (tried override/env/candidates: "
                      f"{EMBED_CANDIDATES}). Pass --embed-url or set EMBED_URL before a real run.")

            if args.dry_run:
                nb = (len(todo) + args.batch_size - 1) // args.batch_size if todo else 0
                print(f"DRY RUN -- would POST {nb} batch(es) of <= {args.batch_size} texts; no writes.")
                conn.rollback()
                return

            if not todo:
                print("nothing to embed.")
                conn.rollback()
                return
            if not base:
                print("ERROR: embed service unreachable; aborting before writes.")
                sys.exit(1)

            ins = ("INSERT INTO document_chunk_embeddings "
                   "(chunk_id, tenant_id, embedding_model, embedding) VALUES %s")
            tmpl = "(%s::uuid, %s, %s, %s::vector)"

            model_seen = rev_seen = None
            total = 0
            start = time.time()
            for bs in range(0, len(todo), args.batch_size):
                batch = todo[bs:bs + args.batch_size]
                texts = [r["txt"] for r in batch]
                try:
                    resp = embed_batch(base, texts)
                except Exception as e:
                    print(f"  batch {bs}: embed call FAILED: {type(e).__name__}: {str(e)[:200]}")
                    conn.rollback()
                    sys.exit(1)

                dim = resp.get("dim")
                model = resp.get("model")
                rev = resp.get("revision")
                embs = resp.get("embeddings")
                if dim != EXPECT_DIM:
                    print(f"  ABORT: service dim={dim}, expected {EXPECT_DIM}")
                    conn.rollback(); sys.exit(1)
                if not isinstance(embs, list) or len(embs) != len(batch):
                    got = len(embs) if isinstance(embs, list) else "?"
                    print(f"  ABORT: got {got} vectors for {len(batch)} texts")
                    conn.rollback(); sys.exit(1)
                if model_seen is None:
                    model_seen, rev_seen = model, rev
                    print(f"  model={model}  revision={rev}  dim={dim}")
                elif model != model_seen or rev != rev_seen:
                    print(f"  ABORT: service model/revision changed mid-run "
                          f"({model}@{rev} vs {model_seen}@{rev_seen})")
                    conn.rollback(); sys.exit(1)

                vals = []
                for r, v in zip(batch, embs):
                    if not isinstance(v, list) or len(v) != EXPECT_DIM:
                        print(f"  ABORT: vector dim {len(v) if isinstance(v, list) else '?'} "
                              f"!= {EXPECT_DIM} for chunk {r['id']}")
                        conn.rollback(); sys.exit(1)
                    vals.append((r["id"], (r["tid"] or tenant).strip(), model_seen, vec_literal(v)))

                psycopg2.extras.execute_values(cur, ins, vals, template=tmpl)
                conn.commit()
                total += len(vals)
                print(f"  batch {bs}-{bs + len(batch)}: embedded {len(vals)}  (running total {total})")

            print(f"=== embedded {total} chunks in {time.time() - start:.1f}s  "
                  f"model={model_seen} revision={rev_seen} ===")

            if not args.skip_index:
                cur.execute("SELECT count(*) AS n FROM document_chunk_embeddings")
                n = cur.fetchone()["n"]
                if n > 0:
                    print(f"building HNSW index over document_chunk_embeddings (global, {n} rows)...")
                    t0 = time.time()
                    cur.execute(HNSW_SQL)
                    conn.commit()
                    print(f"HNSW index ix_dce_embedding_hnsw ready ({time.time() - t0:.1f}s).")

            # Concise verification of what now exists in scope.
            cur.execute(f"""
                SELECT count(*) AS embedded
                FROM document_chunk_embeddings e
                JOIN document_chunks c ON c.id = e.chunk_id
                WHERE {scope}
            """, params)
            print(f"scope now has {cur.fetchone()['embedded']} embedded chunks.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
