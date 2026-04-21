"""
modules/ediscovery/services/collection_service.py

Data sources for eDiscovery collection widgets.
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def get_collection_summary(scope: dict) -> dict:
    """
    Data source for widget: ediscovery_collection_status

    Returns a summary of eDiscovery collections for the tenant,
    optionally scoped to a specific matter.

    Scope keys:
      tenant_id   — required
      matter_id   — optional, filters to one matter
      limit       — optional, max collections to return (default 10)

    Returns:
      {
        "total": int,
        "by_status": {"collecting": N, "processing": N, "review_ready": N, ...},
        "collections": [...],   # recent/active collections
        "has_active": bool,
      }
    """
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text as sa_text

    tenant_id = (scope.get("tenant_id") or "").strip()
    matter_id = scope.get("matter_id")
    limit = int(scope.get("limit") or 10)

    try:
        async with AsyncSessionLocal() as session:
            params: dict[str, Any] = {"tid": tenant_id, "limit": limit}

            matter_clause = ""
            if matter_id:
                matter_clause = " AND CAST(ec.matter_id AS text) = :mid"
                params["mid"] = str(matter_id)

            # Summary counts by status
            r_counts = await session.execute(sa_text(f"""
                SELECT
                    status,
                    COUNT(*) as cnt
                FROM ediscovery_collections ec
                WHERE TRIM(ec.tenant_id) = TRIM(:tid)
                {matter_clause}
                GROUP BY status
            """), params)
            by_status: dict[str, int] = {}
            total = 0
            for row in r_counts.fetchall():
                by_status[row[0] or "unknown"] = int(row[1])
                total += int(row[1])

            # Recent collections with matter name
            r_cols = await session.execute(sa_text(f"""
                SELECT
                    ec.id::text,
                    COALESCE(ec.collection_name, ec.name, 'Unnamed') AS collection_name,
                    ec.status,
                    ec.source_type,
                    ec.source_party,
                    ec.total_docs,
                    ec.reviewed_docs,
                    ec.processed_docs,
                    ec.created_at,
                    m.matter_name,
                    m.matter_number
                FROM ediscovery_collections ec
                LEFT JOIN matters m ON m.id = ec.matter_id
                    AND TRIM(m.tenant_id) = TRIM(ec.tenant_id)
                WHERE TRIM(ec.tenant_id) = TRIM(:tid)
                {matter_clause}
                ORDER BY ec.created_at DESC
                LIMIT :limit
            """), params)

            collections = []
            for row in r_cols.mappings().fetchall():
                d = dict(row)
                # Build progress pct
                total_docs = int(d.get("total_docs") or 0)
                reviewed   = int(d.get("reviewed_docs") or 0)
                d["pct_reviewed"] = round((reviewed / total_docs * 100)) if total_docs > 0 else 0
                d["created_at"]   = d["created_at"].isoformat() if d.get("created_at") else None
                collections.append(d)

        active_statuses = {"collecting", "processing", "review_ready"}
        has_active = any(s in by_status for s in active_statuses)

        return {
            "total": total,
            "by_status": by_status,
            "collections": collections,
            "has_active": has_active,
        }

    except Exception as exc:
        logger.error("get_collection_summary error: %s", exc)
        return {
            "total": 0,
            "by_status": {},
            "collections": [],
            "has_active": False,
            "error": str(exc),
        }
