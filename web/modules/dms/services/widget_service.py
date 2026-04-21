"""
DMS Widget Service — modules/dms/services/widget_service.py
==============================================================
Data source functions for all DMS widgets registered in widget_registry.

Scope contract — every function receives:
    scope = {
        "tenant_id":   str,
        "user_id":     int | None,
        "matter_id":   str | None,
        "attorney_id": int | None,
        "client_id":   str | None,
        "date_from":   str | None,
        "date_to":     str | None,
        "request":     Request,
    }
And returns a plain dict of template context variables.

Scope branching convention:
    matter_id   → single matter context
    client_id   → all matters for that client
    attorney_id → all matters where this user is responsible/originating attorney
    (none set)  → firm-wide

Registered widget slugs (widget_registry):
    dms_recent_documents        data_panel  supports: firm, attorney, client, matter
    dms_doc_count               data_panel  supports: matter, client, attorney
    dms_document_activity_feed  data_panel  supports: firm, attorney, client, matter
    dms_folder_health           data_panel  supports: matter
    dms_storage_stats           data_panel  supports: firm
    matter_header               data_panel  supports: matter (shared with dashboard)
"""

import logging
from datetime import datetime

from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _fmt_size(b):
    if b is None:
        return "—"
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b / (1024 * 1024):.1f} MB"


def _ext_icon(path):
    if not path:
        return "📄"
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "pdf": "📕", "doc": "📘", "docx": "📘",
        "xls": "📗", "xlsx": "📗",
        "ppt": "📙", "pptx": "📙",
        "txt": "📄", "msg": "📧", "eml": "📧",
        "jpg": "🖼", "jpeg": "🖼", "png": "🖼",
        "tif": "🖼", "tiff": "🖼",
        "mp3": "🎵", "mp4": "🎬", "wav": "🎵",
        "zip": "📦", "wpd": "📄",
    }.get(ext, "📄")


def _fmt_dt(val) -> str:
    if isinstance(val, datetime):
        return val.strftime("%b %d")
    return str(val or "")[:10]


# ---------------------------------------------------------------------------
# matter_header
# Shared header strip — used on /dms/matter/{id} and /dashboard/matter/{id}
# Scope: matter_id required
# ---------------------------------------------------------------------------

async def get_matter_header(scope: dict) -> dict:
    """
    data_source for widget: matter_header
    Returns matter identity fields for the shared header strip.
    """
    from core.db.base import AsyncSessionLocal

    tenant_id = scope.get("tenant_id", "")
    matter_id = scope.get("matter_id")

    if not matter_id:
        return {
            "matter_name": "Unknown Matter", "matter_number": None,
            "client_name": None, "status": None,
            "responsible_attorney": None, "originating_attorney": None,
            "practice_area": None, "open_date": None,
            "matter_id": matter_id,
        }

    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT
                    m.id::text              AS matter_id,
                    m.matter_name,
                    m.matter_number,
                    m.status,
                    m.practice_area,
                    m.open_date,
                    c.client_name,
                    ou.full_name            AS responsible_attorney,
                    NULL::text              AS originating_attorney
                FROM matters m
                LEFT JOIN clients c
                    ON m.client_id = c.id
                    AND trim(c.tenant_id) = trim(m.tenant_id)
                LEFT JOIN users ou
                    ON m.originating_attorney_id = ou.id
                    AND trim(ou.tenant_id::text) = trim(m.tenant_id::text)
                WHERE m.id = CAST(:mid AS uuid)
                  AND trim(m.tenant_id) = trim(:tid)
            """), {"mid": matter_id, "tid": tenant_id})
            row = r.mappings().fetchone()

        if not row:
            return {
                "matter_name": "Matter Not Found", "matter_number": None,
                "client_name": None, "status": None,
                "responsible_attorney": None, "originating_attorney": None,
                "practice_area": None, "open_date": None,
                "matter_id": matter_id,
            }

        open_date = row.get("open_date")
        if open_date and hasattr(open_date, "strftime"):
            open_date = open_date.strftime("%b %d, %Y")
        elif open_date:
            open_date = str(open_date)[:10]

        return {
            "matter_id":              matter_id,
            "matter_name":            row["matter_name"] or "Untitled",
            "matter_number":          row.get("matter_number"),
            "client_name":            row.get("client_name"),
            "status":                 row.get("status") or "active",
            "practice_area":          row.get("practice_area"),
            "open_date":              open_date,
            "responsible_attorney":   row.get("responsible_attorney"),
            "originating_attorney":   row.get("originating_attorney"),
        }

    except Exception as exc:
        logger.error("get_matter_header error: %s", exc)
        return {
            "matter_name": "Error", "matter_number": None,
            "client_name": None, "status": None,
            "responsible_attorney": None, "originating_attorney": None,
            "practice_area": None, "open_date": None,
            "matter_id": matter_id, "error": str(exc),
        }


# ---------------------------------------------------------------------------
# dms_recent_documents
# Supports: firm, attorney, client, matter
# ---------------------------------------------------------------------------

async def get_recent_documents(scope: dict) -> dict:
    """
    data_source for widget: dms_recent_documents
    Branches on matter_id → client_id → attorney_id → firm-wide.
    Returns: {"docs": [...], "matter_id": str|None, "total": int}
    """
    from core.db.base import AsyncSessionLocal

    tenant_id = scope.get("tenant_id", "")
    matter_id = scope.get("matter_id")
    client_id = scope.get("client_id")
    attorney_id = scope.get("attorney_id")
    limit = 10

    try:
        async with AsyncSessionLocal() as session:

            base_select = """
                SELECT
                    d.id::text,
                    d.title,
                    d.doc_type,
                    d.file_size,
                    d.updated_at,
                    d.storage_path,
                    m.matter_name,
                    c.client_name
                FROM documents d
                LEFT JOIN matters m
                    ON d.matter_id = m.id AND d.tenant_id = m.tenant_id
                LEFT JOIN clients c
                    ON m.client_id = c.id AND m.tenant_id = c.tenant_id
            """

            if matter_id:
                r = await session.execute(sa_text(
                    base_select +
                    "WHERE trim(d.tenant_id) = trim(:tid)"
                    "  AND d.matter_id = CAST(:mid AS uuid)"
                    " ORDER BY d.updated_at DESC LIMIT :lim"
                ), {"tid": tenant_id, "mid": matter_id, "lim": limit})

            elif client_id:
                r = await session.execute(sa_text(
                    base_select +
                    "WHERE trim(d.tenant_id) = trim(:tid)"
                    "  AND m.client_id = CAST(:cid AS uuid)"
                    " ORDER BY d.updated_at DESC LIMIT :lim"
                ), {"tid": tenant_id, "cid": client_id, "lim": limit})

            elif attorney_id:
                r = await session.execute(sa_text(
                    base_select +
                    "WHERE trim(d.tenant_id) = trim(:tid)"
                    "  AND m.responsible_attorney_id = :atty_id"
                    " ORDER BY d.updated_at DESC LIMIT :lim"
                ), {"tid": tenant_id, "atty_id": int(attorney_id), "lim": limit})

            else:
                r = await session.execute(sa_text(
                    base_select +
                    "WHERE trim(d.tenant_id) = trim(:tid)"
                    " ORDER BY d.updated_at DESC LIMIT :lim"
                ), {"tid": tenant_id, "lim": limit})

            rows = r.mappings().fetchall()

        docs = []
        for row in rows:
            docs.append({
                "id":           row["id"],
                "title":        row.get("title") or "Untitled",
                "doc_type":     (row.get("doc_type") or "").upper(),
                "size_str":     _fmt_size(row.get("file_size")),
                "updated_str":  _fmt_dt(row.get("updated_at")),
                "storage_path": row.get("storage_path") or "",
                "matter_name":  row.get("matter_name") or "",
                "client_name":  row.get("client_name") or "",
                "icon":         _ext_icon(
                    row.get("storage_path") or row.get("title") or ""
                ),
            })

        return {"docs": docs, "matter_id": matter_id, "total": len(docs)}

    except Exception as exc:
        logger.error("get_recent_documents error: %s", exc)
        return {"docs": [], "matter_id": matter_id, "total": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# dms_doc_count  (was: dms_matter_doc_count — renamed to reflect scope)
# Supports: matter, client, attorney
# ---------------------------------------------------------------------------

async def get_doc_count(scope: dict) -> dict:
    """
    data_source for widget: dms_doc_count
    Returns document and folder counts, scoped to matter/client/attorney.
    """
    from core.db.base import AsyncSessionLocal

    tenant_id = scope.get("tenant_id", "")
    matter_id = scope.get("matter_id")
    client_id = scope.get("client_id")
    attorney_id = scope.get("attorney_id")

    try:
        async with AsyncSessionLocal() as session:
            if matter_id:
                nc = await session.execute(sa_text("""
                    SELECT COUNT(*) FROM documents
                    WHERE matter_id = CAST(:mid AS uuid)
                      AND trim(tenant_id) = trim(:tid)
                """), {"mid": matter_id, "tid": tenant_id})
                native_count = nc.scalar() or 0

                fc = await session.execute(sa_text("""
                    SELECT COUNT(*) FROM matter_folders
                    WHERE matter_id = CAST(:mid AS uuid)
                      AND trim(tenant_id) = trim(:tid)
                """), {"mid": matter_id, "tid": tenant_id})
                folder_count = fc.scalar() or 0

                # Legacy dms_documents count for this matter
                lc = await session.execute(sa_text("""
                    SELECT COUNT(dd.id)
                    FROM dms_documents dd
                    JOIN dms_folder_matches mf
                        ON trim(mf.tenant_id) = trim(:tid)
                        AND dd.file_path LIKE mf.folder_path || '%'
                    WHERE mf.matter_id = CAST(:mid AS uuid)
                      AND trim(dd.tenant_id) = trim(:tid)
                """), {"mid": matter_id, "tid": tenant_id})
                legacy_count = lc.scalar() or 0

                label = None

            elif client_id:
                nc = await session.execute(sa_text("""
                    SELECT COUNT(d.id)
                    FROM documents d
                    JOIN matters m ON d.matter_id = m.id
                      AND d.tenant_id = m.tenant_id
                    WHERE trim(d.tenant_id) = trim(:tid)
                      AND m.client_id = CAST(:cid AS uuid)
                """), {"tid": tenant_id, "cid": client_id})
                native_count = nc.scalar() or 0
                folder_count = 0
                legacy_count = 0
                label = None

            elif attorney_id:
                nc = await session.execute(sa_text("""
                    SELECT COUNT(d.id)
                    FROM documents d
                    JOIN matters m ON d.matter_id = m.id
                      AND d.tenant_id = m.tenant_id
                    WHERE trim(d.tenant_id) = trim(:tid)
                      AND m.responsible_attorney_id = :atty_id
                """), {"tid": tenant_id, "atty_id": int(attorney_id)})
                native_count = nc.scalar() or 0
                folder_count = 0
                legacy_count = 0
                label = None

            else:
                return {"native_count": 0, "folder_count": 0,
                        "legacy_count": 0, "label": None}

        return {
            "native_count": native_count,
            "folder_count": folder_count,
            "legacy_count": legacy_count,
            "label": label,
            "matter_id": matter_id,
        }

    except Exception as exc:
        logger.error("get_doc_count error: %s", exc)
        return {
            "native_count": 0, "folder_count": 0,
            "legacy_count": 0, "label": None, "error": str(exc),
        }


# Keep old name as alias so existing widget_registry rows don't break
async def get_matter_doc_count(scope: dict) -> dict:
    return await get_doc_count(scope)


# ---------------------------------------------------------------------------
# dms_document_activity_feed
# Supports: firm, attorney, client, matter
# ---------------------------------------------------------------------------

async def get_document_activity_feed(scope: dict) -> dict:
    """
    data_source for widget: dms_document_activity_feed
    Recent file indexing activity from dms_documents.
    Branches on matter_id → client_id → attorney_id → firm-wide.
    """
    from core.db.base import AsyncSessionLocal

    tenant_id = scope.get("tenant_id", "")
    matter_id = scope.get("matter_id")
    client_id = scope.get("client_id")
    attorney_id = scope.get("attorney_id")
    limit = 12

    try:
        async with AsyncSessionLocal() as session:
            if matter_id:
                r = await session.execute(sa_text("""
                    SELECT
                        dd.file_path, dd.file_size_bytes, dd.indexed_at,
                        dd.ocr_status, dd.source,
                        m.id::text AS matter_id,
                        m.matter_name, c.client_name
                    FROM dms_documents dd
                    JOIN dms_folder_matches mf
                        ON trim(mf.tenant_id) = trim(:tid)
                        AND dd.file_path LIKE mf.folder_path || '%'
                    JOIN matters m ON mf.matter_id = m.id
                    LEFT JOIN clients c ON m.client_id = c.id
                    WHERE mf.matter_id = CAST(:mid AS uuid)
                      AND trim(dd.tenant_id) = trim(:tid)
                    ORDER BY dd.indexed_at DESC
                    LIMIT :lim
                """), {"tid": tenant_id, "mid": matter_id, "lim": limit})

            elif client_id:
                r = await session.execute(sa_text("""
                    SELECT
                        dd.file_path, dd.file_size_bytes, dd.indexed_at,
                        dd.ocr_status, dd.source,
                        m.id::text AS matter_id,
                        m.matter_name, c.client_name
                    FROM dms_documents dd
                    JOIN dms_folder_matches mf
                        ON trim(mf.tenant_id) = trim(:tid)
                        AND dd.file_path LIKE mf.folder_path || '%'
                    JOIN matters m ON mf.matter_id = m.id
                    LEFT JOIN clients c ON m.client_id = c.id
                    WHERE m.client_id = CAST(:cid AS uuid)
                      AND trim(dd.tenant_id) = trim(:tid)
                    ORDER BY dd.indexed_at DESC
                    LIMIT :lim
                """), {"tid": tenant_id, "cid": client_id, "lim": limit})

            elif attorney_id:
                r = await session.execute(sa_text("""
                    SELECT
                        dd.file_path, dd.file_size_bytes, dd.indexed_at,
                        dd.ocr_status, dd.source,
                        m.id::text AS matter_id,
                        m.matter_name, c.client_name
                    FROM dms_documents dd
                    JOIN dms_folder_matches mf
                        ON trim(mf.tenant_id) = trim(:tid)
                        AND dd.file_path LIKE mf.folder_path || '%'
                    JOIN matters m ON mf.matter_id = m.id
                    LEFT JOIN clients c ON m.client_id = c.id
                    WHERE m.responsible_attorney_id = :atty_id
                      AND trim(dd.tenant_id) = trim(:tid)
                    ORDER BY dd.indexed_at DESC
                    LIMIT :lim
                """), {"tid": tenant_id, "atty_id": int(attorney_id), "lim": limit})

            else:
                r = await session.execute(sa_text("""
                    SELECT
                        dd.file_path, dd.file_size_bytes, dd.indexed_at,
                        dd.ocr_status, dd.source,
                        m.id::text AS matter_id,
                        m.matter_name, c.client_name
                    FROM dms_documents dd
                    LEFT JOIN dms_folder_matches mf
                        ON trim(mf.tenant_id) = trim(:tid)
                        AND dd.file_path LIKE mf.folder_path || '%'
                    LEFT JOIN matters m ON mf.matter_id = m.id
                    LEFT JOIN clients c ON m.client_id = c.id
                    WHERE trim(dd.tenant_id) = trim(:tid)
                    ORDER BY dd.indexed_at DESC
                    LIMIT :lim
                """), {"tid": tenant_id, "lim": limit})

            rows = r.mappings().fetchall()

        items = []
        for row in rows:
            path = row.get("file_path") or ""
            filename = path.replace("\\", "/").split("/")[-1]
            items.append({
                "filename":       filename or path,
                "file_path":      path,
                "matter_id":      row.get("matter_id"),
                "matter_name":    row.get("matter_name") or "",
                "client_name":    row.get("client_name") or "",
                "indexed_at_str": _fmt_dt(row.get("indexed_at")),
                "ocr_status":     row.get("ocr_status") or "",
                "source":         row.get("source") or "",
                "icon":           _ext_icon(path),
            })

        return {"items": items, "matter_id": matter_id, "total": len(items)}

    except Exception as exc:
        logger.error("get_document_activity_feed error: %s", exc)
        return {"items": [], "matter_id": matter_id, "total": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# dms_folder_health
# Supports: matter (primary), firm-wide fallback
# ---------------------------------------------------------------------------

async def get_folder_health(scope: dict) -> dict:
    """
    data_source for widget: dms_folder_health
    OCR and extraction coverage for all indexed files in this matter.
    """
    from core.db.base import AsyncSessionLocal

    tenant_id = scope.get("tenant_id", "")
    matter_id = scope.get("matter_id")

    try:
        async with AsyncSessionLocal() as session:
            if matter_id:
                # Folders linked to this matter
                folders_r = await session.execute(sa_text("""
                    SELECT
                        mf.folder_path,
                        mf.disk_root,
                        COUNT(dd.id)                                        AS file_count,
                        COUNT(dd.id) FILTER (WHERE dd.ocr_status = 'done')  AS ocr_done
                    FROM dms_folder_matches mf
                    LEFT JOIN dms_documents dd
                        ON trim(dd.tenant_id) = trim(:tid)
                        AND dd.file_path LIKE mf.folder_path || '%'
                    WHERE mf.matter_id = CAST(:mid AS uuid)
                      AND trim(mf.tenant_id) = trim(:tid)
                    GROUP BY mf.folder_path, mf.disk_root
                    ORDER BY mf.folder_path
                """), {"mid": matter_id, "tid": tenant_id})

                stats_r = await session.execute(sa_text("""
                    SELECT
                        COUNT(dd.id)                                            AS total_files,
                        COUNT(dd.id) FILTER (WHERE dd.ocr_status = 'done')      AS ocr_done,
                        COUNT(dd.id) FILTER (WHERE dd.ocr_status = 'pending')   AS ocr_pending,
                        COUNT(dd.id) FILTER (WHERE dd.ocr_status = 'failed')    AS ocr_failed
                    FROM dms_documents dd
                    JOIN dms_folder_matches mf
                        ON trim(mf.tenant_id) = trim(:tid)
                        AND dd.file_path LIKE mf.folder_path || '%'
                    WHERE mf.matter_id = CAST(:mid AS uuid)
                      AND trim(dd.tenant_id) = trim(:tid)
                """), {"mid": matter_id, "tid": tenant_id})

            else:
                folders_r = await session.execute(sa_text("""
                    SELECT '' AS folder_path, NULL AS disk_root,
                           COUNT(*) AS file_count,
                           COUNT(*) FILTER (WHERE ocr_status = 'done') AS ocr_done
                    FROM dms_documents
                    WHERE trim(tenant_id) = trim(:tid)
                """), {"tid": tenant_id})

                stats_r = await session.execute(sa_text("""
                    SELECT
                        COUNT(*)                                            AS total_files,
                        COUNT(*) FILTER (WHERE ocr_status = 'done')         AS ocr_done,
                        COUNT(*) FILTER (WHERE ocr_status = 'pending')      AS ocr_pending,
                        COUNT(*) FILTER (WHERE ocr_status = 'failed')       AS ocr_failed
                    FROM dms_documents
                    WHERE trim(tenant_id) = trim(:tid)
                """), {"tid": tenant_id})

            folder_rows = folders_r.mappings().fetchall()
            stats = stats_r.mappings().fetchone()

        total_files  = int(stats["total_files"]  or 0)
        ocr_done     = int(stats["ocr_done"]     or 0)
        ocr_pending  = int(stats["ocr_pending"]  or 0)
        ocr_failed   = int(stats["ocr_failed"]   or 0)
        ocr_pct      = int((ocr_done / total_files * 100) if total_files else 0)

        folders = []
        for f in folder_rows:
            label = (f.get("folder_path") or "").replace("\\", "/").rstrip("/")
            label = label.split("/")[-1] if "/" in label else label
            label = label or "Root"
            folders.append({
                "folder_label": label,
                "file_count":   int(f.get("file_count") or 0),
                "ocr_done":     int(f.get("ocr_done") or 0),
                "disk_root":    f.get("disk_root"),
            })

        return {
            "total_files": total_files,
            "ocr_done":    ocr_done,
            "ocr_pending": ocr_pending,
            "ocr_failed":  ocr_failed,
            "ocr_pct":     ocr_pct,
            "folders":     folders,
            "matter_id":   matter_id,
        }

    except Exception as exc:
        logger.error("get_folder_health error: %s", exc)
        return {
            "total_files": 0, "ocr_done": 0, "ocr_pending": 0,
            "ocr_failed": 0, "ocr_pct": 0, "folders": [],
            "matter_id": matter_id, "error": str(exc),
        }


# ---------------------------------------------------------------------------
# dms_storage_stats  (lifts the inline query from dms_home)
# Supports: firm
# ---------------------------------------------------------------------------

async def get_storage_stats(scope: dict) -> dict:
    """
    data_source for widget: dms_storage_stats
    Firm-wide document corpus stats — replaces inline query in dms_home.
    """
    from core.db.base import AsyncSessionLocal

    tenant_id = scope.get("tenant_id", "")

    try:
        async with AsyncSessionLocal() as session:
            stats_r = await session.execute(sa_text("""
                SELECT
                    COUNT(*)                                        AS total_indexed,
                    COALESCE(SUM(file_size_bytes), 0)              AS total_bytes,
                    COUNT(*) FILTER (WHERE ocr_status = 'pending') AS ocr_pending,
                    COUNT(*) FILTER (WHERE ocr_status = 'done')    AS ocr_done,
                    COUNT(*) FILTER (
                        WHERE indexed_at >= NOW() - INTERVAL '7 days'
                    )                                              AS indexed_this_week
                FROM dms_documents
                WHERE trim(tenant_id) = trim(:tid)
            """), {"tid": tenant_id})
            sr = stats_r.mappings().fetchone()

            ocr_q = await session.execute(sa_text("""
                SELECT COUNT(*) FROM dms_ocr_queue
                WHERE trim(tenant_id) = trim(:tid)
                  AND status IN ('pending', 'processing')
            """), {"tid": tenant_id})
            ocr_queued = int(ocr_q.scalar() or 0)

            matters_r = await session.execute(sa_text("""
                SELECT
                    COUNT(DISTINCT m.id)     AS total_matters,
                    COUNT(DISTINCT m.client_id) AS total_clients
                FROM matters m
                WHERE trim(m.tenant_id) = trim(:tid)
                  AND m.status = 'active'
            """), {"tid": tenant_id})
            mr = matters_r.mappings().fetchone()

        return {
            "total_indexed":     int(sr["total_indexed"]     or 0),
            "total_bytes":       int(sr["total_bytes"]       or 0),
            "total_bytes_fmt":   _fmt_size(int(sr["total_bytes"] or 0)),
            "ocr_pending":       int(sr["ocr_pending"]       or 0),
            "ocr_done":          int(sr["ocr_done"]          or 0),
            "indexed_this_week": int(sr["indexed_this_week"] or 0),
            "ocr_queued":        ocr_queued,
            "total_matters":     int(mr["total_matters"]     or 0),
            "total_clients":     int(mr["total_clients"]     or 0),
        }

    except Exception as exc:
        logger.error("get_storage_stats error: %s", exc)
        return {
            "total_indexed": 0, "total_bytes": 0, "total_bytes_fmt": "0 B",
            "ocr_pending": 0, "ocr_done": 0, "indexed_this_week": 0,
            "ocr_queued": 0, "total_matters": 0, "total_clients": 0,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# get_firm_matter_tree  — DEPRECATED
# Kept as a thin redirect to dms_recent_documents for backward compat.
# Remove after widget_registry is updated to point to
# modules.dashboard.services.pi_widget_service.get_firm_matter_tree_pi
# ---------------------------------------------------------------------------

async def get_firm_matter_tree(scope: dict) -> dict:
    """
    DEPRECATED — use modules.dashboard.services.pi_widget_service.get_firm_matter_tree_pi
    This stub prevents import errors during the transition.
    """
    logger.warning(
        "get_firm_matter_tree called from dms.widget_service — "
        "update widget_registry data_source to pi_widget_service"
    )
    from modules.dashboard.services.pi_widget_service import get_firm_matter_tree_pi
    return await get_firm_matter_tree_pi(scope)


# ---------------------------------------------------------------------------
# DMS Capture Widgets (dms_home 3x2 grid)
# ---------------------------------------------------------------------------

async def get_scan_drop_zone(scope: dict) -> dict:
    """
    dms_scan_drop_zone widget — flat matters list for assignment dropdown.
    The actual file POST goes to /scan/upload (existing scanning_portal route).
    """
    from core.db.base import AsyncSessionLocal
    tenant_id = scope["tenant_id"]

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.id, m.matter_name, m.matter_number, c.client_name
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id
                AND trim(m.tenant_id) = trim(c.tenant_id)
            WHERE trim(m.tenant_id) = :tid AND m.status = 'active'
            ORDER BY c.client_name ASC, m.matter_name ASC
        """), {"tid": tenant_id})
        matters = [dict(row) for row in r.mappings().fetchall()]

    return {"matters": matters}


async def get_dictation_drop(scope: dict) -> dict:
    """
    dms_dictation_drop widget — flat matters list for assignment dropdown.
    The actual file POST goes to /scan/dictation/upload (existing route).
    """
    from core.db.base import AsyncSessionLocal
    tenant_id = scope["tenant_id"]

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.id, m.matter_name, m.matter_number, c.client_name
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id
                AND trim(m.tenant_id) = trim(c.tenant_id)
            WHERE trim(m.tenant_id) = :tid AND m.status = 'active'
            ORDER BY c.client_name ASC, m.matter_name ASC
        """), {"tid": tenant_id})
        matters = [dict(row) for row in r.mappings().fetchall()]

    return {"matters": matters}


async def get_scan_queue(scope: dict) -> dict:
    """
    dms_scan_queue widget — non-completed scan_queue rows with matter name.
    AI intervention point (future): matter suggestion on ocr_complete rows
    where matter_id IS NULL.
    """
    from core.db.base import AsyncSessionLocal
    tenant_id = scope["tenant_id"]

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT sq.*, m.matter_name
            FROM scan_queue sq
            LEFT JOIN matters m ON sq.matter_id = m.id::text
                AND trim(sq.tenant_id) = trim(m.tenant_id)
            WHERE trim(sq.tenant_id) = :tid AND sq.status != 'completed'
            ORDER BY sq.created_at DESC LIMIT 50
        """), {"tid": tenant_id})
        scan_queue = [dict(row) for row in r.mappings().fetchall()]

        mr = await session.execute(sa_text("""
            SELECT m.id, m.matter_name, m.matter_number, c.client_name
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id
                AND trim(m.tenant_id) = trim(c.tenant_id)
            WHERE trim(m.tenant_id) = :tid AND m.status = 'active'
            ORDER BY c.client_name ASC, m.matter_name ASC
        """), {"tid": tenant_id})
        matters = [dict(row) for row in mr.mappings().fetchall()]

    return {"scan_queue": scan_queue, "matters": matters}


async def get_dictation_queue(scope: dict) -> dict:
    """
    dms_dictation_queue widget — non-completed dictation_queue rows.
    AI intervention point (future): auto-transcription status, matter suggestion.
    """
    from core.db.base import AsyncSessionLocal
    tenant_id = scope["tenant_id"]

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT dq.*, m.matter_name, u.full_name as user_name
            FROM dictation_queue dq
            LEFT JOIN matters m ON dq.matter_id = m.id::text
                AND trim(dq.tenant_id) = trim(m.tenant_id)
            LEFT JOIN users u ON dq.user_id = u.id
                AND trim(dq.tenant_id) = trim(u.tenant_id)
            WHERE trim(dq.tenant_id) = :tid AND dq.status != 'completed'
            ORDER BY dq.created_at DESC LIMIT 50
        """), {"tid": tenant_id})
        dictation_queue = [dict(row) for row in r.mappings().fetchall()]

    return {"dictation_queue": dictation_queue}
