"""eDiscovery Home JSON API.

GET /api/v1/ediscovery/home  → dashboard stats, collections, productions, ingest queue
"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/ediscovery", tags=["ediscovery-api"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()

@router.get("/home")
async def ediscovery_home_api(request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        # Collections — parent/child collapsed, active first
        cols_r = await session.execute(sa_text("""
            WITH child_stats AS (
                SELECT parent_collection_id,
                       CAST(SUM(COALESCE(NULLIF(document_count, 0), total_docs, 0)) AS bigint) AS child_total,
                       COUNT(*) AS child_count,
                       COUNT(*) FILTER (WHERE status IN ('review_ready', 'failed')) AS child_done
                FROM ediscovery_collections
                WHERE trim(tenant_id) = trim(:tid)
                  AND parent_collection_id IS NOT NULL
                GROUP BY parent_collection_id
            )
            SELECT c.id::text, c.collection_name,
                   CASE
                     WHEN cs.child_count IS NOT NULL AND cs.child_done < cs.child_count THEN 'processing'
                     WHEN cs.child_count IS NOT NULL AND cs.child_done = cs.child_count THEN 'review_ready'
                     ELSE c.status
                   END AS status,
                   CAST(CASE
                     WHEN cs.child_total IS NOT NULL THEN cs.child_total
                     ELSE COALESCE(NULLIF(c.document_count, 0), c.total_docs, 0)
                   END AS bigint) AS total_docs,
                   c.created_at, c.source_type,
                   m.matter_name, m.matter_number, cl.client_name,
                   c.matter_id::text,
                   COALESCE(cs.child_count, 0) AS child_count,
                   COALESCE(cs.child_done, 0) AS children_complete
            FROM ediscovery_collections c
            LEFT JOIN child_stats cs ON cs.parent_collection_id = c.id
            LEFT JOIN matters m ON c.matter_id = m.id AND trim(c.tenant_id) = trim(m.tenant_id)
            LEFT JOIN clients cl ON m.client_id = cl.id AND trim(m.tenant_id) = trim(cl.tenant_id)
            WHERE trim(c.tenant_id) = trim(:tid)
              AND c.parent_collection_id IS NULL
              AND COALESCE(c.is_internal, false) = false
            ORDER BY
              CASE WHEN c.status IN ('processing','collecting','ingesting','queued')
                   OR (cs.child_count IS NOT NULL AND cs.child_done < cs.child_count)
                   THEN 0 ELSE 1 END,
              c.created_at DESC
            LIMIT 30
        """), {"tid": tid})
        collections = []
        for r in cols_r.mappings().fetchall():
            row = dict(r)
            row["created_at"] = str(row["created_at"])[:10] if row.get("created_at") else None
            collections.append(row)

        # Productions
        prods_r = await session.execute(sa_text("""
            SELECT p.id::text, p.production_name, p.status, p.bates_prefix,
                   p.bates_start, p.bates_end, p.doc_count, p.created_at,
                   m.matter_name
            FROM ediscovery_productions p
            LEFT JOIN matters m ON p.matter_id = m.id AND trim(p.tenant_id) = trim(m.tenant_id)
            WHERE trim(p.tenant_id) = trim(:tid)
            ORDER BY p.created_at DESC LIMIT 20
        """), {"tid": tid})
        productions = []
        for r in prods_r.mappings().fetchall():
            row = dict(r)
            row["created_at"] = str(row["created_at"])[:10] if row.get("created_at") else None
            productions.append(row)

        # Stats
        stats_r = await session.execute(sa_text("""
            SELECT
                (SELECT COUNT(*) FROM ediscovery_collections WHERE trim(tenant_id) = trim(:tid)) AS total_collections,
                (SELECT COUNT(*) FROM ediscovery_documents WHERE trim(tenant_id) = trim(:tid)) AS total_documents,
                (SELECT COUNT(*) FROM ediscovery_productions WHERE trim(tenant_id) = trim(:tid)) AS total_productions,
                (SELECT COUNT(*) FROM ediscovery_documents WHERE trim(tenant_id) = trim(:tid) AND review_status = 'pending') AS pending_review,
                (SELECT COUNT(*) FROM ediscovery_documents WHERE trim(tenant_id) = trim(:tid) AND review_status = 'responsive') AS responsive,
                (SELECT COUNT(*) FROM ediscovery_documents WHERE trim(tenant_id) = trim(:tid) AND review_status = 'privileged') AS privileged
        """), {"tid": tid})
        stats = dict(stats_r.mappings().fetchone())

        # Matters with eDiscovery activity (collections linked)
        matters_r = await session.execute(sa_text("""
            SELECT DISTINCT m.id::text, m.matter_name, m.matter_number, cl.client_name,
                   (SELECT COUNT(*) FROM ediscovery_collections ec WHERE ec.matter_id = m.id AND trim(ec.tenant_id) = trim(m.tenant_id)) AS collection_count,
                   (SELECT COUNT(*) FROM ediscovery_documents ed
                    JOIN ediscovery_collections ec2 ON ed.collection_id = ec2.id
                    WHERE ec2.matter_id = m.id AND trim(ec2.tenant_id) = trim(m.tenant_id)) AS doc_count
            FROM ediscovery_collections c
            JOIN matters m ON c.matter_id = m.id AND trim(c.tenant_id) = trim(m.tenant_id)
            LEFT JOIN clients cl ON m.client_id = cl.id AND trim(m.tenant_id) = trim(cl.tenant_id)
            WHERE trim(c.tenant_id) = trim(:tid)
            ORDER BY m.matter_name LIMIT 30
        """), {"tid": tid})
        matters = [dict(r) for r in matters_r.mappings().fetchall()]

    return JSONResponse({
        "stats": stats,
        "collections": collections,
        "productions": productions,
        "matters": matters,
    })
