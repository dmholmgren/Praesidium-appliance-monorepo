"""
run_0063_layout_token_block_line.py -- add fitz block/line grouping to layout tokens.

Adds block_no / line_no (PyMuPDF "words" tuple block_no, line_no) to
doc_layout_tokens so the PERSISTED geometry artifact carries the structural
line/paragraph grouping. Consumers (paragraph segmenter via vertical-gap,
exhibit assembly, viewer, redaction burn-in) then reuse one durable artifact
without re-clustering or re-parsing the PDF. Additive + nullable: existing rows
stay valid and are repopulated on the geometry reprocess. Idempotent.

    docker exec -i praesidium-web python - < alembic/run_scripts/run_0063_layout_token_block_line.py
"""
import sys; sys.path.insert(0, "/app")
import asyncio
from sqlalchemy import text
from core.db.base import AsyncSessionLocal

STMTS = [
    "ALTER TABLE doc_layout_tokens ADD COLUMN IF NOT EXISTS block_no integer",
    "ALTER TABLE doc_layout_tokens ADD COLUMN IF NOT EXISTS line_no integer",
]


async def main():
    async with AsyncSessionLocal() as s:
        for st in STMTS:
            await s.execute(text(st))
        await s.execute(text(
            "UPDATE alembic_version SET version_num='0063_layout_token_block_line'"))
        await s.commit()
        cols = (await s.execute(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='doc_layout_tokens' "
            "AND column_name IN ('block_no','line_no')"))).scalar()
        head = (await s.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        print(f"block_no/line_no cols present: {cols}/2  head: {head}")


if __name__ == "__main__":
    asyncio.run(main())
