"""
modules/ediscovery/routes/upload.py

eDiscovery ingestion entry points:

POST /api/v1/ediscovery/upload
  Multipart: files[], collection_name, matter_id, source_type, source_party
  → saves files to eDiscovery storage via FBRG-01, creates collection, queues ingest job

POST /api/v1/ediscovery/ingest-path
  JSON: { path, disk_root, collection_name, matter_id, source_type, source_party }
  → creates collection pointing at server-side path, queues ingest job

GET /api/v1/ediscovery/resolve-path
  Query: path=, disk_root=
  → looks up matter_folders, returns { matter_id, matter_name, client_name }
"""
import os
import logging
import zipfile as _zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ediscovery", tags=["ediscovery-upload"])

# File bridge URL — all file writes go through FBRG-01
EDISCOVERY_ROOT = os.environ.get("CIFS_EDISCOVERY_MOUNT", "/mnt/ediscovery")
MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024  # 5GB


# ── Path resolver ─────────────────────────────────────────────────────────────

@router.get("/resolve-path")
async def resolve_path(
    request: Request,
    path: str = "",
    disk_root: str = "",
    user=Depends(get_current_user),
):
    """
    Look up a dragged folder path in matter_folders to get matter_id and matter name.
    Called by the drop zone JS when a folder is dragged from the share browser.
    """
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal

    tenant_id = getattr(request.state, "tenant_id", "").strip()

    if not path:
        return JSONResponse({"resolved": False})

    try:
        async with AsyncSessionLocal() as session:
            # Try exact match first
            result = await session.execute(
                text("""
                    SELECT
                        mf.matter_id::text,
                        m.matter_name,
                        m.matter_number,
                        ts_clients.raw_data->>'name' as client_name
                    FROM matter_folders mf
                    JOIN matters m ON m.id = mf.matter_id
                    LEFT JOIN ts_clients ON ts_clients.raw_data->>'nickname2' = m.matter_number
                    WHERE TRIM(mf.tenant_id) = :tid
                      AND mf.folder_path = :path
                      AND (:disk_root = '' OR mf.disk_root = :disk_root)
                    LIMIT 1
                """),
                {"tid": tenant_id, "path": path, "disk_root": disk_root}
            )
            row = result.mappings().first()

            if not row:
                # Try prefix match — dragged subfolder of a mapped folder
                result = await session.execute(
                    text("""
                        SELECT
                            mf.matter_id::text,
                            m.matter_name,
                            m.matter_number,
                            ts_clients.raw_data->>'name' as client_name
                        FROM matter_folders mf
                        JOIN matters m ON m.id = mf.matter_id
                        LEFT JOIN ts_clients ON ts_clients.raw_data->>'nickname2' = m.matter_number
                        WHERE TRIM(mf.tenant_id) = :tid
                          AND :path LIKE mf.folder_path || '%'
                          AND (:disk_root = '' OR mf.disk_root = :disk_root)
                        ORDER BY LENGTH(mf.folder_path) DESC
                        LIMIT 1
                    """),
                    {"tid": tenant_id, "path": path, "disk_root": disk_root}
                )
                row = result.mappings().first()

            if row:
                return JSONResponse({
                    "resolved":    True,
                    "matter_id":   row["matter_id"],
                    "matter_name": row["matter_name"],
                    "matter_number": row["matter_number"],
                    "client_name": row["client_name"] or "",
                })

    except Exception as e:
        logger.warning(f"resolve-path error: {e}")

    return JSONResponse({"resolved": False})


# ── Server-side path ingest ───────────────────────────────────────────────────

@router.post("/ingest-path")
async def ingest_path(
    request: Request,
    user=Depends(get_current_user),
):
    """
    Create collection from a server-side path (dragged from share browser).
    No file upload — points ingest job at existing disk path.
    """
    import redis as redis_lib
    from rq import Queue

    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    matter_id       = body.get("matter_id", "")
    collection_name = body.get("collection_name", "")
    source_type     = body.get("source_type", "opposing_production")
    source_party    = body.get("source_party", "")
    folder_path     = body.get("path", "")
    disk_root       = body.get("disk_root", "")

    if not matter_id or not collection_name:
        return JSONResponse({"error": "matter_id and collection_name required"}, status_code=400)

    # Build full disk path
    dms_source_path = None
    if folder_path and disk_root:
        dms_source_path = os.path.join(disk_root, folder_path).replace("\\", "/")

    try:
        storage_path = _build_storage_path(tenant_id, matter_id, collection_name)
        collection_id = await _create_collection(
            tenant_id=tenant_id,
            matter_id=matter_id,
            collection_name=collection_name,
            source_type=source_type,
            source_party=source_party or None,
            dms_source_path=dms_source_path,
            storage_path=storage_path,
            user_id=user_id,
        )

        _enqueue_ingest(tenant_id, str(collection_id), user_id)

        return JSONResponse({
            "collection_id":   str(collection_id),
            "collection_name": collection_name,
            "status":          "queued",
        })

    except Exception as e:
        logger.error(f"ingest-path error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)




def _check_zip_password(file_path):
    """Check if a ZIP file is password-protected. Returns True if encrypted."""
    try:
        if not file_path.lower().endswith('.zip'):
            return False
        with _zipfile.ZipFile(file_path, 'r') as zf:
            for info in zf.infolist():
                if info.flag_bits & 0x1:  # encrypted flag
                    return True
            # Also try extracting first file to detect encryption
            try:
                first_file = next((i for i in zf.infolist() if not i.is_dir()), None)
                if first_file:
                    zf.read(first_file.filename)
            except RuntimeError as e:
                if 'password' in str(e).lower() or 'encrypted' in str(e).lower():
                    return True
    except Exception:
        pass
    return False

# ── File upload ingest ────────────────────────────────────────────────────────

def _safe_rel_path(rel, filename):
    """Sanitized relative path for folder-drop uploads.

    Normalizes separators, strips '', '.', '..' parts (zip-slip /
    traversal-proof). Falls back to the upload's basename.
    """
    from pathlib import PurePosixPath
    fallback = Path(filename or "upload.bin").name
    if not rel:
        return fallback
    parts = [p for p in PurePosixPath(str(rel).replace("\\", "/")).parts
             if p not in ("", ".", "..", "/")]
    if not parts:
        return fallback
    return os.path.join(*parts)


@router.post("/upload")
async def upload_files(
    request: Request,
    files: List[UploadFile] = File(...),
    paths: Optional[List[str]] = Form(None),
    collection_name: str = Form(...),
    matter_id: str = Form(...),
    source_type: str = Form("opposing_production"),
    source_party: Optional[str] = Form(None),
    user=Depends(get_current_user),
):
    """
    Accept dropped OS files, save to eDiscovery storage via FBRG-01,
    create collection, queue ingest.
    Handles multiple ZIPs for multi-volume Relativity productions.
    """
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    if not tenant_id or not matter_id or not collection_name:
        return JSONResponse({"error": "tenant_id, matter_id, collection_name required"}, status_code=400)

    storage_path = _build_storage_path(tenant_id, matter_id, collection_name)
    # Direct write to /mnt/ediscovery — no file bridge
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_name = collection_name.replace(" ", "_").replace("/", "_")[:64]
    incoming_dir = os.path.join(EDISCOVERY_ROOT, tenant_id, matter_id, f"{safe_name}_{timestamp}", "originals", "as_received")
    os.makedirs(incoming_dir, exist_ok=True)
    saved_files = []
    total_bytes = 0
    for i, upload in enumerate(files):
        try:
            content = await upload.read()
            file_size = len(content)
            total_bytes += file_size
            if total_bytes > MAX_UPLOAD_BYTES:
                return JSONResponse(
                    {"error": "Upload exceeds 5GB. Use Bitvise to put large files on server directly."},
                    status_code=413
                )
            safe_filename = _safe_rel_path(
                paths[i] if paths and i < len(paths) else None,
                upload.filename,
            )
            dest_path = os.path.join(incoming_dir, safe_filename)
            os.makedirs(os.path.dirname(dest_path) or incoming_dir,
                        exist_ok=True)
            with open(dest_path, "wb") as fout:
                fout.write(content)
            saved_files.append(safe_filename)
            logger.info(f"Saved directly: {dest_path} ({file_size} bytes)")
        except Exception as e:
            logger.error(f"File save error {upload.filename}: {e}")
            return JSONResponse({"error": f"Failed to save {upload.filename}: {e}"}, status_code=500)
    if not saved_files:
        return JSONResponse({"error": "No files were saved"}, status_code=400)

    # Check for password-protected ZIPs
    password_protected = []
    for fname in saved_files:
        fpath = os.path.join(incoming_dir, fname)
        if fname.lower().endswith('.zip') and _check_zip_password(fpath):
            password_protected.append(fname)

    if password_protected:
        return JSONResponse({
            "error": "password_protected",
            "message": f"ZIP file(s) are password-protected: {', '.join(password_protected)}. Please provide the password or upload an unencrypted archive.",
            "files": password_protected,
            "storage_path": incoming_dir,
        }, status_code=422)

    # dms_source_path is the as_received directory on the actual mount
    dms_source_path = os.path.join(EDISCOVERY_ROOT, tenant_id, matter_id, f"{safe_name}_{timestamp}", "originals", "as_received")

    try:
        collection_id = await _create_collection(
            tenant_id=tenant_id,
            matter_id=matter_id,
            collection_name=collection_name,
            source_type=source_type,
            source_party=source_party,
            dms_source_path=dms_source_path,
            storage_path=os.path.join(EDISCOVERY_ROOT, tenant_id, matter_id, f"{safe_name}_{timestamp}"),
            user_id=user_id,
        )

        _enqueue_ingest(tenant_id, str(collection_id), user_id)

        return JSONResponse({
            "collection_id":   str(collection_id),
            "collection_name": collection_name,
            "file_count":      len(saved_files),
            "total_mb":        round(total_bytes / 1024 / 1024, 1),
            "status":          "queued",
        })

    except Exception as e:
        logger.error(f"upload collection create error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_storage_path(tenant_id: str, matter_id: str, collection_name: str) -> str:
    safe_name  = collection_name.replace(" ", "_").replace("/", "_")[:64]
    timestamp  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return f"/mnt/ediscovery/{tenant_id}/{matter_id}/{safe_name}_{timestamp}"


async def _create_collection(
    tenant_id, matter_id, collection_name, source_type,
    source_party, dms_source_path, storage_path, user_id
) -> str:
    """Insert ediscovery_collections row, return UUID."""
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO ediscovery_collections
                    (tenant_id, matter_id, name, collection_name, status,
                     source_type, source_party, dms_source_path,
                     storage_path, received_by, created_at, updated_at)
                VALUES
                    (:tid, :mid, :name, :name, 'collecting',
                     :src_type, :src_party, :src_path,
                     :storage, :uid, NOW(), NOW())
                RETURNING id::text
            """),
            {
                "tid":      tenant_id,
                "mid":      matter_id,
                "name":     collection_name,
                "src_type": source_type,
                "src_party": source_party,
                "src_path": dms_source_path,
                "storage":  storage_path,
                "uid":      user_id,
            }
        )
        collection_id = result.scalar()
        await session.commit()
        return collection_id


def _enqueue_ingest(tenant_id: str, collection_id: str, user_id):
    """Push ingest job to RQ."""
    import redis as redis_lib
    from rq import Queue

    redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
    q = Queue("ediscovery", connection=redis_lib.Redis.from_url(redis_url))
    q.enqueue(
        "modules.ediscovery.jobs.ledger_dag.run_collection_full",
        tenant_id,
        collection_id,
        user_id,
        spine_workers=6,
        ocr_workers=2,
        embed_workers=1,
        job_timeout="24h",
        result_ttl=3600,
    )
    logger.info(f"Enqueued ingest for collection {collection_id}")


# ---------------------------------------------------------------------------
# POST /api/v1/ediscovery/upload-production-zip
#   Receives a production zip from the New Collection form drop zone.
#   Extracts to /mnt/ediscovery/{tenant_id}/incoming/{timestamp}/
#   Peeks the .dat file to extract Bates begin/end for auto-populate.
#   Returns: {path, file_count, bates_begin, bates_end}
# ---------------------------------------------------------------------------

@router.post("/upload-production-zip")
async def upload_production_zip(
    request: Request,
    file: UploadFile = File(...),
):
    import zipfile
    import tarfile
    import tempfile
    import time

    tenant_id = await _get_tenant_id(request)

    timestamp = int(time.time())
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(file.filename).stem)[:60]
    dest_dir = os.path.join(
        EDISCOVERY_ROOT, tenant_id, "incoming", f"{safe_name}_{timestamp}"
    )
    os.makedirs(dest_dir, exist_ok=True)

    # Write uploaded file to temp location
    tmp_path = os.path.join(dest_dir, file.filename)
    contents = await file.read()
    with open(tmp_path, "wb") as f_out:
        f_out.write(contents)

    # Extract archive
    file_count = 0
    extracted_dir = os.path.join(dest_dir, "originals", "unpacked")
    os.makedirs(extracted_dir, exist_ok=True)

    try:
        fname_lower = file.filename.lower()
        if fname_lower.endswith(".zip"):
            with zipfile.ZipFile(tmp_path, "r") as zf:
                zf.extractall(extracted_dir)
                file_count = len([n for n in zf.namelist() if not n.endswith("/")])
        elif fname_lower.endswith((".tar.gz", ".tgz", ".tar")):
            with tarfile.open(tmp_path, "r:*") as tf:
                tf.extractall(extracted_dir)
                file_count = len([m for m in tf.getmembers() if m.isfile()])
    except Exception as ex:
        logger.error("upload-production-zip extraction error: %s", ex)

    # Peek .dat file for Bates range
    bates_begin = ""
    bates_end = ""
    try:
        import glob as _glob
        dat_files = _glob.glob(
            os.path.join(extracted_dir, "**", "*.dat"), recursive=True
        )
        if dat_files:
            dat_path = dat_files[0]
            DAT_SEP = "þ"
            DAT_QUOTE = ""
            for enc in ("utf-8-sig", "windows-1252", "utf-8"):
                try:
                    with open(dat_path, encoding=enc, errors="replace") as df:
                        lines = df.read().splitlines()
                    break
                except Exception:
                    continue

            if lines:
                headers = [h.strip(DAT_QUOTE).strip() for h in lines[0].split(DAT_SEP)]
                BATES_KEYS = [
                    "Production::Begin Bates", "Begin Bates", "BegBates",
                    "BEGBATES", "Beg Prod",
                ]
                bates_col = next(
                    (h for h in headers if h in BATES_KEYS), None
                )
                if bates_col:
                    bates_idx = headers.index(bates_col)
                    all_bates = []
                    for line in lines[1:]:
                        if not line.strip():
                            continue
                        vals = line.split(DAT_SEP)
                        if bates_idx < len(vals):
                            b = vals[bates_idx].strip(DAT_QUOTE).strip()
                            if b:
                                all_bates.append(b)
                    if all_bates:
                        bates_begin = all_bates[0]
                        bates_end = all_bates[-1]
    except Exception as be:
        logger.warning("Bates peek error: %s", be)

    return JSONResponse({
        "path": dest_dir,
        "file_count": file_count,
        "bates_begin": bates_begin,
        "bates_end": bates_end,
    })
