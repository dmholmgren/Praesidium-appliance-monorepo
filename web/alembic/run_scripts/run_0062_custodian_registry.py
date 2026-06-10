"""
run_0062_custodian_registry.py -- canonical custodian registry + doc link.

Per-matter registry that collapses custodian variants (name spellings, PST
filenames, email addresses) to one canonical custodian, so custodian-scoped
dedup and the cross-custodian report count a person once. Idempotent.

    docker exec -i praesidium-web python - < alembic/run_scripts/run_0062_custodian_registry.py
"""
import sys; sys.path.insert(0, "/app")
import asyncio
from sqlalchemy import text
from core.db.base import AsyncSessionLocal

STMTS = [
    """CREATE TABLE IF NOT EXISTS ediscovery_custodians (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id varchar(36) NOT NULL,
        matter_id uuid NOT NULL,
        canonical_name varchar(255) NOT NULL,
        normalized_key varchar(255) NOT NULL,
        aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
        emails  jsonb NOT NULL DEFAULT '[]'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_edr_custodian_key UNIQUE (tenant_id, matter_id, normalized_key)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_edr_custodians_matter ON ediscovery_custodians (tenant_id, matter_id)",
    "CREATE INDEX IF NOT EXISTS ix_edr_custodians_emails ON ediscovery_custodians USING gin (emails)",
    "ALTER TABLE ediscovery_documents ADD COLUMN IF NOT EXISTS custodian_id uuid",
    "CREATE INDEX IF NOT EXISTS ix_edr_docs_custodian_id ON ediscovery_documents (custodian_id)",
]


async def main():
    async with AsyncSessionLocal() as s:
        for st in STMTS:
            await s.execute(text(st))
        await s.execute(text("UPDATE alembic_version SET version_num='0062_custodian_registry'"))
        await s.commit()
        tbl = (await s.execute(text("SELECT to_regclass('public.ediscovery_custodians') IS NOT NULL"))).scalar()
        col = (await s.execute(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='ediscovery_documents' AND column_name='custodian_id'"))).scalar()
        head = (await s.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        print(f"ediscovery_custodians present: {tbl}  custodian_id col: {col}  head: {head}")


if __name__ == "__main__":
    asyncio.run(main())
