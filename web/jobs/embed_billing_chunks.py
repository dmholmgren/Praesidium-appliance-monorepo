#!/usr/bin/env python3
"""
jobs/embed_billing_chunks.py
Embedding job — reads unembedded billing_chunks, sends to Voyage AI
voyage-law-2, writes vectors to billing_chunk_embeddings.

Requires:
  - VOYAGE_API_KEY env var (or pass --api-key)
  - pip install voyageai (inside container)

Usage:
    sudo docker exec -e VOYAGE_API_KEY=pa-... praesidium-web \
        python3 /app/jobs/embed_billing_chunks.py \
        --tenant 986c0fee-1390-43bb-ad28-8cd1db6de53f

Batch size tuned for voyage-law-2 limits:
  - Max 1,000 texts per call
  - Max 120,000 tokens per call
  - We use batches of 64 to stay safe (~64 * 500 tokens = 32K)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

log = logging.getLogger("embed_billing")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

MODEL = "voyage-law-2"
EMBEDDING_DIM = 1024
BATCH_SIZE = 64  # Conservative to stay under 120K token limit


def get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return "host=localhost port=5432 dbname=praesidium user=praesidium"
    return url.replace("postgresql+asyncpg://", "postgresql://")


def main():
    parser = argparse.ArgumentParser(description="Embed billing chunks via Voyage AI")
    parser.add_argument("--tenant", required=True, help="Tenant ID")
    parser.add_argument("--api-key", default=None, help="Voyage API key (or set VOYAGE_API_KEY)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--limit", type=int, default=0, help="Limit chunks to embed (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="Count chunks without embedding")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("VOYAGE_API_KEY", "")
    if not api_key and not args.dry_run:
        log.error("No API key. Set VOYAGE_API_KEY or pass --api-key")
        sys.exit(1)

    tid = args.tenant.strip()

    # Import voyageai
    if not args.dry_run:
        try:
            import voyageai
        except ImportError:
            log.error("voyageai not installed. Run: pip install voyageai --break-system-packages")
            sys.exit(1)
        vo = voyageai.Client(api_key=api_key)

    conn = psycopg2.connect(get_db_url())

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Find unembedded chunks
            limit_clause = f"LIMIT {args.limit}" if args.limit else ""
            cur.execute(f"""
                SELECT bc.id, bc.embedded_content, bc.token_count
                FROM billing_chunks bc
                LEFT JOIN billing_chunk_embeddings bce
                    ON bce.chunk_id = bc.id AND bce.tenant_id = bc.tenant_id
                WHERE TRIM(bc.tenant_id) = %s
                  AND bce.id IS NULL
                  AND bc.embedded_content IS NOT NULL
                  AND bc.embedded_content != ''
                ORDER BY bc.chunked_at
                {limit_clause}
            """, (tid,))
            chunks = cur.fetchall()

            log.info("=== Voyage Embedding Job ===")
            log.info("Model:     %s", MODEL)
            log.info("Tenant:    %s", tid)
            log.info("Chunks:    %d to embed", len(chunks))

            if args.dry_run:
                total_tokens = sum(c.get("token_count") or 0 for c in chunks)
                log.info("Dry run — estimated tokens: %d", total_tokens)
                log.info("Estimated cost at $0.12/1M tokens: $%.4f",
                         total_tokens * 0.12 / 1_000_000)
                return

            if not chunks:
                log.info("Nothing to embed.")
                return

            total_embedded = 0
            total_tokens_used = 0
            start = time.time()

            # Process in batches
            for batch_start in range(0, len(chunks), args.batch_size):
                batch = chunks[batch_start:batch_start + args.batch_size]
                texts = [c["embedded_content"] for c in batch]
                chunk_ids = [c["id"] for c in batch]

                try:
                    t0 = time.time()
                    result = vo.embed(
                        texts,
                        model=MODEL,
                        input_type="document",
                    )
                    duration_ms = int((time.time() - t0) * 1000)

                    embeddings = result.embeddings
                    tokens_used = result.total_tokens

                    # Write embeddings
                    for j, (chunk_id, embedding) in enumerate(zip(chunk_ids, embeddings)):
                        # Format as pgvector literal
                        vec_str = "[" + ",".join(str(v) for v in embedding) + "]"

                        cur.execute("""
                            INSERT INTO billing_chunk_embeddings (
                                id, tenant_id, chunk_id, embedding_model,
                                embedding_duration_ms, cost_usd, embedding_1024
                            ) VALUES (
                                gen_random_uuid(), %s, %s::uuid, %s,
                                %s, %s, %s::vector
                            )
                            ON CONFLICT DO NOTHING
                        """, (
                            tid,
                            str(chunk_id),
                            MODEL,
                            duration_ms // len(batch),
                            round(tokens_used * 0.12 / 1_000_000, 6),
                            vec_str,
                        ))

                    conn.commit()
                    total_embedded += len(batch)
                    total_tokens_used += tokens_used

                    log.info("  Batch %d-%d: %d chunks, %d tokens, %dms",
                             batch_start, batch_start + len(batch),
                             len(batch), tokens_used, duration_ms)

                except Exception as e:
                    log.error("  Batch %d failed: %s", batch_start, e)
                    conn.rollback()
                    # Continue with next batch
                    continue

            elapsed = time.time() - start
            cost = total_tokens_used * 0.12 / 1_000_000

            log.info("=== Embedding Summary ===")
            log.info("  Chunks embedded:  %d", total_embedded)
            log.info("  Tokens used:      %d", total_tokens_used)
            log.info("  Cost:             $%.4f", cost)
            log.info("  Time:             %.1fs", elapsed)

    finally:
        conn.close()

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
