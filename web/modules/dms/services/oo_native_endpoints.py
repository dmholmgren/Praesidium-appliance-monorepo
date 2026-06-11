"""
oo_native_endpoints.py - Native OnlyOffice Version Integration (fixed)
Adds to matter_workspace_api.py router.

POST /{matter_id}/oo-saveas       - Handle File > Save Copy As
GET  /{matter_id}/oo-history      - Version chain for refreshHistory
GET  /{matter_id}/oo-history-data - Version data for setHistoryData
POST /{matter_id}/oo-restore      - Restore a version

Patent Pending - Series 1/2/3 - D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations
import hashlib, logging, mimetypes, os, re, shutil, time, urllib.parse, uuid as _uuid
from datetime import datetime, timezone

import httpx
import jwt as pyjwt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

PRAESIDIUM_ROOT = "/mnt/praesidium"
OO_JWT_SECRET = "praesidium-oo-jwt-2026"
OO_INTERNAL = os.environ.get("OO_INTERNAL_URL", "http://172.28.0.20")
WEB_INTERNAL = os.environ.get("OO_INTERNAL_WEB_URL", "http://172.28.0.5:8000")


async def _resolve_matter_native(tid, mid):
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.matter_name, c.client_name
            FROM matters m LEFT JOIN clients c ON m.client_id = c.id AND trim(m.tenant_id) = trim(c.tenant_id)
            WHERE m.id = CAST(:mid AS uuid) AND trim(m.tenant_id) = trim(:tid)
        """), {"mid": mid, "tid": tid})
        row = r.mappings().fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Matter not found")
    p = os.path.join(PRAESIDIUM_ROOT, tid, "matters", row["client_name"] or "", row["matter_name"] or "")
    return p if os.path.isdir(p) else None


async def _find_doc_by_path(tid, storage_path):
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT id::text AS document_id, filename, version_number,
                   parent_doc_id::text, file_size, checksum,
                   created_at::text, storage_path
            FROM documents
            WHERE storage_path = :sp AND TRIM(tenant_id) = :tid
            LIMIT 1
        """), {"sp": storage_path, "tid": tid})
        return r.mappings().fetchone()


async def _get_version_chain(tid, doc_id):
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            WITH RECURSIVE ancestors AS (
                SELECT id, parent_doc_id FROM documents
                WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
                UNION ALL
                SELECT d.id, d.parent_doc_id FROM documents d
                JOIN ancestors a ON a.parent_doc_id = d.id
                WHERE TRIM(d.tenant_id) = :tid AND a.parent_doc_id IS NOT NULL
            )
            SELECT id::text FROM ancestors WHERE parent_doc_id IS NULL LIMIT 1
        """), {"did": doc_id, "tid": tid})
        root_id = r.scalar() or doc_id

        r2 = await session.execute(sa_text("""
            WITH RECURSIVE chain AS (
                SELECT id, parent_doc_id, version_number, filename,
                       storage_path, file_size, checksum, created_at
                FROM documents WHERE id = CAST(:rid AS uuid) AND TRIM(tenant_id) = :tid
                UNION ALL
                SELECT d.id, d.parent_doc_id, d.version_number, d.filename,
                       d.storage_path, d.file_size, d.checksum, d.created_at
                FROM documents d JOIN chain c ON d.parent_doc_id = c.id
                WHERE TRIM(d.tenant_id) = :tid
            )
            SELECT id::text AS document_id, version_number, filename,
                   storage_path, file_size, checksum, created_at::text AS created_at
            FROM chain ORDER BY version_number ASC
        """), {"rid": root_id, "tid": tid})
        return root_id, [dict(row) for row in r2.mappings().fetchall()]


def _abs_to_matter_rel(abs_path, matter_root):
    """Convert absolute storage_path to matter-relative path for oo-download."""
    if not matter_root or not abs_path:
        return abs_path
    try:
        rel = os.path.relpath(abs_path, matter_root)
        if rel.startswith('..'):
            return abs_path
        return rel
    except ValueError:
        return abs_path


async def oo_saveas(request: Request, matter_id: str):
    tid = (getattr(request.state, "tenant_id", "") or "").strip()
    body = await request.json()
    oo_url = body.get("url", "")
    title = body.get("title", "")
    file_type = body.get("fileType", "docx")
    source_path = body.get("path", "")

    if not oo_url:
        raise HTTPException(status_code=400, detail="url required")

    root = await _resolve_matter_native(tid, matter_id)
    if not root:
        raise HTTPException(status_code=404, detail="No disk root")

    download_url = re.sub(r'https?://[^/]+/oo/', OO_INTERNAL + '/', oo_url, count=1)
    if download_url.startswith('https://') or download_url.startswith('http://login'):
        download_url = re.sub(r'https?://[^/]+/', OO_INTERNAL + '/', download_url, count=1)

    try:
        async with httpx.AsyncClient(timeout=60, verify=False) as client:
            resp = await client.get(download_url)
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail="OO download failed: HTTP " + str(resp.status_code))
            content = resp.content
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail="OO download error: " + str(e))

    save_dir = os.path.dirname(os.path.join(root, source_path)) if source_path else root
    if not os.path.isdir(save_dir):
        os.makedirs(save_dir, exist_ok=True)

    save_filename = title or ("SavedCopy." + file_type)
    save_path = os.path.join(save_dir, save_filename)
    if os.path.exists(save_path):
        base, ext = os.path.splitext(save_filename)
        i = 1
        while os.path.exists(save_path):
            save_path = os.path.join(save_dir, base + " (" + str(i) + ")" + ext)
            i += 1
        save_filename = os.path.basename(save_path)

    with open(save_path, "wb") as f:
        f.write(content)

    checksum = hashlib.sha256(content).hexdigest()
    doc_id = str(_uuid.uuid4())
    mime_type = mimetypes.guess_type(save_filename)[0] or "application/octet-stream"

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                INSERT INTO documents (id, tenant_id, matter_id, filename, original_filename,
                    mime_type, file_size, storage_path, checksum, version_number, status, created_at, updated_at)
                VALUES (CAST(:id AS uuid), :tid, CAST(:mid AS uuid), :fname, :fname,
                    :mime, :fsize, :spath, :cs, 1, 'active', NOW(), NOW())
                ON CONFLICT DO NOTHING
            """), {"id": doc_id, "tid": tid, "mid": matter_id, "fname": save_filename,
                   "mime": mime_type, "fsize": len(content), "spath": save_path, "cs": checksum})
            await session.commit()
    except Exception as e:
        logger.error("oo-saveas doc insert: %s", e)

    version_number = None
    if source_path:
        source_abs = os.path.join(root, source_path) if not source_path.startswith('/') else source_path
        orig_doc = await _find_doc_by_path(tid, source_abs)
        if orig_doc:
            try:
                from modules.dms.services.dms_version_service import create_version_link
                vlink = await create_version_link(tid, doc_id, orig_doc["document_id"])
                version_number = vlink.get("version_number")
            except Exception as e:
                logger.warning("Version link failed: %s", e)

    return JSONResponse({"status": "ok", "document_id": doc_id, "filename": save_filename,
                         "version_number": version_number, "path": os.path.relpath(save_path, root)})


async def oo_history(request: Request, matter_id: str, path: str = ""):
    tid = (getattr(request.state, "tenant_id", "") or "").strip()
    root = await _resolve_matter_native(tid, matter_id)
    if not root:
        return JSONResponse({"history": [], "currentVersion": 1})

    abs_path = os.path.join(root, path) if path and not path.startswith('/') else (path or root)
    doc = await _find_doc_by_path(tid, abs_path)
    if not doc:
        mtime = os.path.getmtime(abs_path) if os.path.isfile(abs_path) else 0
        return JSONResponse({"currentVersion": 1, "history": [{"created": datetime.now(timezone.utc).strftime("%Y-%m-%d %I:%M %p"),
            "key": hashlib.md5(("{}:{}".format(abs_path, mtime)).encode()).hexdigest()[:20], "version": 1, "user": {"id": "0", "name": "System"}}]})

    _, versions = await _get_version_chain(tid, doc["document_id"])
    if not versions:
        return JSONResponse({"history": [], "currentVersion": 1})

    current_ver = 1
    for v in versions:
        if v["document_id"] == doc["document_id"]:
            current_ver = v["version_number"] or 1
            break

    history = []
    for v in versions:
        sp = v["storage_path"] or ""
        mtime = os.path.getmtime(sp) if sp and os.path.isfile(sp) else 0
        key = hashlib.md5(("{}:{}:{}".format(sp, mtime, v["checksum"] or "")).encode()).hexdigest()[:20]
        created = v["created_at"] or ""
        try:
            dt = datetime.fromisoformat(created.split('.')[0])
            created_fmt = dt.strftime("%Y-%m-%d %I:%M %p")
        except:
            created_fmt = created[:19]
        history.append({"created": created_fmt, "key": key, "version": v["version_number"] or (len(history)+1),
                        "user": {"id": "0", "name": "System"}})

    return JSONResponse({"currentVersion": current_ver, "history": history})


async def oo_history_data(request: Request, matter_id: str, path: str = "", version: int = 1):
    tid = (getattr(request.state, "tenant_id", "") or "").strip()
    root = await _resolve_matter_native(tid, matter_id)
    if not root:
        raise HTTPException(status_code=404, detail="No disk root")

    abs_path = os.path.join(root, path) if path and not path.startswith('/') else (path or root)
    doc = await _find_doc_by_path(tid, abs_path)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    root_id, versions = await _get_version_chain(tid, doc["document_id"])
    ver_row = None
    for v in versions:
        if v["version_number"] == version:
            ver_row = v
            break
    if not ver_row:
        raise HTTPException(status_code=404, detail="Version " + str(version) + " not found")

    sp = ver_row["storage_path"] or ""
    if not sp or not os.path.isfile(sp):
        raise HTTPException(status_code=404, detail="Version file not on disk")

    ext = sp.rsplit('.', 1)[-1].lower() if '.' in sp else 'docx'
    mtime = os.path.getmtime(sp)
    key = hashlib.md5(("{}:{}:{}".format(sp, mtime, ver_row["checksum"] or "")).encode()).hexdigest()[:20]

    # Build download URL using the existing oo-download endpoint
    # which takes matter_id + matter-relative path
    rel_path = _abs_to_matter_rel(sp, root)
    enc_path = urllib.parse.quote(rel_path, safe="")
    url = WEB_INTERNAL + "/api/v1/dms/matter/" + matter_id + "/oo-download?path=" + enc_path

    payload = {"fileType": ext, "key": key, "url": url, "version": version}
    payload["token"] = pyjwt.encode(payload, OO_JWT_SECRET, algorithm="HS256")
    return JSONResponse(payload)


async def oo_restore(request: Request, matter_id: str):
    tid = (getattr(request.state, "tenant_id", "") or "").strip()
    body = await request.json()
    source_path = body.get("path", "")
    version = body.get("version", 1)

    root = await _resolve_matter_native(tid, matter_id)
    if not root:
        raise HTTPException(status_code=404, detail="No disk root")

    abs_path = os.path.join(root, source_path) if source_path and not source_path.startswith('/') else (source_path or root)
    doc = await _find_doc_by_path(tid, abs_path)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    _, versions = await _get_version_chain(tid, doc["document_id"])
    ver_path = None
    for v in versions:
        if v["version_number"] == version:
            ver_path = v["storage_path"]
            break
    if not ver_path or not os.path.isfile(ver_path):
        raise HTTPException(status_code=404, detail="Version " + str(version) + " file not found")

    if os.path.isfile(abs_path):
        backup_dir = os.path.join(os.path.dirname(abs_path), ".versions")
        os.makedirs(backup_dir, exist_ok=True)
        shutil.copy2(abs_path, os.path.join(backup_dir, os.path.basename(abs_path) + "." + str(int(time.time())) + ".pre-restore.bak"))

    shutil.copy2(ver_path, abs_path)
    logger.info("oo-restore: v%d from %s to %s", version, ver_path, abs_path)
    return JSONResponse({"status": "ok", "version": version})
