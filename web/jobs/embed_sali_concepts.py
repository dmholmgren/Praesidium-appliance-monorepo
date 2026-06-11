#!/usr/bin/env python3
"""
jobs/embed_sali_concepts.py
Embedding pass for the SALI/FOLIO concept table. Reads sali_concepts rows whose
embedding is missing or stale, embeds (pref_label + definition) via Voyage AI
voyage-law-2, writes the 1024-dim vector back IN PLACE, then builds the deferred
HNSW index from migration 0043 (DEFERRED_HNSW).

Mirrors jobs/embed_billing_chunks.py (same Voyage client, model, batch sizing,
pgvector literal, DB helper). Differences:
  - sali_concepts is a cross-tenant reference table -> NO --tenant filter / TRIM
    on the embed read (but the VAULT lookup IS tenant-scoped, see below).
  - embedding column is ON the table -> in-place UPDATE, no side table.
  - builds the HNSW index after vectors exist (0041/0043 deferral convention).
  - KEY RESOLUTION pulls from credentials_vault by default (BYOK doctrine),
    decrypting with the same Fernet-from-SECRET_KEY pattern as the Anthropic
    adapter -- so the key never touches the command line. Order:
        --api-key  ->  VOYAGE_API_KEY env  ->  credentials_vault(voyage/api_key)

Staleness: embeds where embedding IS NULL OR embedded_at IS NULL OR
updated_at > embedded_at -- so a FOLIO re-download (which bumps updated_at without
touching embeddings) re-embeds only the rows whose content actually changed.

Embedded text = "<pref_label>. <definition>" (input_type='document' -- corpus side).

Requires (inside the container): voyageai, cryptography (both present), SECRET_KEY
in env (it is, from .env) for the vault path, and TENANT_ID env or --tenant.

Usage (vault path -- no key on CLI):
    sudo docker exec praesidium-web python3 /app/jobs/embed_sali_concepts.py --dry-run
    sudo docker exec praesidium-web python3 /app/jobs/embed_sali_concepts.py
"""
from __future__ import annotations

import argparse
import base64
import logging
import os
import sys
import time

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

log = logging.getLogger("embed_sali")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

MODEL = "voyage-law-2"
EMBEDDING_DIM = 1024
BATCH_SIZE = 64
COST_PER_1M = 0.12

DEFERRED_HNSW = (
    "CREATE INDEX IF NOT EXISTS ix_sali_concepts_embedding "
    "ON sali_concepts USING hnsw (embedding vector_cosine_ops)"
)


def get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return "host=localhost port=5432 dbname=praesidium user=praesidium"
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _derive_fernet_key() -> bytes:
    """Match modules/intelligence/anthropic_adapter.py exactly so vault values
    written by the BYOK UI decrypt here: first 32 chars of SECRET_KEY, padded to
    32 bytes with '0', urlsafe-base64-encoded."""
    secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    key_bytes = (secret[:32]).encode().ljust(32, b"0")
    return base64.urlsafe_b64encode(key_bytes)


def get_voyage_key_from_vault(conn, tenant_id):
    """Read + Fernet-decrypt the voyage api_key from credentials_vault."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT encrypted_key
              FROM credentials_vault
             WHERE TRIM(tenant_id) = %s
               AND provider = 'voyage'
               AND key_type = 'api_key'
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            (tenant_id.strip(),),
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        from cryptography.fernet import Fernet, InvalidToken
        f = Fernet(_derive_fernet_key())
        return f.decrypt(row[0].encode()).decode()
    except InvalidToken:
        log.error("Vault voyage key failed decryption (SECRET_KEY mismatch?).")
        return None


def resolve_api_key(args, conn):
    """Return (key, source). Order: --api-key -> VOYAGE_API_KEY -> vault."""
    if args.api_key:
        return args.api_key, "arg"
    env_key = os.environ.get("VOYAGE_API_KEY", "").strip()
    if env_key:
        return env_key, "env"
    tid = args.tenant or os.environ.get("TENANT_ID", "")
    if tid:
        k = get_voyage_key_from_vault(conn, tid)
        if k:
            return k, "vault"
    return None, "none"


def embed_text(row):
    label = (row.get("pref_label") or "").strip()
    definition = (row.get("definition") or "").strip()
    return f"{label}. {definition}".strip() if definition else label


def main():
    ap = argparse.ArgumentParser(description="Embed sali_concepts via Voyage AI")
    ap.add_argument("--api-key", default=None, help="Voyage key (else env, else vault)")
    ap.add_argument("--tenant", default=None, help="Tenant for vault lookup (default TENANT_ID env)")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("--limit", type=int, default=0, help="Limit rows to embed (0=all)")
    ap.add_argument("--branch", default=None, help="Only embed a given branch")
    ap.add_argument("--dry-run", action="store_true", help="Count rows without embedding")
    ap.add_argument("--skip-index", action="store_true", help="Do not build the HNSW index")
    args = ap.parse_args()

    conn = psycopg2.connect(get_db_url())
    try:
        vo = None
        if not args.dry_run:
            api_key, source = resolve_api_key(args, conn)
            if not api_key:
                log.error("No Voyage key found (checked --api-key, VOYAGE_API_KEY, "
                          "credentials_vault voyage/api_key for tenant).")
                sys.exit(1)
            log.info("Voyage key resolved from: %s", source)
            try:
                import voyageai
            except ImportError:
                log.error("voyageai not installed: pip install voyageai --break-system-packages")
                sys.exit(1)
            vo = voyageai.Client(api_key=api_key)

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            branch_clause = "AND branch = %(branch)s" if args.branch else ""
            limit_clause = f"LIMIT {args.limit}" if args.limit else ""
            cur.execute(f"""
                SELECT iri, pref_label, definition
                FROM sali_concepts
                WHERE (embedding IS NULL
                       OR embedded_at IS NULL
                       OR updated_at > embedded_at)
                  {branch_clause}
                ORDER BY iri
                {limit_clause}
            """, {"branch": args.branch} if args.branch else {})
            rows = cur.fetchall()

            log.info("=== Voyage Embedding Job: sali_concepts ===")
            log.info("Model:  %s  | Rows to embed: %d%s",
                     MODEL, len(rows),
                     f" (branch={args.branch})" if args.branch else "")

            if args.dry_run:
                est = sum(max(1, len(embed_text(r)) // 4) for r in rows)
                log.info("Dry run -- est. tokens: %d  est. cost: $%.4f",
                         est, est * COST_PER_1M / 1_000_000)
                return

            if rows:
                total_embedded = total_tokens = 0
                start = time.time()
                for bs in range(0, len(rows), args.batch_size):
                    batch = rows[bs:bs + args.batch_size]
                    texts = [embed_text(r) for r in batch]
                    iris = [r["iri"] for r in batch]
                    try:
                        result = vo.embed(texts, model=MODEL, input_type="document")
                        for iri, emb in zip(iris, result.embeddings):
                            vec_str = "[" + ",".join(str(v) for v in emb) + "]"
                            cur.execute(
                                "UPDATE sali_concepts SET embedding = %s::vector, "
                                "embedded_at = now() WHERE iri = %s",
                                (vec_str, iri),
                            )
                        conn.commit()
                        total_embedded += len(batch)
                        total_tokens += result.total_tokens
                        log.info("  Batch %d-%d: %d concepts, %d tokens",
                                 bs, bs + len(batch), len(batch), result.total_tokens)
                    except Exception as e:
                        log.error("  Batch %d failed: %s", bs, e)
                        conn.rollback()
                        continue
                log.info("=== Embedding Summary ===")
                log.info("  Concepts embedded: %d", total_embedded)
                log.info("  Tokens used:       %d", total_tokens)
                log.info("  Cost:              $%.4f", total_tokens * COST_PER_1M / 1_000_000)
                log.info("  Time:              %.1fs", time.time() - start)
            else:
                log.info("Nothing to embed (all rows current).")

            if not args.skip_index:
                cur.execute("SELECT count(*) AS n FROM sali_concepts WHERE embedding IS NOT NULL")
                n = cur.fetchone()["n"]
                if n > 0:
                    log.info("Building HNSW index over %d embedded rows...", n)
                    cur.execute(DEFERRED_HNSW)
                    conn.commit()
                    log.info("HNSW index ix_sali_concepts_embedding ready.")
                else:
                    log.info("No embedded rows yet; skipping HNSW build.")
    finally:
        conn.close()
    log.info("=== Done ===")


if __name__ == "__main__":
    main()
