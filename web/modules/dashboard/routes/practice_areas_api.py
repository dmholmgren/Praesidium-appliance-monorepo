"""Practice Areas registry API.

Read-only endpoint feeding the practice-area dropdown(s). The practice_areas
table is global (no tenant scope), like document_type_taxonomy.

GET /api/v1/practice-areas
    -> { ok, total, categories: [ { category, items: [
           { code, display_name, default_matter_type, intelligence_template } ] } ] }
    Grouped by category (optgroup-ready), ordered by sort_order.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["practice-areas"])


@router.get("/practice-areas")
async def list_practice_areas(request: Request):
    """Active practice areas, grouped by category for optgroups."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(sa_text("""
                SELECT code, display_name, category,
                       default_matter_type, intelligence_template, sort_order
                FROM practice_areas
                WHERE is_active = true
                ORDER BY sort_order, display_name
            """))
            rows = [dict(m) for m in result.mappings()]

        # Group by category, preserving sort_order (first-seen) ordering.
        categories: list[dict] = []
        index: dict[str, dict] = {}
        for row in rows:
            cat = row.get("category") or "Other"
            bucket = index.get(cat)
            if bucket is None:
                bucket = {"category": cat, "items": []}
                index[cat] = bucket
                categories.append(bucket)
            bucket["items"].append({
                "code": row["code"],
                "display_name": row["display_name"],
                "default_matter_type": row["default_matter_type"],
                "intelligence_template": row["intelligence_template"],
            })

        return JSONResponse({"ok": True, "total": len(rows), "categories": categories})
    except Exception as exc:  # noqa: BLE001
        logger.exception("practice-areas list failed")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
