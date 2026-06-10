"""
run_0061_custodian_source.py -- add per-document custodian provenance.

ediscovery_documents.custodian_source records HOW the custodian was derived
(pst_owner | folder | collection | email_from | load_file | manual | unresolved),
so custodian-scoped dedup and the cross-custodian report are auditable and the
collection != custodian distinction is explicit. Idempotent.

    docker exec -i praesidium-web python - < alembic/run_scripts/run_0061_custodian_source.py
"""
import sys; sys.path.insert(0, "/app")
import asyncio
from sqlalchemy import text
from core.db.base import AsyncSessionLocal

STMTS = [
    "ALTER TABLE ediscovery_documents ADD COLUMN IF NOT EXISTS custodian_source varchar(32)",
    # quick review surface: which docs ended up unresolved (conservative inclusion)
    ("CREATE INDEX IF NOT EXISTS ix_edr_docs_custodian_unresolved "
     "ON ediscovery_documents (collection_id) WHERE custodian_source = 'unresolved'"),
]


async def main():
    async with AsyncSessionLocal() as s:
        for st in STMTS:
            await s.execute(text(st))
        await s.execute(text("UPDATE alembic_version SET version_num='0061_custodian_source'"))
        await s.commit()
        col = (await s.execute(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='ediscovery_documents' AND column_name='custodian_source'"))).scalar()
        head = (await s.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        print(f"custodian_source present: {col}  alembic head: {head}")


if __name__ == "__main__":
    asyncio.run(main())
