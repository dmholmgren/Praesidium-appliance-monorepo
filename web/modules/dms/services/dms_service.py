# modules/dms/services/dms_service.py
"""
DMS Widget Service — matter intelligence panel data sources.
Registered in widget_registry.data_source as:
    modules.dms.services.dms_service.<function_name>

Each function receives a scope dict:
    scope = {
        "tenant_id":   str,
        "user_id":     int | None,
        "matter_id":   str | None,
        "attorney_id": str | None,
        "date_from":   str | None,
        "date_to":     str | None,
        "request":     Request,
    }
And returns a dict of template context variables.

Columns confirmed on documents table:
    id, tenant_id, matter_id, file_name, title, doc_type, document_type,
    file_size, storage_path, status, created_at, updated_at, created_by,
    page_count, checksum, version_number
No checked_out_by / checked_out_at columns — checkout feature not yet built.
"""

import logging
from datetime import datetime

from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)


def _fmt_size(b):
    if b is None:
        return "—"
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b / (1024 * 1024):.1f} MB"


def _fmt_dt(dt):
    if dt is None:
        return "—"
    if isinstance(dt, datetime):
        return dt.strftime("%b %d, %Y")
    return str(dt)[:10]


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


# ---------------------------------------------------------------------------
# dms_matter_document_count
# widget_slug: dms_matter_document_count
# Shows native document count, folder count, and matter name.
# ---------------------------------------------------------------------------

async def get_matter_document_count(scope: dict) -> dict:
    """
    Returns {"native_count": int, "folder_count": int, "matter_name": str,
             "matter_id": str|None, "total_size_str": str}
    """
    from core.db.base import AsyncSessionLocal

    tenant_id = (scope.get("tenant_id") or "").strip()
    matter_id = scope.get("matter_id")

    if not matter_id:
        return {
            "native_count": 0, "folder_count": 0,
            "matter_name": "", "matter_id": None,
            "total_size_str": "—",
        }

    try:
        async with AsyncSessionLocal() as session:
            # Document count + total size
            dc = await session.execute(sa_text("""
                SELECT COUNT(*) as cnt, COALESCE(SUM(file_size), 0) as total_size
                FROM documents
                WHERE matter_id = CAST(:mid AS uuid)
                  AND TRIM(tenant_id) = TRIM(:tid)
            """), {"mid": matter_id, "tid": tenant_id})
            dc_row = dc.mappings().fetchone()
            native_count = dc_row["cnt"] or 0
            total_size = dc_row["total_size"] or 0

            # Folder count
            fc = await session.execute(sa_text("""
                SELECT COUNT(*) FROM matter_folders
                WHERE matter_id = CAST(:mid AS uuid)
                  AND TRIM(tenant_id) = TRIM(:tid)
            """), {"mid": matter_id, "tid": tenant_id})
            folder_count = fc.scalar() or 0

            # Matter name
            mr = await session.execute(sa_text("""
                SELECT matter_name FROM matters
                WHERE id = CAST(:mid AS uuid)
                  AND TRIM(tenant_id) = TRIM(:tid)
            """), {"mid": matter_id, "tid": tenant_id})
            matter_row = mr.fetchone()
            matter_name = matter_row[0] if matter_row else ""

        return {
            "native_count": native_count,
            "folder_count": folder_count,
            "matter_name": matter_name,
            "matter_id": matter_id,
            "total_size_str": _fmt_size(total_size),
        }

    except Exception as exc:
        logger.error("get_matter_document_count error: %s", exc)
        return {
            "native_count": 0, "folder_count": 0,
            "matter_name": "", "matter_id": matter_id,
            "total_size_str": "—", "error": str(exc),
        }


# ---------------------------------------------------------------------------
# dms_document_activity_feed
# widget_slug: dms_document_activity_feed
# Shows the 10 most recently modified documents for this matter.
# ---------------------------------------------------------------------------

async def get_document_activity(scope: dict) -> dict:
    """
    Returns {"activity": [...], "matter_id": str|None}
    Each activity entry: {file_name, doc_type, action, updated_str, icon, id}
    """
    from core.db.base import AsyncSessionLocal

    tenant_id = (scope.get("tenant_id") or "").strip()
    matter_id = scope.get("matter_id")
    limit = 10

    try:
        async with AsyncSessionLocal() as session:
            if matter_id:
                r = await session.execute(sa_text("""
                    SELECT
                        id::text,
                        COALESCE(file_name, title, 'Untitled') as file_name,
                        doc_type,
                        status,
                        created_at,
                        updated_at,
                        storage_path
                    FROM documents
                    WHERE matter_id = CAST(:mid AS uuid)
                      AND TRIM(tenant_id) = TRIM(:tid)
                    ORDER BY updated_at DESC NULLS LAST
                    LIMIT :lim
                """), {"mid": matter_id, "tid": tenant_id, "lim": limit})
            else:
                r = await session.execute(sa_text("""
                    SELECT
                        id::text,
                        COALESCE(file_name, title, 'Untitled') as file_name,
                        doc_type,
                        status,
                        created_at,
                        updated_at,
                        storage_path
                    FROM documents
                    WHERE TRIM(tenant_id) = TRIM(:tid)
                    ORDER BY updated_at DESC NULLS LAST
                    LIMIT :lim
                """), {"tid": tenant_id, "lim": limit})

            rows = r.mappings().fetchall()

        activity = []
        for row in rows:
            updated = row.get("updated_at")
            created = row.get("created_at")
            # Infer action from timestamps
            if updated and created and abs(
                (updated if isinstance(updated, datetime) else datetime.fromisoformat(str(updated))).timestamp() -
                (created if isinstance(created, datetime) else datetime.fromisoformat(str(created))).timestamp()
            ) < 5:
                action = "Added"
            else:
                action = "Modified"

            activity.append({
                "id": row["id"],
                "file_name": row["file_name"] or "Untitled",
                "doc_type": (row.get("doc_type") or "").upper(),
                "action": action,
                "updated_str": _fmt_dt(updated),
                "icon": _ext_icon(row.get("storage_path") or row.get("file_name") or ""),
            })

        return {"activity": activity, "matter_id": matter_id}

    except Exception as exc:
        logger.error("get_document_activity error: %s", exc)
        return {"activity": [], "matter_id": matter_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# dms_checked_out_documents
# widget_slug: dms_checked_out_documents
# Checkout feature not yet built — returns graceful empty state.
# Activate when checked_out_by / checked_out_at columns are added to documents.
# ---------------------------------------------------------------------------

async def get_checked_out_documents(scope: dict) -> dict:
    """
    Returns {"checked_out": [], "matter_id": str|None, "feature_pending": True}
    Checkout columns (checked_out_by, checked_out_at) not yet on documents table.
    """
    return {
        "checked_out": [],
        "matter_id": scope.get("matter_id"),
        "feature_pending": True,
    }


# ---------------------------------------------------------------------------
# dms_folder_health
# widget_slug: dms_folder_health
# Shows folder list with file counts for this matter.
# ---------------------------------------------------------------------------

async def get_folder_health(scope: dict) -> dict:
    """
    Returns {"folders": [...], "matter_id": str|None, "total_folders": int}
    Each folder: {folder_path, disk_root, file_count, root_label, last_activity_str}
    """
    from core.db.base import AsyncSessionLocal

    tenant_id = (scope.get("tenant_id") or "").strip()
    matter_id = scope.get("matter_id")

    if not matter_id:
        return {"folders": [], "matter_id": None, "total_folders": 0}

    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT
                    id::text,
                    folder_path,
                    disk_root,
                    COALESCE(file_count, 0) as file_count
                FROM matter_folders
                WHERE matter_id = CAST(:mid AS uuid)
                  AND TRIM(tenant_id) = TRIM(:tid)
                ORDER BY disk_root NULLS LAST, folder_path
                LIMIT 20
            """), {"mid": matter_id, "tid": tenant_id})
            rows = r.mappings().fetchall()

        folders = []
        for row in rows:
            disk_root = (row.get("disk_root") or "").lower()
            if "docsend" in disk_root:
                root_label = "Docsend"
            elif disk_root:
                root_label = "Clients"
            else:
                root_label = "Praesidium"

            folder_path = row.get("folder_path") or ""
            short_name = folder_path.split("/")[-1] if "/" in folder_path else folder_path

            folders.append({
                "id": row["id"],
                "folder_path": folder_path,
                "short_name": short_name or folder_path,
                "disk_root": row.get("disk_root") or "",
                "file_count": row["file_count"],
                "root_label": root_label,
            })

        return {
            "folders": folders,
            "matter_id": matter_id,
            "total_folders": len(folders),
        }

    except Exception as exc:
        logger.error("get_folder_health error: %s", exc)
        return {"folders": [], "matter_id": matter_id, "total_folders": 0, "error": str(exc)}
