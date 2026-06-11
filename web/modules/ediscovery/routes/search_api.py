"""
modules/ediscovery/routes/search_api.py
JSON API for the React eDiscovery Search page.
"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/ediscovery/search", tags=["ediscovery-search-api"])


def _tenant(r: Request) -> str:
    return (getattr(r.state, "tenant_id", "") or "").strip()


def _serialize(obj):
    import uuid
    from datetime import datetime, date
    from decimal import Decimal
    if obj is None: return None
    if isinstance(obj, dict): return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list): return [_serialize(v) for v in obj]
    if isinstance(obj, uuid.UUID): return str(obj)
    if isinstance(obj, (datetime, date)): return obj.isoformat()
    if isinstance(obj, Decimal): return float(obj)
    if isinstance(obj, bytes): return obj.decode("utf-8", errors="replace")
    return obj


@router.get("/matters")
async def search_matters(request: Request, user=Depends(get_current_user)):
    """All matters with eDiscovery collections for the matter picker."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT DISTINCT m.id::text, m.matter_name, m.matter_number, c.client_name
                FROM matters m
                LEFT JOIN clients c ON m.client_id = c.id AND trim(m.tenant_id) = trim(c.tenant_id)
                WHERE trim(m.tenant_id) = trim(:tid)
                AND EXISTS (SELECT 1 FROM ediscovery_collections ec WHERE ec.matter_id = m.id AND trim(ec.tenant_id) = trim(:tid))
                ORDER BY m.matter_name
            """), {"tid": tid})
            matters = [dict(row) for row in r.mappings().fetchall()]
        return JSONResponse(_serialize(matters))
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


@router.get("/facets")
async def search_facets(request: Request, matter_id: str = Query(""), user=Depends(get_current_user)):
    """Facet aggregations for left rail."""
    tid = _tenant(request)
    facets = {"doc_types": [], "review_statuses": [], "custodians": [], "collections": [], "tags": []}
    try:
        async with AsyncSessionLocal() as session:
            # Base filter
            where = "trim(ed.tenant_id::text) = trim(:tid)"
            params = {"tid": tid}
            if matter_id:
                where += " AND ed.collection_id IN (SELECT id FROM ediscovery_collections WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid))"
                params["mid"] = matter_id

            # Collections
            r = await session.execute(sa_text(f"""
                SELECT ec.id::text, ec.collection_name as name, COUNT(ed.id) as cnt
                FROM ediscovery_collections ec
                LEFT JOIN ediscovery_documents ed ON ed.collection_id = ec.id AND (ed.is_duplicate IS NOT TRUE)
                WHERE trim(ec.tenant_id) = trim(:tid)
                {'AND ec.matter_id = CAST(:mid AS uuid)' if matter_id else ''}
                GROUP BY ec.id, ec.collection_name ORDER BY cnt DESC LIMIT 20
            """), params)
            facets["collections"] = [dict(row) for row in r.mappings().fetchall()]

            # Doc types
            r = await session.execute(sa_text(f"""
                SELECT ed.doc_type, COUNT(*) as cnt FROM ediscovery_documents ed
                WHERE {where} AND ed.doc_type IS NOT NULL AND (ed.is_duplicate IS NOT TRUE)
                GROUP BY ed.doc_type ORDER BY cnt DESC LIMIT 20
            """), params)
            facets["doc_types"] = [dict(row) for row in r.mappings().fetchall()]

            # Review statuses
            r = await session.execute(sa_text(f"""
                SELECT COALESCE(ed.review_status, 'unreviewed') as review_status, COUNT(*) as cnt
                FROM ediscovery_documents ed
                WHERE {where} AND (ed.is_duplicate IS NOT TRUE)
                GROUP BY COALESCE(ed.review_status, 'unreviewed') ORDER BY cnt DESC
            """), params)
            facets["review_statuses"] = [dict(row) for row in r.mappings().fetchall()]

            # Custodians
            r = await session.execute(sa_text(f"""
                SELECT ed.custodian, COUNT(*) as cnt FROM ediscovery_documents ed
                WHERE {where} AND ed.custodian IS NOT NULL AND ed.custodian != '' AND (ed.is_duplicate IS NOT TRUE)
                GROUP BY ed.custodian ORDER BY cnt DESC LIMIT 20
            """), params)
            facets["custodians"] = [dict(row) for row in r.mappings().fetchall()]

            # Tags (with doc counts for this matter scope)
            tag_params = {"tid": tid}
            tag_matter_filter = ""
            if matter_id:
                tag_matter_filter = "AND (t.collection_id IN (SELECT id FROM ediscovery_collections WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)) OR t.collection_id IS NULL)"
                tag_params["mid"] = matter_id
            r_tags = await session.execute(sa_text(f"""
                SELECT t.id::text AS tag_id, t.name, t.category, t.color, t.is_system,
                       COUNT(dt.document_id) AS cnt
                FROM tags t
                LEFT JOIN document_tags dt ON dt.tag_id = t.id AND trim(dt.tenant_id::text) = trim(:tid)
                WHERE trim(t.tenant_id::text) = trim(:tid) {tag_matter_filter}
                GROUP BY t.id, t.name, t.category, t.color, t.is_system
                ORDER BY cnt DESC, t.name
                LIMIT 30
            """), tag_params)
            facets["tags"] = [dict(row) for row in r_tags.mappings().fetchall()]

        return JSONResponse(_serialize(facets))
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


@router.get("/results")
async def search_results(
    request: Request,
    q: str = Query(""), matter_id: str = Query(""),
    collection_id: str = Query(""), doc_type: str = Query(""),
    review_status: str = Query(""), custodian: str = Query(""),
    date_from: str = Query(""), date_to: str = Query(""),
    tag_ids: str = Query(""),
    collection_ids: str = Query(""),
    doc_types: str = Query(""),
    review_statuses: str = Query(""),
    custodians: str = Query(""),
    sort_by: str = Query(""),
    sort_dir: str = Query("desc"),
    page: int = Query(1), per_page: int = Query(25),
    search_mode: str = Query("keyword"),
    hide_dupes: str = Query("1"),
    user=Depends(get_current_user),
):
    """Search eDiscovery documents — ES for relevance, PG for metadata."""
    tid = _tenant(request)
    offset = (max(1, page) - 1) * per_page
    per_page = min(100, max(1, per_page))

    # ── Try Elasticsearch for relevance scoring ──
    es_ids = None
    es_total = 0
    if q:
        try:
            from core.services.search_service import SearchService
            es_results = await SearchService.search_documents(
                tenant_id=tid, query=q, matter_id=matter_id or None,
                document_type=doc_type or None, limit=per_page, offset=offset,
            )
            if es_results and es_results.get("total", 0) > 0:
                es_ids = [r["id"] for r in es_results.get("results", []) if r.get("id")]
                es_total = es_results["total"]
        except Exception as e:
            logger.warning("ES search failed, falling back to PG: %s", e)

    # ── If ES returned IDs, hydrate from ediscovery_documents ──
    if es_ids:
        try:
            async with AsyncSessionLocal() as session:
                # Build placeholders for the IN clause
                id_params = {f"eid{i}": eid for i, eid in enumerate(es_ids)}
                id_placeholders = ", ".join(f"CAST(:eid{i} AS uuid)" for i in range(len(es_ids)))

                r = await session.execute(sa_text(f"""
                    SELECT ed.id::text, ed.file_name, ed.file_size, ed.mime_type,
                           ed.doc_type, ed.doc_date, ed.custodian, ed.collection_id::text,
                           ed.email_from, ed.email_to, ed.email_subject, ed.email_date,
                           ed.review_status, ed.privilege_status, ed.is_duplicate,
                           ed.bates_begin, ed.bates_end, ed.page_count,
                           LEFT(ed.extracted_text, 300) as snippet,
                       ed.author, ed.original_path,
                           ec.collection_name
                    FROM ediscovery_documents ed
                    LEFT JOIN ediscovery_collections ec ON ec.id = ed.collection_id
                    WHERE ed.id IN ({id_placeholders})
                      AND trim(ed.tenant_id::text) = trim(:tid)
                """), {**id_params, "tid": tid})
                rows_by_id = {}
                for row in r.mappings().fetchall():
                    rec = dict(row)
                    name = rec.get("file_name") or rec.get("email_subject") or "(untitled)"
                    rec["display_name"] = name.replace("\\", "/").rsplit("/", 1)[-1]
                    rec["status_display"] = (rec.get("review_status") or "unreviewed").replace("_", " ")
                    rows_by_id[rec["id"]] = rec

                # Return in ES relevance order, skip IDs not found in ediscovery_documents
                results = [rows_by_id[eid] for eid in es_ids if eid in rows_by_id]

                if results:
                    # Use ES total but note some may not be eDiscovery docs
                    total_pages = max(1, (es_total + per_page - 1) // per_page)
                    return JSONResponse(_serialize({
                        "results": results, "total": es_total, "page": page,
                        "per_page": per_page, "total_pages": total_pages,
                        "source": "elasticsearch+pg",
                    }))
                # If no eDiscovery docs matched ES hits, fall through to PG
        except Exception as e:
            logger.warning("ES hydration failed, falling back to PG: %s", e)

    # ── PostgreSQL full search (fallback or non-ES path) ──
    where = ["trim(ed.tenant_id::text) = trim(:tid)"]
    params = {"tid": tid, "limit": per_page, "offset": offset}

    if hide_dupes == "1":
        where.append("(ed.is_duplicate IS NOT TRUE)")
    if matter_id:
        where.append("ed.collection_id IN (SELECT id FROM ediscovery_collections WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid))")
        params["mid"] = matter_id
    if collection_id:
        where.append("ed.collection_id = CAST(:cid AS uuid)")
        params["cid"] = collection_id
    if doc_type:
        where.append("ed.doc_type = :dtype")
        params["dtype"] = doc_type
    if review_status:
        if review_status == "unreviewed":
            where.append("COALESCE(ed.review_status, 'unreviewed') = 'unreviewed'")
        else:
            where.append("ed.review_status = :rstatus")
            params["rstatus"] = review_status
    if custodian:
        where.append("LOWER(ed.custodian) LIKE '%%'||LOWER(:cust)||'%%'")
        params["cust"] = custodian
    if tag_ids:
        tag_id_list = [t.strip() for t in tag_ids.split(",") if t.strip()]
        if tag_id_list:
            tag_placeholders = ", ".join(f"CAST(:tagid{i} AS uuid)" for i in range(len(tag_id_list)))
            where.append(f"ed.id IN (SELECT document_id FROM document_tags WHERE tag_id IN ({tag_placeholders}) AND trim(tenant_id::text) = trim(:tid))")
            for i, tid_val in enumerate(tag_id_list):
                params[f"tagid{i}"] = tid_val
    # Multi-value filters (comma-separated from facet checkboxes)
    if collection_ids:
        cid_list = [c.strip() for c in collection_ids.split(",") if c.strip()]
        if cid_list:
            cid_ph = ", ".join(f"CAST(:cid{i} AS uuid)" for i in range(len(cid_list)))
            where.append(f"ed.collection_id IN ({cid_ph})")
            for i, cv in enumerate(cid_list):
                params[f"cid{i}"] = cv
    if doc_types and not doc_type:
        dt_list = [d.strip() for d in doc_types.split(",") if d.strip()]
        if dt_list:
            dt_ph = ", ".join(f":dtype{i}" for i in range(len(dt_list)))
            where.append(f"ed.doc_type IN ({dt_ph})")
            for i, dv in enumerate(dt_list):
                params[f"dtype{i}"] = dv
    if review_statuses and not review_status:
        rs_list = [r.strip() for r in review_statuses.split(",") if r.strip()]
        if rs_list:
            rs_ph = ", ".join(f":rstatus{i}" for i in range(len(rs_list)))
            # Handle 'unreviewed' specially
            if 'unreviewed' in rs_list:
                others = [r for r in rs_list if r != 'unreviewed']
                if others:
                    ors_ph = ", ".join(f":rstatus{i}" for i, r in enumerate(others))
                    where.append(f"(COALESCE(ed.review_status, 'unreviewed') = 'unreviewed' OR ed.review_status IN ({ors_ph}))")
                    for i, rv in enumerate(others):
                        params[f"rstatus{i}"] = rv
                else:
                    where.append("COALESCE(ed.review_status, 'unreviewed') = 'unreviewed'")
            else:
                where.append(f"ed.review_status IN ({rs_ph})")
                for i, rv in enumerate(rs_list):
                    params[f"rstatus{i}"] = rv
    if custodians and not custodian:
        cu_list = [c.strip() for c in custodians.split(",") if c.strip()]
        if cu_list:
            cu_ph = ", ".join(f":cust{i}" for i in range(len(cu_list)))
            where.append(f"ed.custodian IN ({cu_ph})")
            for i, cv in enumerate(cu_list):
                params[f"cust{i}"] = cv
    if date_from:
        where.append("ed.doc_date >= CAST(:dfrom AS date)")
        params["dfrom"] = date_from
    if date_to:
        where.append("ed.doc_date <= CAST(:dto AS date)")
        params["dto"] = date_to
    if q:
        where.append("(LOWER(COALESCE(ed.file_name,''))||' '||LOWER(COALESCE(ed.email_subject,''))||' '||LOWER(COALESCE(ed.extracted_text,''))||' '||LOWER(COALESCE(ed.custodian,''))) LIKE '%%'||LOWER(:q)||'%%'")
        params["q"] = q

    wsql = " AND ".join(where)

    # Build sort clause
    _sort_map = {
        "display_name": "COALESCE(ed.email_subject, ed.file_name)",
        "doc_type": "ed.doc_type",
        "custodian": "ed.custodian",
        "email_subject": "ed.email_subject",
        "email_from": "ed.email_from",
        "email_to": "ed.email_to",
        "email_date": "ed.email_date",
        "doc_date": "ed.doc_date",
        "file_size": "ed.file_size",
        "page_count": "ed.page_count",
        "bates_begin": "ed.bates_begin",
        "review_status": "ed.review_status",
        "privilege_status": "ed.privilege_status",
        "collection_name": "ec.collection_name",
        "file_name": "ed.file_name",
        "relevance_score": "ed.relevance_score",
    }
    _sd = "ASC" if sort_dir == "asc" else "DESC"
    if sort_by and sort_by in _sort_map:
        sort_clause = f"{_sort_map[sort_by]} {_sd} NULLS LAST, ed.file_name ASC"
    else:
        sort_clause = "COALESCE(ed.doc_date, ed.ingested_at::date) DESC NULLS LAST, ed.file_name ASC"

    try:
        async with AsyncSessionLocal() as session:
            r_total = await session.execute(sa_text(f"SELECT COUNT(*) FROM ediscovery_documents ed WHERE {wsql}"), params)
            total = int(r_total.scalar() or 0)

            r = await session.execute(sa_text(f"""
                SELECT ed.id::text, ed.file_name, ed.file_size, ed.mime_type,
                       ed.doc_type, ed.doc_date, ed.custodian, ed.collection_id::text,
                       ed.email_from, ed.email_to, ed.email_subject, ed.email_date,
                       ed.review_status, ed.privilege_status, ed.is_duplicate,
                       ed.bates_begin, ed.bates_end, ed.page_count,
                       LEFT(ed.extracted_text, 300) as snippet,
                       ec.collection_name
                FROM ediscovery_documents ed
                LEFT JOIN ediscovery_collections ec ON ec.id = ed.collection_id
                WHERE {wsql}
                ORDER BY {sort_clause}
                LIMIT :limit OFFSET :offset
            """), params)
            rows = [dict(row) for row in r.mappings().fetchall()]

        results = []
        for rec in rows:
            name = rec.get("file_name") or rec.get("email_subject") or "(untitled)"
            rec["display_name"] = name.replace("\\", "/").rsplit("/", 1)[-1]
            rec["status_display"] = (rec.get("review_status") or "unreviewed").replace("_", " ")
            results.append(rec)

        total_pages = max(1, (total + per_page - 1) // per_page)
        return JSONResponse(_serialize({
            "results": results, "total": total, "page": page, "per_page": per_page,
            "total_pages": total_pages, "source": "postgresql",
        }))
    except Exception as e:
        logger.error("search_results: %s", e)
        return JSONResponse({"error": str(e)}, 500)

@router.get("/columns")
async def search_columns(request: Request, user=Depends(get_current_user)):
    """Available metadata columns for the search results grid."""
    return JSONResponse([
        {"key": "display_name",    "label": "Document",       "default": True,  "width": 280, "sortable": True},
        {"key": "doc_type",        "label": "Type",           "default": True,  "width": 70,  "sortable": True},
        {"key": "custodian",       "label": "Custodian",      "default": True,  "width": 130, "sortable": True},
        {"key": "email_subject",   "label": "Subject",        "default": False, "width": 200, "sortable": True},
        {"key": "email_from",      "label": "From",           "default": True,  "width": 150, "sortable": True},
        {"key": "email_to",        "label": "To",             "default": False, "width": 150, "sortable": True},
        {"key": "email_date",      "label": "Email Date",     "default": False, "width": 100, "sortable": True},
        {"key": "doc_date",        "label": "Date",           "default": True,  "width": 90,  "sortable": True},
        {"key": "file_size",       "label": "Size",           "default": True,  "width": 70,  "sortable": True},
        {"key": "page_count",      "label": "Pages",          "default": False, "width": 60,  "sortable": True},
        {"key": "bates_begin",     "label": "Bates Begin",    "default": False, "width": 100, "sortable": True},
        {"key": "bates_end",       "label": "Bates End",      "default": False, "width": 100, "sortable": True},
        {"key": "review_status",   "label": "Review",         "default": True,  "width": 90,  "sortable": True},
        {"key": "privilege_status","label": "Privilege",       "default": False, "width": 90,  "sortable": True},
        {"key": "collection_name", "label": "Collection",     "default": True,  "width": 130, "sortable": True},
        {"key": "author",          "label": "Author",         "default": False, "width": 120, "sortable": True},
        {"key": "file_name",       "label": "File Name",      "default": False, "width": 180, "sortable": True},
        {"key": "original_path",   "label": "Original Path",  "default": False, "width": 250, "sortable": True},
        {"key": "is_duplicate",    "label": "Duplicate",      "default": False, "width": 70,  "sortable": True},
        {"key": "relevance_score", "label": "Relevance",      "default": False, "width": 80,  "sortable": True},
    ])

