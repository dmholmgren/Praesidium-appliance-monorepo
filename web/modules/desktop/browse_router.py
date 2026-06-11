"""
M-DESK C2 — Browse router for the desktop client.

Read-only endpoints that let the VSTO Word/Excel/Outlook add-ins
browse matters, folders, documents, and version chains. All routes
are JWT-authenticated via require_desktop_user.

Endpoints:

  GET  /api/v1/desktop/profile         — current user info
  GET  /api/v1/desktop/matters         — search matters (grouped by client)
  GET  /api/v1/desktop/matters/{id}/folders   — folder tree for a matter
  GET  /api/v1/desktop/matters/{id}/documents — document list (filterable)
  GET  /api/v1/desktop/documents/{id}/versions — version chain via parent_doc_id
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi import Path as PathParam
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.desktop import jwt_service
from modules.desktop.checkout_router import require_desktop_user

logger = logging.getLogger(__name__)



# ═════════════════════════════════════════════════════════════════════════
# Disk tree walker — matches DMS web UI pattern
# ═════════════════════════════════════════════════════════════════════════

def _walk_disk_tree(root_path: str, max_depth: int = 4) -> list:
    """Walk a directory and return nested folder structure.
    Skips dotfiles. Returns [{name, path, children, file_count}]."""
    result = []
    if not os.path.isdir(root_path):
        return result
    try:
        entries = sorted(os.scandir(root_path), key=lambda e: e.name)
    except PermissionError:
        return result
    for entry in entries:
        if entry.name.startswith('.'):
            continue
        if entry.is_dir(follow_symlinks=False):
            children = _walk_disk_tree(entry.path, max_depth - 1) if max_depth > 1 else []
            try:
                file_count = sum(1 for f in os.scandir(entry.path)
                                 if f.is_file() and not f.name.startswith('.'))
            except (PermissionError, OSError):
                file_count = 0
            result.append({
                "name": entry.name,
                "path": entry.path,
                "children": children,
                "file_count": file_count,
            })
    return result


def _flatten_disk_tree(tree: list, parent_label: str = "") -> list:
    """Recursively flatten nested disk tree into flat list for VSTO client.
    Returns [{id, folder_path, disk_root, file_count}] matching FoldersResponse."""
    flat = []
    for node in tree:
        label = f"{parent_label}/{node['name']}" if parent_label else node["name"]
        flat.append({
            "id": node["path"],
            "folder_path": label,
            "disk_root": node["path"],
            "file_count": node["file_count"],
        })
        if node.get("children"):
            flat.extend(_flatten_disk_tree(node["children"], label))
    return flat


router = APIRouter(prefix="/api/v1/desktop", tags=["m-desk-browse"])


# ═════════════════════════════════════════════════════════════════════════
# JSON helpers
# ═════════════════════════════════════════════════════════════════════════

def _jsonable(obj: Any) -> Any:
    """Coerce types that json.dumps can't handle."""
    if obj is None:
        return None
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return obj


def _row_dict(row) -> dict:
    """Convert a SQLAlchemy RowMapping to a JSON-safe dict."""
    return {k: _jsonable(v) for k, v in dict(row).items()}


# ═════════════════════════════════════════════════════════════════════════
# GET /profile — current user
# ═════════════════════════════════════════════════════════════════════════

@router.get("/profile")
async def get_profile(
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Return the current user's profile for the tray/ribbon UI."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa_text("""
                SELECT
                  id::text      AS id,
                  username,
                  email,
                  full_name,
                  role::text    AS role,
                  is_active
                FROM users
                WHERE id = :uid
                  AND TRIM(tenant_id) = :tid
                LIMIT 1
            """),
            {"uid": claims.user_id, "tid": claims.tenant_id},
        )
        row = result.mappings().first()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "user_not_found"},
            )
    return _row_dict(row)


# ═════════════════════════════════════════════════════════════════════════
# GET /matters — search matters grouped by client
# ═════════════════════════════════════════════════════════════════════════

@router.get("/matters")
async def list_matters(
    q: str = Query("", description="Search term (name, number, or client)"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Return matters for the tenant, optionally filtered by search term.

    Results are grouped by client name for the VSTO matter-picker tree.
    """
    search = q.strip()
    if search:
        where = """
            AND (
                m.matter_name ILIKE :search
                OR m.matter_number ILIKE :search
                OR c.client_name ILIKE :search
            )
        """
        params = {
            "tid": claims.tenant_id,
            "search": f"%{search}%",
            "limit": limit,
            "offset": offset,
        }
    else:
        where = ""
        params = {
            "tid": claims.tenant_id,
            "limit": limit,
            "offset": offset,
        }

    sql = f"""
        SELECT
          m.id::text          AS id,
          m.matter_name       AS matter_name,
          m.matter_number,
          m.status,
          m.practice_area,
          m.open_date,
          c.id::text          AS client_id,
          c.client_name       AS client_name
        FROM matters m
        LEFT JOIN clients c ON c.id = m.client_id
                           AND TRIM(c.tenant_id) = :tid
        WHERE TRIM(m.tenant_id) = :tid
          AND COALESCE(m.status, 'active') != 'closed'
          {where}
        ORDER BY c.client_name, m.matter_name
        LIMIT :limit OFFSET :offset
    """

    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text(sql), params)
        rows = [_row_dict(r) for r in result.mappings().all()]

    # Group by client for the tree view
    clients: dict[str, dict] = {}
    for row in rows:
        cid = row.get("client_id") or "unassigned"
        cname = row.get("client_name") or "(No Client)"
        if cid not in clients:
            clients[cid] = {
                "client_id": cid,
                "client_name": cname,
                "matters": [],
            }
        clients[cid]["matters"].append({
            "id": row["id"],
            "name": row["matter_name"],
            "matter_number": row["matter_number"],
            "status": row["status"],
            "practice_area": row["practice_area"],
            "open_date": row["open_date"],
        })

    return {
        "clients": list(clients.values()),
        "total_matters": len(rows),
        "limit": limit,
        "offset": offset,
    }


# ═════════════════════════════════════════════════════════════════════════
# GET /matters/{id}/folders — folder tree for a matter
# ═════════════════════════════════════════════════════════════════════════

@router.get("/matters/{matter_id}/folders")
async def list_matter_folders(
    matter_id: str = PathParam(..., description="Matter UUID"),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Return the folder tree for a matter.
    
    Strategy:
    1. Check matter_folders for a Praesidium disk_root
    2. If found and directory exists on disk, walk the actual filesystem
    3. Otherwise fall back to matter_folders DB records
    
    This matches the DMS web UI behavior (disk-walking for Praesidium matters).
    """
    async with AsyncSessionLocal() as session:
        # Verify matter belongs to tenant
        matter_check = await session.execute(
            sa_text("""
                SELECT id, folder_path FROM matters
                WHERE id = CAST(:mid AS uuid)
                  AND TRIM(tenant_id) = :tid
                LIMIT 1
            """),
            {"mid": matter_id, "tid": claims.tenant_id},
        )
        matter_row = matter_check.mappings().first()
        if not matter_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "matter_not_found", "matter_id": matter_id},
            )

        # Get matter_folders rows
        result = await session.execute(
            sa_text("""
                SELECT
                  id::text        AS id,
                  matter_id::text AS matter_id,
                  folder_path,
                  disk_root,
                  file_count,
                  added_at
                FROM matter_folders
                WHERE matter_id = CAST(:mid AS uuid)
                  AND TRIM(tenant_id) = :tid
                  AND migrated_to_folder_id IS NULL
                ORDER BY folder_path
            """),
            {"mid": matter_id, "tid": claims.tenant_id},
        )
        db_rows = [_row_dict(r) for r in result.mappings().all()]

    # Check for Praesidium disk_root — walk filesystem if it exists
    praesidium_root = None
    for row in db_rows:
        dr = row.get("disk_root") or ""
        if dr.startswith("/mnt/praesidium") and os.path.isdir(dr):
            praesidium_root = dr
            break

    if praesidium_root:
        # Walk actual disk — same as DMS web UI
        disk_tree = _walk_disk_tree(praesidium_root, max_depth=3)
        folders = _flatten_disk_tree(disk_tree)
        
        # Add root-level entry for the matter root itself
        try:
            root_file_count = sum(1 for f in os.scandir(praesidium_root)
                                  if f.is_file() and not f.name.startswith('.'))
        except (PermissionError, OSError):
            root_file_count = 0
        
        if root_file_count > 0:
            folders.insert(0, {
                "id": praesidium_root,
                "folder_path": "(root)",
                "disk_root": praesidium_root,
                "file_count": root_file_count,
            })
        
        return {"matter_id": matter_id, "folders": folders}
    else:
        # Fallback: return DB records
        return {"matter_id": matter_id, "folders": db_rows}


@router.get("/matters/{matter_id}/documents")
async def list_matter_documents(
    matter_id: str = PathParam(..., description="Matter UUID"),
    q: str = Query("", description="Search filename or title"),
    folder_id: Optional[str] = Query(None, description="Filter by folder path or UUID"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Return documents for a matter from both documents and dms_documents tables.
    
    - documents table: manually uploaded files (DMS native, versioned)
    - dms_documents table: synced files from legacy shares (115K+ files)
    
    When folder_id is an absolute disk path (from disk-walked tree), filters
    dms_documents by file_path prefix. When folder_id is a UUID, filters
    documents by matter_folders disk_root.
    """
    params: dict[str, Any] = {
        "tid": claims.tenant_id,
        "mid": matter_id,
        "limit": limit,
        "offset": offset,
    }

    search = q.strip()

    # ── Query 1: dms_documents (synced files — bulk of content) ──
    dms_folder_filter = ""
    dms_search_filter = ""
    
    if folder_id and folder_id.startswith("/"):
        # Disk path from disk-walked tree
        dms_folder_filter = "AND dd.file_path LIKE :folder_prefix"
        params["folder_prefix"] = folder_id.rstrip("/") + "/%"
    
    if search:
        dms_search_filter = "AND dd.file_path ILIKE :dms_search"
        params["dms_search"] = f"%{search}%"

    # We need to join dms_documents to matter_folders to find which matter
    # owns which files. dms_documents.folder_root matches matter_folders.disk_root
    dms_sql = f"""
        SELECT
          dd.id::text           AS id,
          REPLACE(dd.file_path, dd.folder_root || '/', '') AS filename,
          dd.file_path          AS original_filename,
          NULL                  AS title,
          NULL                  AS mime_type,
          dd.file_size_bytes    AS file_size,
          NULL                  AS document_type,
          NULL                  AS doc_type,
          1                     AS version_number,
          NULL                  AS parent_doc_id,
          'synced'              AS status,
          dd.indexed_at         AS created_at,
          dd.updated_at,
          dd.file_path          AS storage_path,
          NULL                  AS checked_out_by,
          NULL                  AS checked_out_at,
          'dms_documents'       AS _source
        FROM dms_documents dd
        JOIN matter_folders mf
          ON dd.folder_root LIKE mf.disk_root || '%'
          AND TRIM(mf.tenant_id) = :tid
        WHERE mf.matter_id = CAST(:mid AS uuid)
          AND TRIM(dd.tenant_id) = :tid
          {dms_folder_filter}
          {dms_search_filter}
    """

    # ── Query 2: documents table (native uploads, versioned) ──
    doc_folder_filter = ""
    doc_search_filter = ""
    
    if folder_id and folder_id.startswith("/"):
        # Match storage_path against the disk path
        rel_path = folder_id.replace("/mnt/", "", 1) if folder_id.startswith("/mnt/") else folder_id
        doc_folder_filter = "AND (d.storage_path LIKE :doc_folder_rel OR d.storage_path LIKE :doc_folder_abs)"
        params["doc_folder_rel"] = rel_path.rstrip("/") + "/%"
        params["doc_folder_abs"] = folder_id.rstrip("/") + "/%"
    elif folder_id:
        # UUID-based folder filter (original behavior)
        doc_folder_filter = """
            AND d.storage_path LIKE (
                SELECT disk_root || '%'
                FROM matter_folders
                WHERE id = CAST(:fid AS uuid)
                  AND TRIM(tenant_id) = :tid
                LIMIT 1
            )
        """
        params["fid"] = folder_id

    if search:
        doc_search_filter = """
            AND (
                d.filename ILIKE :search
                OR d.title ILIKE :search
                OR d.original_filename ILIKE :search
            )
        """
        params["search"] = f"%{search}%"

    doc_sql = f"""
        SELECT
          d.id::text            AS id,
          d.filename,
          d.original_filename,
          d.title,
          d.mime_type,
          d.file_size,
          d.document_type,
          d.doc_type,
          d.version_number,
          d.parent_doc_id::text AS parent_doc_id,
          d.status,
          d.created_at,
          d.updated_at,
          d.storage_path,
          d.metadata->>'checked_out_by' AS checked_out_by,
          d.metadata->>'checked_out_at' AS checked_out_at,
          'documents'           AS _source
        FROM documents d
        WHERE d.matter_id = CAST(:mid AS uuid)
          AND TRIM(d.tenant_id) = :tid
          AND COALESCE(d.status, 'active') != 'deleted'
          AND NOT EXISTS (
              SELECT 1 FROM documents child
              WHERE child.parent_doc_id = d.id
                AND TRIM(child.tenant_id) = :tid
          )
          {doc_folder_filter}
          {doc_search_filter}
    """

    # UNION both, sort, limit
    combined_sql = f"""
        ({dms_sql})
        UNION ALL
        ({doc_sql})
        ORDER BY filename
        LIMIT :limit OFFSET :offset
    """

    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text(combined_sql), params)
        rows = [_row_dict(r) for r in result.mappings().all()]

    # Strip _source from output (internal only)
    for row in rows:
        row.pop("_source", None)

    # ── Fallback: disk-walk for Praesidium-native files not in DB ──
    # If folder_id is an absolute Praesidium path and DB returned nothing,
    # scan the directory and return files directly. This covers files
    # uploaded via web UI, rsync, or external tools that aren't yet in
    # documents or dms_documents tables.
    if not rows and folder_id and folder_id.startswith("/mnt/praesidium"):
        disk_rows = _scan_disk_files(folder_id, search, limit)
        if disk_rows:
            return {
                "matter_id": matter_id,
                "documents": disk_rows,
                "total": len(disk_rows),
                "limit": limit,
                "offset": offset,
            }

    # Also: if we DO have DB rows but folder_id is a Praesidium path,
    # merge in any disk files that aren't already represented in DB results.
    # This handles the mixed case where some files are in DB and some aren't.
    if folder_id and folder_id.startswith("/mnt/praesidium"):
        db_paths = set()
        for row in rows:
            sp = row.get("storage_path") or row.get("original_filename") or ""
            if sp:
                db_paths.add(os.path.basename(sp))
        disk_rows = _scan_disk_files(folder_id, search, limit)
        for dr in disk_rows:
            if dr["filename"] not in db_paths:
                rows.append(dr)
        rows.sort(key=lambda r: (r.get("filename") or "").lower())
        rows = rows[:limit]

    return {
        "matter_id": matter_id,
        "documents": rows,
        "total": len(rows),
        "limit": limit,
        "offset": offset,
    }



# ═════════════════════════════════════════════════════════════════════════
# Disk-file helpers for Praesidium-native folders
# ═════════════════════════════════════════════════════════════════════════

MIME_MAP = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".msg": "application/vnd.ms-outlook",
    ".eml": "message/rfc822",
    ".zip": "application/zip",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _scan_disk_files(folder_path: str, search: str = "", limit: int = 200) -> list:
    """Walk a directory and return files as virtual document rows.
    These files exist on disk but not in any DB table."""
    if not os.path.isdir(folder_path):
        return []
    results = []
    try:
        for entry in sorted(os.scandir(folder_path), key=lambda e: e.name.lower()):
            if entry.is_file() and not entry.name.startswith("."):
                if search and search.lower() not in entry.name.lower():
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                stat = entry.stat()
                results.append({
                    "id": entry.path,  # Use absolute path as ID for disk files
                    "filename": entry.name,
                    "original_filename": entry.path,
                    "title": None,
                    "mime_type": MIME_MAP.get(ext, "application/octet-stream"),
                    "file_size": stat.st_size,
                    "document_type": None,
                    "doc_type": None,
                    "version_number": 1,
                    "parent_doc_id": None,
                    "status": "disk",
                    "created_at": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc).isoformat(),
                    "updated_at": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc).isoformat(),
                    "storage_path": entry.path,
                    "checked_out_by": None,
                    "checked_out_at": None,
                })
                if len(results) >= limit:
                    break
    except (PermissionError, OSError):
        pass
    return results


@router.get("/open-path")
async def open_file_by_path(
    path: str = Query(..., description="Absolute file path on disk"),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Serve file bytes for a disk file identified by absolute path.
    
    Used by the VSTO client to open files that exist on disk but aren't
    in the documents or dms_documents tables. Path must be under
    /mnt/praesidium/{tenant_id}/ for security.
    """
    from fastapi.responses import Response

    tid = claims.tenant_id.strip()

    # Security: only allow paths under this tenant's Praesidium mount
    allowed_prefix = f"/mnt/praesidium/{tid}/"
    if not path.startswith(allowed_prefix):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "path_outside_tenant", "path": path},
        )

    if not os.path.isfile(path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "file_not_found", "path": path},
        )

    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    mime = MIME_MAP.get(ext, "application/octet-stream")

    content = open(path, "rb").read()
    return Response(
        content=content, media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ═════════════════════════════════════════════════════════════════════════
# GET /open/{doc_id} — read-only file download (no checkout lock)
# ═════════════════════════════════════════════════════════════════════════

@router.get("/open/{doc_id}")
async def open_document_readonly(
    doc_id: str = PathParam(..., description="Document UUID from documents or dms_documents"),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Serve file bytes for read-only opening in Word.
    
    Checks both tables:
    1. documents (manual uploads) — uses storage_path + _read_file_bytes
    2. dms_documents (synced files) — uses file_path directly
    
    No checkout lock is acquired. File opens read-only in the VSTO client.
    """
    from fastapi.responses import Response
    from modules.desktop.checkout_router import _read_file_bytes
    
    tid = claims.tenant_id
    
    async with AsyncSessionLocal() as session:
        # Try documents table first
        result = await session.execute(
            sa_text("""
                SELECT id::text, filename, original_filename, mime_type, 
                       file_size, storage_path
                FROM documents
                WHERE id = CAST(:doc AS uuid)
                  AND TRIM(tenant_id) = :tid
                LIMIT 1
            """),
            {"doc": doc_id, "tid": tid},
        )
        row = result.mappings().first()
        
        if row:
            storage_path = row["storage_path"]
            filename = row["filename"] or row["original_filename"] or "document"
            mime = row["mime_type"] or "application/octet-stream"
            try:
                content = await _read_file_bytes(storage_path, tid)
            except FileNotFoundError:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "file_not_found", "storage_path": storage_path},
                )
            return Response(
                content=content, media_type=mime,
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        
        # Try dms_documents table
        result2 = await session.execute(
            sa_text("""
                SELECT id::text, file_path, file_size_bytes
                FROM dms_documents
                WHERE id = CAST(:doc AS uuid)
                  AND TRIM(tenant_id) = :tid
                LIMIT 1
            """),
            {"doc": doc_id, "tid": tid},
        )
        row2 = result2.mappings().first()
        
        if not row2:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "document_not_found", "doc_id": doc_id},
            )
        
        file_path = row2["file_path"]
        import os
        if not file_path or not os.path.exists(file_path):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "file_not_found", "file_path": file_path},
            )
        
        filename = os.path.basename(file_path)
        # Guess mime type from extension
        ext = os.path.splitext(filename)[1].lower()
        mime_map = {
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".pdf": "application/pdf",
        }
        mime = mime_map.get(ext, "application/octet-stream")
        
        content = open(file_path, "rb").read()
        
        return Response(
            content=content, media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )



# ═════════════════════════════════════════════════════════════════════════
# GET /documents/{id}/versions — version chain
# ═════════════════════════════════════════════════════════════════════════

@router.get("/documents/{doc_id}/versions")
async def list_document_versions(
    doc_id: str = PathParam(..., description="Document UUID"),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Return the full version chain for a document.

    Walks both directions:
      - ancestors (via parent_doc_id going UP)
      - descendants (via parent_doc_id pointing TO this doc going DOWN)

    Returns a flat list sorted by version_number ascending.
    """
    async with AsyncSessionLocal() as session:
        # Recursive CTE walking both directions from the given doc
        result = await session.execute(
            sa_text("""
                WITH RECURSIVE chain AS (
                  -- Anchor: the requested doc
                  SELECT id, parent_doc_id, version_number, filename,
                         file_size, mime_type, created_at, created_by,
                         storage_path, checksum, status,
                         metadata->>'checked_out_by' AS checked_out_by
                  FROM documents
                  WHERE id = CAST(:doc AS uuid)
                    AND TRIM(tenant_id) = :tid

                  UNION

                  -- Walk UP to ancestors
                  SELECT d.id, d.parent_doc_id, d.version_number, d.filename,
                         d.file_size, d.mime_type, d.created_at, d.created_by,
                         d.storage_path, d.checksum, d.status,
                         d.metadata->>'checked_out_by' AS checked_out_by
                  FROM documents d
                  JOIN chain c ON d.id = c.parent_doc_id
                  WHERE TRIM(d.tenant_id) = :tid

                  UNION

                  -- Walk DOWN to descendants
                  SELECT d.id, d.parent_doc_id, d.version_number, d.filename,
                         d.file_size, d.mime_type, d.created_at, d.created_by,
                         d.storage_path, d.checksum, d.status,
                         d.metadata->>'checked_out_by' AS checked_out_by
                  FROM documents d
                  JOIN chain c ON d.parent_doc_id = c.id
                  WHERE TRIM(d.tenant_id) = :tid
                )
                SELECT
                  id::text              AS id,
                  parent_doc_id::text   AS parent_doc_id,
                  version_number,
                  filename,
                  file_size,
                  mime_type,
                  created_at,
                  created_by::text      AS created_by,
                  checksum,
                  status,
                  checked_out_by
                FROM chain
                ORDER BY version_number ASC, created_at ASC
            """),
            {"doc": doc_id, "tid": claims.tenant_id},
        )
        rows = [_row_dict(r) for r in result.mappings().all()]

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "document_not_found", "doc_id": doc_id},
        )

    return {
        "doc_id": doc_id,
        "versions": rows,
        "version_count": len(rows),
    }
