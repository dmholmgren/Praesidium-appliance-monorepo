"""
run_0059_ediscovery_embedding_768.py -- applied migration (run-script form).

Adds the ModernBERT-768 embedding column + HNSW index to
ediscovery_chunk_embeddings, and amends the exactly-one-dim CHECK constraint to
count embedding_768 (the original constraint predated the column and counted
only 1024/1536/3072, so a 768-only row summed to zero and failed the check).

Idempotent. Applied live 2026-06-10 and alembic_version stamped to
'0059_ediscovery_embedding_768'. Run-script form matches the house convention
(schema evolution via AsyncSessionLocal + stamp; the migration chain is
file-gapped and managed via run-scripts rather than `alembic upgrade`).

    docker exec -i praesidium-web python - < alembic/run_scripts/run_0059_ediscovery_embedding_768.py
"""
import sys; sys.path.insert(0, "/app")
import asyncio
from sqlalchemy import text
from core.db.base import AsyncSessionLocal

STMTS = [
    "ALTER TABLE ediscovery_chunk_embeddings ADD COLUMN IF NOT EXISTS embedding_768 vector(768)",
    ("CREATE INDEX IF NOT EXISTS ix_edr_chunk_embeddings_768_hnsw "
     "ON ediscovery_chunk_embeddings USING hnsw (embedding_768 vector_cosine_ops) "
     "WHERE embedding_768 IS NOT NULL"),
    "ALTER TABLE ediscovery_chunk_embeddings DROP CONSTRAINT IF EXISTS ck_edr_chunk_embeddings_exactly_one_dim",
    ("ALTER TABLE ediscovery_chunk_embeddings ADD CONSTRAINT ck_edr_chunk_embeddings_exactly_one_dim "
     "CHECK ((CASE WHEN embedding_768 IS NOT NULL THEN 1 ELSE 0 END "
     "      + CASE WHEN embedding_1024 IS NOT NULL THEN 1 ELSE 0 END "
     "      + CASE WHEN embedding_1536 IS NOT NULL THEN 1 ELSE 0 END "
     "      + CASE WHEN embedding_3072 IS NOT NULL THEN 1 ELSE 0 END) = 1)"),
]


async def main():
    async with AsyncSessionLocal() as s:
        for st in STMTS:
            await s.execute(text(st))
        await s.execute(text("UPDATE alembic_version SET version_num='0059_ediscovery_embedding_768'"))
        await s.commit()
        col = (await s.execute(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='ediscovery_chunk_embeddings' AND column_name='embedding_768'"))).scalar()
        head = (await s.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        print(f"embedding_768 present: {col}  alembic head: {head}")


if __name__ == "__main__":
    asyncio.run(main())
