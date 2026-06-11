"""eDiscovery Matter Home JSON API — OPTIMIZED.

GET /api/v1/ediscovery/matter/{matter_id}/home
Single-pass stats query instead of 7 correlated subqueries.
"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/ediscovery/matter", tags=["ediscovery-matter-api"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()


@router.get("/{matter_id}/home")
async def ediscovery_matter_home_api(request: Request, matter_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        # -- Matter info --
        mr = await session.execute(sa_text("""
            SELECT m.id::text, m.matter_name, m.matter_number, m.matter_type, m.status,
                   cl.client_name
            FROM matters m
            LEFT JOIN clients cl ON m.client_id = cl.id AND trim(m.tenant_id) = trim(cl.tenant_id)
            WHERE m.id = CAST(:mid AS uuid) AND trim(m.tenant_id) = trim(:tid)
        """), {"mid": matter_id, "tid": tid})
        matter_row = mr.mappings().fetchone()
        if not matter_row:
            return JSONResponse({"error": "Matter not found"}, status_code=404)
        matter = dict(matter_row)

        # -- Pre-fetch collection IDs for this matter (small set) --
        # Include ALL collections (parents + children) for doc stats,
        # but count only top-level for the display count
        cid_r = await session.execute(sa_text("""
            SELECT id, parent_collection_id FROM ediscovery_collections
            WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)
        """), {"mid": matter_id, "tid": tid})
        all_colls = cid_r.mappings().fetchall()
        coll_ids = [r["id"] for r in all_colls]
        total_collections = sum(1 for r in all_colls
                                if r["parent_collection_id"] is None
                                and not False)  # is_internal checked in display query

        # -- Single-pass stats (conditional aggregation) --
        if coll_ids:
            stats_r = await session.execute(sa_text("""
                SELECT
                    COUNT(*) AS total_documents,
                    COUNT(*) FILTER (WHERE review_status IS NOT NULL
                                       AND review_status NOT IN ('pending','unreviewed')) AS reviewed,
                    COUNT(*) FILTER (WHERE review_status IS NULL
                                       OR review_status IN ('pending','unreviewed')) AS pending_review,
                    COUNT(*) FILTER (WHERE review_status = 'responsive') AS responsive,
                    COUNT(*) FILTER (WHERE review_status = 'not_responsive') AS not_responsive,
                    COUNT(*) FILTER (WHERE review_status = 'privileged'
                                       OR privilege_status = 'privileged') AS privileged
                FROM ediscovery_documents
                WHERE collection_id = ANY(CAST(:cids AS uuid[]))
            """), {"cids": coll_ids})
            stats = dict(stats_r.mappings().fetchone())
        else:
            stats = {"total_documents": 0, "reviewed": 0, "pending_review": 0,
                     "responsive": 0, "not_responsive": 0, "privileged": 0}
        stats["total_collections"] = total_collections

        # -- Doc type breakdown (single pass, uses collection_id index) --
        if coll_ids:
            dt_r = await session.execute(sa_text("""
                SELECT COALESCE(doc_type, 'Unknown') AS doc_type, COUNT(*) AS cnt
                FROM ediscovery_documents
                WHERE collection_id = ANY(CAST(:cids AS uuid[]))
                GROUP BY COALESCE(doc_type, 'Unknown')
                ORDER BY cnt DESC
                LIMIT 20
            """), {"cids": coll_ids})
            doc_types = [dict(r) for r in dt_r.mappings().fetchall()]
        else:
            doc_types = []

        # -- Custodians (single pass) --
        if coll_ids:
            cu_r = await session.execute(sa_text("""
                SELECT COALESCE(ed.custodian, 'Unknown') AS custodian,
                       COUNT(*) AS cnt
                FROM ediscovery_documents ed
                WHERE ed.collection_id = ANY(CAST(:cids AS uuid[]))
                  AND ed.custodian IS NOT NULL AND ed.custodian != ''
                GROUP BY COALESCE(ed.custodian, 'Unknown')
                ORDER BY cnt DESC
                LIMIT 20
            """), {"cids": coll_ids})
            custodians = [dict(r) for r in cu_r.mappings().fetchall()]
        else:
            custodians = []

        # -- Collections list (parent/child collapsed, internal hidden) --
        # Show top-level collections only. For parents with children,
        # roll up total_docs from children. Hide is_internal children.
        cols_r = await session.execute(sa_text("""
            WITH child_stats AS (
                SELECT parent_collection_id,
                       CAST(SUM(COALESCE(NULLIF(document_count, 0), total_docs, 0)) AS bigint) AS child_total,
                       COUNT(*) AS child_count,
                       COUNT(*) FILTER (WHERE status IN ('review_ready', 'failed')) AS child_done,
                       COUNT(*) FILTER (WHERE status = 'failed') AS child_failed
                FROM ediscovery_collections
                WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)
                  AND parent_collection_id IS NOT NULL
                GROUP BY parent_collection_id
            )
            SELECT c.id::text, c.collection_name, 
                   CASE 
                     WHEN cs.child_count IS NOT NULL AND cs.child_done < cs.child_count THEN 'processing'
                     WHEN cs.child_count IS NOT NULL AND cs.child_failed > 0 AND cs.child_done = cs.child_count THEN 'review_ready'
                     WHEN cs.child_count IS NOT NULL AND cs.child_done = cs.child_count THEN 'review_ready'
                     ELSE c.status
                   END AS status,
                   CAST(CASE
                     WHEN cs.child_total IS NOT NULL THEN cs.child_total
                     ELSE COALESCE(NULLIF(c.document_count, 0), c.total_docs, 0)
                   END AS bigint) AS total_docs,
                   c.source_type, c.created_at,
                   COALESCE(cs.child_count, 0) AS child_count,
                   COALESCE(cs.child_done, 0) AS children_complete
            FROM ediscovery_collections c
            LEFT JOIN child_stats cs ON cs.parent_collection_id = c.id
            WHERE c.matter_id = CAST(:mid AS uuid) AND trim(c.tenant_id) = trim(:tid)
              AND c.parent_collection_id IS NULL
              AND COALESCE(c.is_internal, false) = false
            ORDER BY c.created_at DESC
        """), {"mid": matter_id, "tid": tid})
        collections = []
        for r in cols_r.mappings().fetchall():
            row = dict(r)
            row["created_at"] = str(row["created_at"])[:10] if row.get("created_at") else None
            collections.append(row)

        # -- Productions --
        productions = []
        try:
            pr_r = await session.execute(sa_text("""
                SELECT p.id::text, p.name, p.status,
                       COALESCE(p.doc_count, 0) AS doc_count,
                       COALESCE(p.page_count, 0) AS page_count,
                       p.start_bates, p.end_bates,
                       p.created_at
                FROM production_sets p
                WHERE p.matter_id = CAST(:mid AS uuid) AND trim(p.tenant_id) = trim(:tid)
                ORDER BY p.created_at DESC
            """), {"mid": matter_id, "tid": tid})
            for r in pr_r.mappings().fetchall():
                row = dict(r)
                row["created_at"] = str(row["created_at"])[:10] if row.get("created_at") else None
                productions.append(row)
        except Exception:
            pass

        # -- Deadlines --
        deadlines = []
        try:
            dl_r = await session.execute(sa_text("""
                SELECT title, deadline_date, urgency,
                       TO_CHAR(deadline_date, 'Mon DD') AS date_label
                FROM scheduling_deadlines
                WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)
                  AND deadline_date >= CURRENT_DATE
                ORDER BY deadline_date ASC
                LIMIT 10
            """), {"mid": matter_id, "tid": tid})
            deadlines = [dict(r) for r in dl_r.mappings().fetchall()]
            for d in deadlines:
                if d.get("deadline_date"):
                    d["deadline_date"] = str(d["deadline_date"])
        except Exception:
            pass

        # -- Tasks --
        tasks = []
        try:
            tk_r = await session.execute(sa_text("""
                SELECT title, priority, due_date,
                       TO_CHAR(due_date, 'Mon DD') AS date_label
                FROM tasks
                WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)
                  AND status != 'completed'
                ORDER BY
                  CASE priority WHEN 'urgent' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 ELSE 4 END,
                  due_date ASC NULLS LAST
                LIMIT 10
            """), {"mid": matter_id, "tid": tid})
            tasks = [dict(r) for r in tk_r.mappings().fetchall()]
            for t in tasks:
                if t.get("due_date"):
                    t["due_date"] = str(t["due_date"])
        except Exception:
            pass

        # -- Projects --
        projects = []
        try:
            pj_r = await session.execute(sa_text("""
                SELECT id::text, title AS name, template_type AS type, status,
                       0 AS doc_count, NULL AS progress
                FROM projects
                WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)
                  AND status IN ('active', 'processing', 'pending')
                ORDER BY created_at DESC
                LIMIT 10
            """), {"mid": matter_id, "tid": tid})
            projects = [dict(r) for r in pj_r.mappings().fetchall()]
        except Exception:
            pass

    return JSONResponse({
        "matter": matter,
        "stats": stats,
        "doc_types": doc_types,
        "custodians": custodians,
        "collections": collections,
        "productions": productions,
        "deadlines": deadlines,
        "tasks": tasks,
        "projects": projects,
    })
