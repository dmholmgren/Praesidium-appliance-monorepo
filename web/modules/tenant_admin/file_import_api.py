"""
modules/tenant_admin/file_import_api.py
JSON API for the React File Import page (Data Sources, AI Recon, Sync Report, Integrity).
"""
import json
import logging
import os
import subprocess
import uuid as _uuid
from datetime import datetime, date
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/file-import", tags=["file-import-api"])

# LEGACY_ROOT = "/mnt/legacy-qnap/D Drive Backup/Clients"  # deprecated
LEGACY_ROOT = "/mnt/legacy"
PRAESIDIUM_ROOT = "/mnt/praesidium"


def _tid(request):
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _ser(obj):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {k: _ser(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_ser(v) for v in obj]
    if isinstance(obj, _uuid.UUID):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return obj


# ═══════════════════════════════════════════════════════════════════════
# DATA SOURCES TAB
# ═══════════════════════════════════════════════════════════════════════

@router.get("/sources")
async def api_sources(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT cs.id::text, cs.source_name, cs.mount_path, cs.description,
                   cs.read_only, cs.status, cs.last_sync_at, cs.last_sync_count
            FROM connector_sources cs
            WHERE TRIM(cs.tenant_id) = :tid ORDER BY cs.source_name
        """), {"tid": tid})).mappings().all()
        inv_counts = (await session.execute(text("""
            SELECT source_id::text, COUNT(*) AS file_count
            FROM file_inventory WHERE TRIM(tenant_id) = :tid AND entry_type = 'file'
            GROUP BY source_id
        """), {"tid": tid})).mappings().all()
        inv_map = {r["source_id"]: r["file_count"] for r in inv_counts}
        fm_counts = (await session.execute(text("""
            SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE accepted = true) AS matched
            FROM dms_folder_matches WHERE TRIM(tenant_id) = :tid
        """), {"tid": tid})).fetchone()

    sources = []
    for r in rows:
        d = dict(r)
        d["file_count"] = inv_map.get(d["id"], d.get("last_sync_count") or 0)
        d["matched_folders"] = fm_counts[1] if fm_counts else 0
        d["total_folders"] = fm_counts[0] if fm_counts else 0
        sources.append(d)
    return JSONResponse(_ser(sources))


@router.post("/sources/add")
async def api_source_add(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    body = await request.json()
    name = (body.get("source_name") or "").strip()
    mount = (body.get("mount_path") or "").strip()
    desc = (body.get("description") or "").strip()
    read_only = body.get("read_only", True)
    if not name or not mount:
        return JSONResponse({"error": "Name and mount path required"}, status_code=400)
    if not os.path.isdir(mount):
        return JSONResponse({"error": f"Path not found: {mount}"}, status_code=400)
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            INSERT INTO connector_sources (id, tenant_id, source_name, mount_path, description, read_only, status, connector_type)
            VALUES (gen_random_uuid(), :tid, :name, :mount, :desc, :ro, 'active', 'file_source')
        """), {"tid": tid, "name": name, "mount": mount, "desc": desc, "ro": read_only})
        await session.commit()
    return JSONResponse({"ok": True})


@router.post("/sources/{source_id}/scan")
async def api_source_scan(request: Request, source_id: str, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text("SELECT mount_path FROM connector_sources WHERE id = CAST(:sid AS uuid) AND TRIM(tenant_id) = :tid"), {"sid": source_id, "tid": tid})).fetchone()
    if not row:
        return JSONResponse({"error": "Source not found"}, status_code=404)
    mount = row[0]
    if not os.path.isdir(mount):
        return JSONResponse({"error": f"Mount not accessible: {mount}"}, status_code=400)
    file_count = 0
    folder_count = 0
    for dirpath, dirnames, filenames in os.walk(mount):
        dirnames[:] = [d for d in dirnames if not d.startswith('.') and not d.startswith('@')]
        folder_count += 1
        file_count += sum(1 for f in filenames if not f.startswith('.'))
    async with AsyncSessionLocal() as session:
        await session.execute(text("UPDATE connector_sources SET last_sync_at = now(), last_sync_count = :fc WHERE id = CAST(:sid AS uuid)"), {"sid": source_id, "fc": file_count})
        await session.commit()
    return JSONResponse({"ok": True, "files": file_count, "folders": folder_count})


@router.post("/sources/{source_id}/deactivate")
async def api_source_deactivate(request: Request, source_id: str, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        await session.execute(text("UPDATE connector_sources SET status = 'inactive' WHERE id = CAST(:sid AS uuid) AND TRIM(tenant_id) = :tid"), {"sid": source_id, "tid": tid})
        await session.commit()
    return JSONResponse({"ok": True})


@router.get("/sources/browse")
async def api_source_browse(request: Request, source_id: str = "", prefix: str = "", share: str = "", user=Depends(get_current_user)):
    tid = _tid(request)
    if source_id:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(text("SELECT mount_path FROM connector_sources WHERE id = CAST(:sid AS uuid) AND TRIM(tenant_id) = :tid"), {"sid": source_id, "tid": tid})).fetchone()
        if not row:
            return JSONResponse({"error": "Source not found"}, status_code=404)
        base = row[0]
    else:
        base = {
            # "clients": "/mnt/clients",   # deprecated — all data under /mnt/legacy
            # "docsend": "/mnt/docsend",   # deprecated — all data under /mnt/legacy
            "legacy": "/mnt/legacy",
        }.get(share, "/mnt/legacy")
    target = os.path.join(base, prefix) if prefix else base
    target = os.path.realpath(target)
    if not target.startswith(os.path.realpath(base)):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not os.path.isdir(target):
        return JSONResponse({"folders": [], "files": 0, "path": prefix})
    folders = []
    file_count = 0
    file_list = []
    try:
        for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
            if entry.name.startswith('.') or entry.name.startswith('@'):
                continue
            if entry.is_dir(follow_symlinks=False):
                try:
                    fc = sum(1 for f in os.scandir(entry.path) if f.is_file() and not f.name.startswith('.'))
                    sc = sum(1 for f in os.scandir(entry.path) if f.is_dir() and not f.name.startswith('.'))
                except (PermissionError, OSError):
                    fc = sc = 0
                folders.append({"name": entry.name, "path": os.path.join(prefix, entry.name) if prefix else entry.name, "file_count": fc, "subfolder_count": sc})
            elif entry.is_file():
                file_count += 1
                try:
                    sz = entry.stat(follow_symlinks=False).st_size
                except (PermissionError, OSError):
                    sz = 0
                file_list.append({"name": entry.name, "path": os.path.join(prefix, entry.name) if prefix else entry.name, "size": sz, "ext": os.path.splitext(entry.name)[1].lower()})
    except (PermissionError, OSError) as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"folders": folders, "files": file_count, "file_list": file_list, "path": prefix})


@router.post("/sources/create-matter")
async def api_create_matter(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    body = await request.json()
    client_name = (body.get("client_name") or "").strip()
    matter_name = (body.get("matter_name") or "").strip()
    matter_number = (body.get("matter_number") or "").strip()
    matter_type = body.get("matter_type", "litigation")
    if not client_name or not matter_name:
        return JSONResponse({"error": "Client and matter name required"}, status_code=400)
    async with AsyncSessionLocal() as session:
        existing = (await session.execute(text("SELECT id FROM clients WHERE TRIM(tenant_id) = :tid AND LOWER(client_name) = LOWER(:cn) LIMIT 1"), {"tid": tid, "cn": client_name})).fetchone()
        if existing:
            client_id = str(existing[0])
        else:
            r = (await session.execute(text("INSERT INTO clients (id, tenant_id, client_name) VALUES (gen_random_uuid(), :tid, :cn) RETURNING id::text"), {"tid": tid, "cn": client_name})).fetchone()
            client_id = r[0]
        r2 = (await session.execute(text("INSERT INTO matters (id, tenant_id, client_id, matter_name, matter_number, matter_type, status) VALUES (gen_random_uuid(), :tid, CAST(:cid AS uuid), :mn, :num, :mt, 'active') RETURNING id::text"), {"tid": tid, "cid": client_id, "mn": matter_name, "num": matter_number or "", "mt": matter_type})).fetchone()
        await session.commit()
    return JSONResponse({"ok": True, "matter_id": r2[0], "matter_name": matter_name, "client_name": client_name})


# ═══════════════════════════════════════════════════════════════════════

@router.get("/clients")
async def api_clients(request: Request, user=Depends(get_current_user)):
    """Return all clients for this tenant, for dropdown population."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(
            "SELECT id::text, client_name FROM clients WHERE TRIM(tenant_id) = :tid ORDER BY client_name"
        ), {"tid": tid})).mappings().all()
    return JSONResponse(_ser([dict(r) for r in rows]))




# ═══════════════════════════════════════════════════════════════════════
# COLLECTION CREATION (for File Import tab)
# ═══════════════════════════════════════════════════════════════════════

@router.post("/create-collection")
async def api_create_collection(request: Request, user=Depends(get_current_user)):
    """Create an eDiscovery collection pointing at a legacy source path.
    Enqueues the ingestion RQ job. Returns JSON with collection id."""
    tid = _tid(request)
    body = await request.json()
    matter_id = (body.get("matter_id") or "").strip()
    collection_name = (body.get("collection_name") or "").strip()
    source_type = body.get("source_type", "opposing_production")
    source_party = body.get("source_party", "")
    received_date = body.get("received_date", "")
    source_path = (body.get("source_path") or "").strip()

    if not matter_id or not collection_name:
        return JSONResponse({"error": "matter_id and collection_name required"}, status_code=400)
    if not source_path:
        return JSONResponse({"error": "source_path required"}, status_code=400)

    # Validate source path exists
    # Could be a single file path or directory
    first_path = source_path.split(",")[0].strip()
    if not os.path.exists(first_path):
        return JSONResponse({"error": f"Path not found: {first_path}"}, status_code=400)

    import uuid as _u
    collection_id = str(_u.uuid4())
    ediscovery_root = os.environ.get("EDISCOVERY_STORAGE_ROOT", "/mnt/ediscovery")
    storage_path = os.path.join(ediscovery_root, tid, matter_id, collection_name.replace(" ", "_"))

    # Determine source_type enum value
    valid_types = ["opposing_production", "client_documents", "third_party_subpoena",
                   "government_records", "internal_collection", "client_collection_dedicated"]
    if source_type not in valid_types:
        source_type = "opposing_production"

    user_id = None
    try:
        user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    except Exception:
        pass

    async with AsyncSessionLocal() as session:
        # Verify matter exists
        mr = (await session.execute(text(
            "SELECT id FROM matters WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"
        ), {"mid": matter_id, "tid": tid})).fetchone()
        if not mr:
            return JSONResponse({"error": "Matter not found"}, status_code=404)

        # Insert collection
        await session.execute(text("""
            INSERT INTO ediscovery_collections
                (id, tenant_id, matter_id, collection_name, storage_path,
                 source_type, source_party, dms_source_path, status, received_by)
            VALUES
                (CAST(:cid AS uuid), :tid, CAST(:mid AS uuid), :name, :spath,
                 :stype, :sparty, :dms_path, 'collecting', :uid)
        """), {
            "cid": collection_id, "tid": tid, "mid": matter_id,
            "name": collection_name, "spath": storage_path,
            "stype": source_type, "sparty": source_party or None,
            "dms_path": source_path, "uid": user_id,
        })
        await session.commit()

    # Enqueue ingestion job
    try:
        from redis import Redis
        from rq import Queue
        REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
        redis_conn = Redis.from_url(REDIS_URL)
        q = Queue("ediscovery", connection=redis_conn)
        q.enqueue(
            "modules.ediscovery.jobs.ledger_dag.run_collection_full",
            tid, collection_id, user_id,
            spine_workers=6,
            ocr_workers=2,
            embed_workers=1,
            job_timeout="24h",
            result_ttl=3600,
        )
        logger.info("Enqueued ingestion for collection %s", collection_id)
    except Exception as e:
        logger.error("Failed to enqueue ingestion: %s", e)
        return JSONResponse({"id": collection_id, "status": "created_no_job", "error": str(e)})

    return JSONResponse({"id": collection_id, "status": "collecting", "collection_name": collection_name})


# AI RECONCILIATION TAB
# ═══════════════════════════════════════════════════════════════════════

@router.get("/recon/matches")
async def api_recon_matches(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        matches = (await session.execute(text("""
            SELECT fm.id::text, fm.folder_path, fm.score, fm.accepted,
                   fm.disk_file_count, fm.best_disk_path,
                   m.matter_name, c.client_name, m.id::text AS matter_id
            FROM dms_folder_matches fm
            LEFT JOIN matters m ON fm.matter_id = m.id
            LEFT JOIN clients c ON m.client_id = c.id
            WHERE TRIM(fm.tenant_id) = :tid
            ORDER BY fm.accepted ASC NULLS FIRST, fm.score DESC
        """), {"tid": tid})).mappings().all()
        stats = (await session.execute(text("""
            SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE accepted = true) AS accepted,
                   COUNT(*) FILTER (WHERE accepted = false) AS pending,
                   COUNT(*) FILTER (WHERE accepted IS NULL) AS unreviewed
            FROM dms_folder_matches WHERE TRIM(tenant_id) = :tid
        """), {"tid": tid})).fetchone()
    return JSONResponse(_ser({"matches": [dict(m) for m in matches], "stats": {"total": stats[0] or 0, "accepted": stats[1] or 0, "pending": stats[2] or 0, "unreviewed": stats[3] or 0}}))


@router.get("/recon/mapped-index")
async def api_recon_mapped_index(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT fm.id::text, fm.folder_path, fm.score, fm.best_disk_path, fm.disk_file_count,
                   m.matter_name, c.client_name, m.id::text AS matter_id
            FROM dms_folder_matches fm LEFT JOIN matters m ON fm.matter_id = m.id
            LEFT JOIN clients c ON m.client_id = c.id
            WHERE TRIM(fm.tenant_id) = :tid AND fm.accepted = true ORDER BY fm.folder_path
        """), {"tid": tid})).mappings().all()
    return JSONResponse(_ser([dict(r) for r in rows]))


@router.get("/recon/search-matters")
async def api_recon_search_matters(request: Request, q: str = "", user=Depends(get_current_user)):
    tid = _tid(request)
    if not q or len(q) < 2:
        return JSONResponse([])
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT m.id::text, m.matter_name, m.matter_number, c.client_name
            FROM matters m LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
            WHERE TRIM(m.tenant_id) = :tid AND (m.matter_name ILIKE :q OR c.client_name ILIKE :q OR m.matter_number ILIKE :q)
            ORDER BY c.client_name, m.matter_name LIMIT 20
        """), {"tid": tid, "q": f"%{q}%"})).mappings().all()
    return JSONResponse(_ser([dict(r) for r in rows]))


@router.post("/recon/accept-batch")
async def api_recon_accept_batch(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    body = await request.json()
    match_ids = body.get("match_ids", [])
    items = body.get("items", [])
    count = 0
    async with AsyncSessionLocal() as session:
        if items:
            for item in items:
                mid = item.get("matter_id", ""); disk_path = item.get("disk_path", "")
                if not mid or not disk_path: continue
                await session.execute(text("""
                    INSERT INTO dms_folder_matches (id, tenant_id, matter_id, folder_path, score, accepted, best_disk_path, disk_file_count, computed_at)
                    VALUES (gen_random_uuid(), :tid, CAST(:mid AS uuid), :fp, 1.0, true, :bp, :fc, NOW())
                    ON CONFLICT (tenant_id, folder_path, matter_id) DO UPDATE SET
                        folder_path = EXCLUDED.folder_path,
                        best_disk_path = EXCLUDED.best_disk_path,
                        disk_file_count = EXCLUDED.disk_file_count,
                        score = EXCLUDED.score,
                        accepted = true,
                        computed_at = NOW()
                """), {"tid": tid, "mid": mid, "fp": disk_path, "bp": item.get("disk_root") or disk_path, "fc": item.get("file_count")})
                count += 1
        elif match_ids:
            for mid in match_ids:
                await session.execute(text("UPDATE dms_folder_matches SET accepted = true WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"), {"mid": mid, "tid": tid})
            count = len(match_ids)
        await session.commit()
    return JSONResponse({"ok": True, "accepted": count})


@router.delete("/recon/reject/{mapping_id}")
async def api_recon_reject(request: Request, mapping_id: str, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        await session.execute(text("UPDATE dms_folder_matches SET accepted = false WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"), {"mid": mapping_id, "tid": tid})
        await session.commit()
    return JSONResponse({"ok": True})


@router.post("/recon/sync-matter/{matter_id}")
async def api_recon_sync_matter(request: Request, matter_id: str, user=Depends(get_current_user)):
    from modules.tenant_admin.tenant_admin import folder_recon_sync_matter
    return await folder_recon_sync_matter(request, matter_id, user)


# ═══════════════════════════════════════════════════════════════════════
# SYNC REPORT TAB
# ═══════════════════════════════════════════════════════════════════════

@router.get("/sync/report")
async def api_sync_report(request: Request, filter: str = "all", offset: int = 0, limit: int = 200, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        prae_rows = (await session.execute(text("SELECT folder_root, COUNT(*) AS cnt FROM dms_documents WHERE TRIM(tenant_id) = :tid AND source = 'matter_sync' AND folder_root LIKE '/mnt/praesidium/%' GROUP BY folder_root"), {"tid": tid})).mappings().all()
        prae_counts = {r["folder_root"]: r["cnt"] for r in prae_rows}
        matches = (await session.execute(text("""
            SELECT fm.id::text, fm.folder_path, fm.best_disk_path, fm.score, fm.accepted,
                   fm.disk_file_count, fm.synced_at, fm.synced_file_count, fm.matter_id::text,
                   m.matter_name, c.client_name
            FROM dms_folder_matches fm LEFT JOIN matters m ON fm.matter_id = m.id
            LEFT JOIN clients c ON m.client_id = c.id
            WHERE TRIM(fm.tenant_id) = :tid ORDER BY fm.folder_path
        """), {"tid": tid})).mappings().all()
        try:
            excl_rows = (await session.execute(text("SELECT matter_id::text AS mid, COALESCE(SUM(file_count),0) AS cnt FROM onboarding_clusters WHERE TRIM(tenant_id) = :tid AND status <> 'dismissed' GROUP BY matter_id"), {"tid": tid})).mappings().all()
            excl_counts = {r["mid"]: int(r["cnt"]) for r in excl_rows if r["mid"]}
        except Exception:
            excl_counts = {}
        total_row = (await session.execute(text("SELECT COUNT(*) FROM file_inventory WHERE TRIM(tenant_id) = :tid AND entry_type = 'folder' AND depth = 1 AND full_path LIKE :p"), {"tid": tid, "p": LEGACY_ROOT + "%"})).fetchone()
        total_folders = total_row[0] if total_row else 0
        recon_row = None
        try:
            recon_row = (await session.execute(text("""
                SELECT run_id::text, MIN(created_at) AS created_at,
                    COUNT(*) FILTER (WHERE issue_type = 'orphan') AS orphans,
                    COUNT(*) FILTER (WHERE issue_type = 'ghost') AS ghosts,
                    COUNT(*) FILTER (WHERE issue_type = 'size_mismatch') AS mismatches
                FROM praesidium_reconciliation WHERE TRIM(tenant_id) = :tid
                GROUP BY run_id ORDER BY MIN(created_at) DESC LIMIT 1
            """), {"tid": tid})).mappings().fetchone()
        except Exception:
            pass

    rows_out = []
    summary = {"total_folders": total_folders, "synced": 0, "partial": 0, "unsynced": 0, "unmatched": 0}
    for m in matches:
        d = dict(m)
        lc = d.get("disk_file_count") or 0
        lc = max(0, lc - excl_counts.get(d.get("matter_id") or "", 0))
        pc = d.get("synced_file_count") or 0
        mn, cn = d.get("matter_name"), d.get("client_name")
        if mn and cn:
            pc = max(pc, prae_counts.get(f"{PRAESIDIUM_ROOT}/{tid}/matters/{cn}/{mn}", 0))
        d["praesidium_file_count"] = pc
        if not d.get("accepted"):
            st = "unmatched"
        elif pc > 0 and pc >= lc and lc > 0:
            st = "synced"
        elif pc > 0:
            st = "partial"
        else:
            st = "unsynced"
        d["sync_status"] = st
        summary[st] = summary.get(st, 0) + 1
        if filter == "all" or filter == st:
            rows_out.append(d)
    summary["unmatched"] = max(0, total_folders - len(matches)) + summary.get("unmatched", 0)
    return JSONResponse(_ser({"summary": summary, "recon": dict(recon_row) if recon_row else None, "rows": rows_out[offset:offset + limit]}))


@router.post("/sync/sync-folder")
async def api_sync_folder(request: Request, user=Depends(get_current_user)):
    body = await request.json()
    matter_id = body.get("matter_id")
    if not matter_id:
        return JSONResponse({"error": "matter_id required"}, status_code=400)
    from modules.tenant_admin.tenant_admin import folder_recon_sync_matter
    return await folder_recon_sync_matter(request, matter_id, user)


@router.post("/sync/assign")
async def api_sync_assign(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    body = await request.json()
    matter_id = body.get("matter_id"); legacy_path = body.get("legacy_path", "")
    sync_mode = body.get("sync_mode", "smart")
    new_client = body.get("new_client", ""); new_matter = body.get("new_matter", "")
    if new_client and new_matter and not matter_id:
        async with AsyncSessionLocal() as session:
            ex = (await session.execute(text("SELECT id::text FROM clients WHERE TRIM(tenant_id) = :tid AND client_name ILIKE :cn LIMIT 1"), {"tid": tid, "cn": new_client})).fetchone()
            cid = ex[0] if ex else (await session.execute(text("INSERT INTO clients (id, tenant_id, client_name) VALUES (gen_random_uuid(), :tid, :cn) RETURNING id::text"), {"tid": tid, "cn": new_client})).fetchone()[0]
            matter_id = (await session.execute(text("INSERT INTO matters (id, tenant_id, client_id, matter_name, status) VALUES (gen_random_uuid(), :tid, CAST(:cid AS uuid), :mn, 'active') RETURNING id::text"), {"tid": tid, "cid": cid, "mn": new_matter})).fetchone()[0]
            await session.commit()
    if not matter_id:
        return JSONResponse({"error": "matter_id required"}, status_code=400)
    fn = os.path.basename(legacy_path)
    async with AsyncSessionLocal() as session:
        await session.execute(text("INSERT INTO dms_folder_matches (id, tenant_id, matter_id, folder_path, best_disk_path, score, accepted, computed_at) VALUES (gen_random_uuid(), :tid, CAST(:mid AS uuid), :fp, :bp, 1.0, true, NOW()) ON CONFLICT DO NOTHING"), {"tid": tid, "mid": matter_id, "fp": fn, "bp": legacy_path})
        await session.commit()
    if sync_mode == "none":
        return JSONResponse({"status": "ok", "message": "Assigned (no sync)"})
    from modules.tenant_admin.tenant_admin import folder_recon_sync_matter
    return await folder_recon_sync_matter(request, matter_id, user)


# ═══════════════════════════════════════════════════════════════════════
# INTEGRITY TAB
# ═══════════════════════════════════════════════════════════════════════

@router.get("/integrity")
async def api_integrity(request: Request, filter: str = "all", offset: int = 0, limit: int = 100, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        recon = None
        try:
            rr = (await session.execute(text("""
                SELECT run_id::text, MIN(created_at) AS created_at,
                    COUNT(*) FILTER (WHERE issue_type = 'orphan') AS orphans,
                    COUNT(*) FILTER (WHERE issue_type = 'ghost') AS ghosts,
                    COUNT(*) FILTER (WHERE issue_type = 'size_mismatch') AS mismatches,
                    COUNT(*) AS total_issues
                FROM praesidium_reconciliation WHERE TRIM(tenant_id) = :tid
                GROUP BY run_id ORDER BY MIN(created_at) DESC LIMIT 1
            """), {"tid": tid})).mappings().fetchone()
            if rr:
                recon = dict(rr)
                vc = (await session.execute(text("SELECT COUNT(*) FROM dms_documents WHERE TRIM(tenant_id) = :tid AND verified_at IS NOT NULL"), {"tid": tid})).fetchone()
                recon["verified"] = vc[0] if vc else 0
        except Exception as e:
            logger.warning(f"Integrity recon query failed: {e}")
        issues = []
        if recon and recon.get("total_issues", 0) > 0:
            type_filter = ""
            params = {"tid": tid, "rid": recon["run_id"], "lim": limit, "off": offset}
            if filter == "orphan": type_filter = "AND pr.issue_type = 'orphan'"
            elif filter == "ghost": type_filter = "AND pr.issue_type = 'ghost'"
            elif filter == "size_mismatch": type_filter = "AND pr.issue_type = 'size_mismatch'"
            issues = (await session.execute(text(f"""
                SELECT pr.id, pr.issue_type, pr.file_path, pr.folder_root,
                       pr.file_size_bytes, pr.details, pr.db_doc_id::text
                FROM praesidium_reconciliation pr
                WHERE TRIM(pr.tenant_id) = :tid AND pr.run_id = CAST(:rid AS uuid) {type_filter}
                ORDER BY pr.issue_type, pr.file_path LIMIT :lim OFFSET :off
            """), params)).mappings().all()
    return JSONResponse(_ser({"recon": recon, "issues": [dict(i) for i in issues]}))


@router.post("/integrity/run")
async def api_integrity_run(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    try:
        result = subprocess.run(["python3", "/app/jobs/reconcile_praesidium.py", "--tenant", tid], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return JSONResponse({"status": "error", "detail": result.stderr[-500:]}, status_code=500)
        lines = result.stdout.split("\n")
        summary_parts = []
        for line in lines:
            for key in ["Verified:", "Orphans:", "Ghosts:", "Mismatches:"]:
                if key in line:
                    summary_parts.append(line.strip().split("  ")[-1].strip())
        return JSONResponse({"status": "ok", "message": "Reconciliation complete. " + " | ".join(summary_parts)})
    except subprocess.TimeoutExpired:
        return JSONResponse({"status": "error", "detail": "Timed out after 120s"}, status_code=500)
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


@router.post("/integrity/register")
async def api_integrity_register(request: Request, user=Depends(get_current_user)):
    body = await request.json()
    tid = _tid(request)
    issue_id = body.get("issue_id")
    async with AsyncSessionLocal() as session:
        issue = (await session.execute(text("SELECT file_path, file_size_bytes, file_hash, folder_root FROM praesidium_reconciliation WHERE id = :iid AND TRIM(tenant_id) = :tid AND issue_type = 'orphan'"), {"iid": issue_id, "tid": tid})).mappings().fetchone()
        if not issue:
            return JSONResponse({"status": "error", "detail": "Issue not found"}, status_code=404)
        fpath = issue["file_path"]
        ext = os.path.splitext(fpath)[1].lower()
        ocr_status = "ocr_pending" if ext in {".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"} else "text_native"
        await session.execute(text("INSERT INTO dms_documents (id, tenant_id, file_path, folder_root, file_hash, file_size_bytes, ocr_status, extraction_status, source, indexed_at, updated_at, verified_at) VALUES (gen_random_uuid(), :tid, :fp, :fr, :fh, :fs, :ocr, 'pending', 'reconciliation', NOW(), NOW(), NOW()) ON CONFLICT (tenant_id, file_path) DO UPDATE SET verified_at = NOW()"), {"tid": tid, "fp": fpath, "fr": issue["folder_root"], "fh": issue["file_hash"], "fs": issue["file_size_bytes"], "ocr": ocr_status})
        await session.execute(text("DELETE FROM praesidium_reconciliation WHERE id = :iid"), {"iid": issue_id})
        await session.commit()
    return JSONResponse({"status": "ok"})


@router.post("/integrity/remove-ghost")
async def api_integrity_remove_ghost(request: Request, user=Depends(get_current_user)):
    body = await request.json()
    tid = _tid(request)
    issue_id = body.get("issue_id")
    async with AsyncSessionLocal() as session:
        issue = (await session.execute(text("SELECT db_doc_id::text FROM praesidium_reconciliation WHERE id = :iid AND TRIM(tenant_id) = :tid AND issue_type = 'ghost'"), {"iid": issue_id, "tid": tid})).fetchone()
        if not issue or not issue[0]:
            return JSONResponse({"status": "error", "detail": "Issue not found"}, status_code=404)
        await session.execute(text("DELETE FROM dms_documents WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid"), {"did": issue[0], "tid": tid})
        await session.execute(text("DELETE FROM praesidium_reconciliation WHERE id = :iid"), {"iid": issue_id})
        await session.commit()
    return JSONResponse({"status": "ok"})

# ═══════════════════════════════════════════════════════════════════════
# IMPORT STORE TAB (integrity sub-view)
# ═══════════════════════════════════════════════════════════════════════

LEGACY_ROOTS = [
    # "/mnt/clients",                              # deprecated — all data under /mnt/legacy
    # "/mnt/docsend",                              # deprecated — all data under /mnt/legacy
    # "/mnt/legacy-qnap/D Drive Backup/Clients",   # deprecated — all data under /mnt/legacy
    # "/mnt/legacy-qnap/D Drive Backup/Docsend",   # deprecated — all data under /mnt/legacy
    "/mnt/legacy",
]


def _resolve_source(best_disk_path, disk_root=None, source_mount=None):
    """Resolve a best_disk_path to an accessible absolute path."""
    if not best_disk_path:
        return ""
    # Already absolute and accessible
    if os.path.isabs(best_disk_path) and os.path.isdir(best_disk_path):
        return best_disk_path
    # Try source mount from connector_sources
    if source_mount:
        candidate = os.path.join(source_mount, best_disk_path)
        if os.path.isdir(candidate):
            return candidate
    # Try disk_root + basename
    if disk_root and os.path.isabs(disk_root):
        candidate = os.path.join(disk_root, os.path.basename(best_disk_path))
        if os.path.isdir(candidate):
            return candidate
    # Try common legacy roots for relative paths
    for root in LEGACY_ROOTS:
        candidate = os.path.join(root, best_disk_path)
        if os.path.isdir(candidate):
            return candidate
    return best_disk_path  # return as-is, will show as inaccessible


def _scan_folder_pair(source_path, prae_path):
    """Walk a source and praesidium path, return comparison stats."""
    source_files = set()
    if source_path and os.path.isdir(source_path):
        for dirpath, dirnames, filenames in os.walk(source_path):
            dirnames[:] = [dn for dn in dirnames if not dn.startswith('.') and not dn.startswith('@')]
            for fn in filenames:
                if not fn.startswith('.'):
                    source_files.add(os.path.relpath(os.path.join(dirpath, fn), source_path))

    prae_files = set()
    if prae_path and os.path.isdir(prae_path):
        for dirpath, dirnames, filenames in os.walk(prae_path):
            dirnames[:] = [dn for dn in dirnames if not dn.startswith('.')]
            for fn in filenames:
                if not fn.startswith('.'):
                    prae_files.add(os.path.relpath(os.path.join(dirpath, fn), prae_path))

    source_basenames = {}
    for rf in source_files:
        source_basenames.setdefault(os.path.basename(rf), []).append(rf)
    prae_basenames = {}
    for rf in prae_files:
        prae_basenames.setdefault(os.path.basename(rf), []).append(rf)

    imported_keys = set(source_basenames.keys()) & set(prae_basenames.keys())
    missing_keys = set(source_basenames.keys()) - set(prae_basenames.keys())
    extra_keys = set(prae_basenames.keys()) - set(source_basenames.keys())

    src_count = len(source_files)
    imported_count = sum(len(source_basenames[b]) for b in imported_keys)
    missing_count = sum(len(source_basenames[b]) for b in missing_keys)
    extra_count = sum(len(prae_basenames[b]) for b in extra_keys)

    return {
        "source_file_count": src_count, "prae_file_count": len(prae_files),
        "imported_count": imported_count, "missing_count": missing_count,
        "extra_count": extra_count,
        "missing_files": sorted(list(missing_keys))[:50],
        "extra_files": sorted(list(extra_keys))[:50],
    }


@router.get("/integrity/import-store")
async def api_import_store(request: Request, offset: int = 0, limit: int = 100, user=Depends(get_current_user)):
    """For each accepted folder match, compare source files vs Praesidium at file level."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        matches = (await session.execute(text("""
            SELECT fm.id::text, fm.folder_path, fm.best_disk_path, fm.disk_file_count,
                   fm.synced_at, fm.synced_file_count, fm.disk_root, fm.source_id::text,
                   m.id::text AS matter_id, m.matter_name, c.client_name
            FROM dms_folder_matches fm
            LEFT JOIN matters m ON fm.matter_id = m.id
            LEFT JOIN clients c ON m.client_id = c.id
            WHERE TRIM(fm.tenant_id) = :tid AND fm.accepted = true
            ORDER BY fm.folder_path
        """), {"tid": tid})).mappings().all()

        # Build source_id -> mount_path lookup
        sm_rows = (await session.execute(text(
            "SELECT id::text, mount_path FROM connector_sources WHERE TRIM(tenant_id) = :tid"
        ), {"tid": tid})).mappings().all()
        source_mounts = {sr["id"]: sr["mount_path"] for sr in sm_rows}

    results = []
    totals = {"total_matches": 0, "fully_imported": 0, "partially_imported": 0,
              "not_imported": 0, "total_source_files": 0, "total_imported_files": 0,
              "total_missing_files": 0, "total_extra_files": 0}

    for m in matches:
        d = dict(m)
        raw_path = d.get("best_disk_path") or ""
        matter_name = d.get("matter_name") or ""
        client_name = d.get("client_name") or ""
        sm = source_mounts.get(d.get("source_id") or "", "")
        source_path = _resolve_source(raw_path, d.get("disk_root"), sm)
        prae_path = os.path.join(PRAESIDIUM_ROOT, tid, "matters", client_name, matter_name) if client_name and matter_name else ""

        scan = _scan_folder_pair(source_path, prae_path)

        src_count = scan["source_file_count"]
        if src_count == 0:
            status = "empty_source"
        elif scan["missing_count"] == 0 and src_count > 0:
            status = "complete"
        elif scan["imported_count"] > 0:
            status = "partial"
        else:
            status = "not_imported"

        d["source_accessible"] = bool(source_path and os.path.isdir(source_path))
        d["prae_accessible"] = bool(prae_path and os.path.isdir(prae_path))
        d["resolved_source_path"] = source_path
        d["prae_path"] = prae_path
        d["import_status"] = status
        d.update(scan)

        totals["total_matches"] += 1
        totals["total_source_files"] += src_count
        totals["total_imported_files"] += scan["imported_count"]
        totals["total_missing_files"] += scan["missing_count"]
        totals["total_extra_files"] += scan["extra_count"]
        if status == "complete": totals["fully_imported"] += 1
        elif status == "partial": totals["partially_imported"] += 1
        elif status == "not_imported": totals["not_imported"] += 1

        results.append(d)

    return JSONResponse(_ser({
        "totals": totals,
        "rows": results[offset:offset + limit],
        "total_rows": len(results),
    }))


@router.get("/integrity/import-store/stream")
async def api_import_store_stream(request: Request, user=Depends(get_current_user)):
    """SSE endpoint that streams per-folder progress during import store scan."""
    tid = _tid(request)

    async def generate():
        async with AsyncSessionLocal() as session:
            matches = (await session.execute(text("""
                SELECT fm.id::text, fm.folder_path, fm.best_disk_path, fm.disk_file_count,
                       fm.synced_at, fm.synced_file_count, fm.disk_root, fm.source_id::text,
                       m.id::text AS matter_id, m.matter_name, c.client_name
                FROM dms_folder_matches fm
                LEFT JOIN matters m ON fm.matter_id = m.id
                LEFT JOIN clients c ON m.client_id = c.id
                WHERE TRIM(fm.tenant_id) = :tid AND fm.accepted = true
                ORDER BY fm.folder_path
            """), {"tid": tid})).mappings().all()

            sm_rows = (await session.execute(text(
                "SELECT id::text, mount_path FROM connector_sources WHERE TRIM(tenant_id) = :tid"
            ), {"tid": tid})).mappings().all()
            source_mounts = {sr["id"]: sr["mount_path"] for sr in sm_rows}

        total = len(matches)
        yield f"data: {json.dumps({'type':'start','total':total})}\n\n"

        results = []
        totals = {"total_matches": 0, "fully_imported": 0, "partially_imported": 0,
                  "not_imported": 0, "total_source_files": 0, "total_imported_files": 0,
                  "total_missing_files": 0, "total_extra_files": 0}

        for idx, m in enumerate(matches):
            d = dict(m)
            raw_path = d.get("best_disk_path") or ""
            matter_name = d.get("matter_name") or ""
            client_name = d.get("client_name") or ""
            sm = source_mounts.get(d.get("source_id") or "", "")
            source_path = _resolve_source(raw_path, d.get("disk_root"), sm)
            prae_path = os.path.join(PRAESIDIUM_ROOT, tid, "matters", client_name, matter_name) if client_name and matter_name else ""

            # Send progress every row
            yield f"data: {json.dumps({'type':'progress','current':idx+1,'total':total,'folder':d.get('folder_path') or raw_path or matter_name})}\n\n"

            scan = _scan_folder_pair(source_path, prae_path)
            src_count = scan["source_file_count"]
            if src_count == 0: status = "empty_source"
            elif scan["missing_count"] == 0 and src_count > 0: status = "complete"
            elif scan["imported_count"] > 0: status = "partial"
            else: status = "not_imported"

            d["source_accessible"] = bool(source_path and os.path.isdir(source_path))
            d["prae_accessible"] = bool(prae_path and os.path.isdir(prae_path))
            d["resolved_source_path"] = source_path
            d["prae_path"] = prae_path
            d["import_status"] = status
            d.update(scan)

            totals["total_matches"] += 1
            totals["total_source_files"] += src_count
            totals["total_imported_files"] += scan["imported_count"]
            totals["total_missing_files"] += scan["missing_count"]
            totals["total_extra_files"] += scan["extra_count"]
            if status == "complete": totals["fully_imported"] += 1
            elif status == "partial": totals["partially_imported"] += 1
            elif status == "not_imported": totals["not_imported"] += 1

            results.append(d)

        yield f"data: {json.dumps({'type':'done','totals':_ser(totals),'rows':_ser(results),'total_rows':len(results)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



@router.post("/integrity/import-store/seed-folder")
async def api_import_store_seed(request: Request, user=Depends(get_current_user)):
    """Create Praesidium folder structure for a single matter."""
    tid = _tid(request)
    body = await request.json()
    matter_id = body.get("matter_id", "")
    if not matter_id:
        return JSONResponse({"error": "matter_id required"}, status_code=400)
    try:
        from modules.dms.jobs.folder_seeder import seed_matter_folders
        result = seed_matter_folders(tid, matter_id)
        return JSONResponse(_ser({"status": "ok", "result": result}))
    except Exception as e:
        logger.exception("seed-folder failed")
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


@router.post("/integrity/import-store/import-files")
async def api_import_store_import(request: Request, user=Depends(get_current_user)):
    """Sync files from source to Praesidium for a single matter."""
    body = await request.json()
    matter_id = body.get("matter_id", "")
    if not matter_id:
        return JSONResponse({"error": "matter_id required"}, status_code=400)
    try:
        from modules.tenant_admin.tenant_admin import folder_recon_sync_matter
        return await folder_recon_sync_matter(request, matter_id, user)
    except Exception as e:
        logger.exception("import-files failed")
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


@router.post("/integrity/import-store/bulk-seed")
async def api_import_store_bulk_seed(request: Request, user=Depends(get_current_user)):
    """Create Praesidium folders for all accepted matches that are missing them."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT fm.matter_id::text
            FROM dms_folder_matches fm
            LEFT JOIN matters m ON fm.matter_id = m.id
            LEFT JOIN clients c ON m.client_id = c.id
            WHERE TRIM(fm.tenant_id) = :tid AND fm.accepted = true
              AND m.matter_name IS NOT NULL AND c.client_name IS NOT NULL
        """), {"tid": tid})).all()

    from modules.dms.jobs.folder_seeder import seed_matter_folders
    created = 0; existed = 0; errors = 0
    for r in rows:
        mid = r[0]
        try:
            result = seed_matter_folders(tid, mid)
            created += result.get("created", 0)
            existed += result.get("existing", 0)
            errors += result.get("errors", 0)
        except Exception:
            errors += 1

    return JSONResponse({"status": "ok", "matters": len(rows),
                         "folders_created": created, "folders_existed": existed, "errors": errors})


@router.get("/integrity/import-store/unmatched-sources")
async def api_import_store_unmatched(request: Request, user=Depends(get_current_user)):
    """Return source folders that have no accepted folder match."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        sources = (await session.execute(text("""
            SELECT id::text, source_name, mount_path
            FROM connector_sources
            WHERE TRIM(tenant_id) = :tid AND status = 'active'
        """), {"tid": tid})).mappings().all()

        matched_paths = set()
        mrows = (await session.execute(text("""
            SELECT best_disk_path FROM dms_folder_matches
            WHERE TRIM(tenant_id) = :tid AND accepted = true AND best_disk_path IS NOT NULL
        """), {"tid": tid})).all()
        for mr in mrows:
            if mr[0]:
                matched_paths.add(mr[0])

    unmatched = []
    for src in sources:
        mount = src["mount_path"]
        if not os.path.isdir(mount):
            continue
        try:
            for entry in sorted(os.scandir(mount), key=lambda e: e.name.lower()):
                if entry.name.startswith('.') or entry.name.startswith('@'):
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
                full = entry.path
                if full in matched_paths or any(full.startswith(mp) or mp.startswith(full) for mp in matched_paths):
                    continue
                try:
                    fc = sum(1 for f in os.scandir(full) if f.is_file() and not f.name.startswith('.'))
                except (PermissionError, OSError):
                    fc = 0
                unmatched.append({
                    "source_name": src["source_name"],
                    "source_id": src["id"],
                    "folder_name": entry.name,
                    "folder_path": full,
                    "file_count": fc,
                })
        except (PermissionError, OSError):
            pass

    return JSONResponse(_ser(unmatched))


# ═══════════════════════════════════════════════════════════════════════
# LEGACY FILE BROWSE — same shape as DMS tree/files API
# ═══════════════════════════════════════════════════════════════════════

LEGACY_MOUNT = "/mnt/legacy"

def _legacy_walk_tree(root, max_depth=5):
    """Walk directory tree, return same shape as DMS _walk_tree."""
    result = []
    if not os.path.isdir(root):
        return result
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name.lower())
    except PermissionError:
        return result
    for entry in entries:
        if entry.name.startswith('.') or entry.name.startswith('@'):
            continue
        if entry.is_dir(follow_symlinks=True):
            children = _legacy_walk_tree(entry.path, max_depth - 1) if max_depth > 1 else []
            try:
                fc = sum(1 for f in os.scandir(entry.path) if f.is_file() and not f.name.startswith('.'))
            except (PermissionError, OSError):
                fc = 0
            result.append({
                "name": entry.name,
                "path": os.path.relpath(entry.path, LEGACY_MOUNT),
                "file_count": fc,
                "children": children,
            })
    return result


def _legacy_list_files(dir_path):
    """List files in a directory, return same shape as DMS _list_files."""
    if not os.path.isdir(dir_path):
        return []
    files = []
    try:
        for entry in sorted(os.scandir(dir_path), key=lambda e: e.name.lower()):
            if entry.name.startswith('.') or not entry.is_file(follow_symlinks=True):
                continue
            try:
                st = entry.stat()
                size = st.st_size
                mt = st.st_mtime
            except (PermissionError, OSError):
                size = mt = 0
            ext = entry.name.rsplit('.', 1)[-1].lower() if '.' in entry.name else ''
            sz = size
            if sz < 1024: sfmt = f"{sz} B"
            elif sz < 1048576: sfmt = f"{sz/1024:.1f} KB"
            elif sz < 1073741824: sfmt = f"{sz/1048576:.1f} MB"
            else: sfmt = f"{sz/1073741824:.1f} GB"
            files.append({"name": entry.name, "size": size, "size_fmt": sfmt, "modified": int(mt), "ext": ext})
    except PermissionError:
        pass
    return files


@router.get("/legacy/tree")
async def api_legacy_tree(request: Request, path: str = "", user=Depends(get_current_user)):
    """Return one level of folders from /mnt/legacy/path. Lazy-load — no deep walk."""
    target = os.path.join(LEGACY_MOUNT, path) if path else LEGACY_MOUNT
    target = os.path.realpath(target)
    if not target.startswith(os.path.realpath(LEGACY_MOUNT)):
        return JSONResponse({"tree": [], "root": None, "root_file_count": 0}, status_code=403)
    if not os.path.isdir(target):
        logger.warning("Legacy tree: %s not a directory", target)
        return JSONResponse({"tree": [], "root": None, "root_file_count": 0})
    tree = []
    rfc = 0
    try:
        for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
            if entry.name.startswith('.') or entry.name.startswith('@'):
                continue
            if entry.is_dir(follow_symlinks=True):
                try:
                    fc = sum(1 for f in os.scandir(entry.path) if f.is_file() and not f.name.startswith('.'))
                    has_children = any(f.is_dir() for f in os.scandir(entry.path) if not f.name.startswith('.') and not f.name.startswith('@'))
                except (PermissionError, OSError):
                    fc = 0
                    has_children = False
                tree.append({
                    "name": entry.name,
                    "path": os.path.relpath(entry.path, LEGACY_MOUNT),
                    "file_count": fc,
                    "children": [],
                    "has_children": has_children,
                })
            elif entry.is_file():
                rfc += 1
    except (PermissionError, OSError) as e:
        logger.error("Legacy tree scan error: %s", e)
    return JSONResponse({"tree": tree, "root": LEGACY_MOUNT, "root_file_count": rfc})


@router.get("/legacy/search")
async def api_legacy_search(request: Request, q: str = "", user=Depends(get_current_user)):
    """Search legacy folders by name. Scans top-level dirs under /mnt/legacy
    and one level inside each (e.g. Clients/FolderName). Returns matching folders."""
    if not q or len(q) < 2:
        return JSONResponse({"results": []})
    q_lower = q.lower()
    results = []
    if not os.path.isdir(LEGACY_MOUNT):
        return JSONResponse({"results": []})
    try:
        for top in sorted(os.scandir(LEGACY_MOUNT), key=lambda e: e.name.lower()):
            if top.name.startswith('.') or top.name.startswith('@') or not top.is_dir(follow_symlinks=True):
                continue
            # Check top-level match
            if q_lower in top.name.lower():
                try:
                    fc = sum(1 for f in os.scandir(top.path) if f.is_file() and not f.name.startswith('.'))
                except (PermissionError, OSError):
                    fc = 0
                results.append({"name": top.name, "path": os.path.relpath(top.path, LEGACY_MOUNT), "file_count": fc, "parent": ""})
            # Search one level inside
            try:
                for sub in os.scandir(top.path):
                    if sub.name.startswith('.') or sub.name.startswith('@') or not sub.is_dir(follow_symlinks=True):
                        continue
                    if q_lower in sub.name.lower():
                        try:
                            fc = sum(1 for f in os.scandir(sub.path) if f.is_file() and not f.name.startswith('.'))
                        except (PermissionError, OSError):
                            fc = 0
                        results.append({"name": sub.name, "path": os.path.relpath(sub.path, LEGACY_MOUNT), "file_count": fc, "parent": top.name})
            except (PermissionError, OSError):
                pass
            if len(results) >= 50:
                break
    except (PermissionError, OSError) as e:
        logger.error("Legacy search error: %s", e)
    return JSONResponse({"results": results[:50]})


@router.get("/legacy/files")
async def api_legacy_files(request: Request, path: str = "", user=Depends(get_current_user)):
    """Return file list from /mnt/legacy/path, same shape as DMS files API."""
    target = os.path.join(LEGACY_MOUNT, path) if path else LEGACY_MOUNT
    target = os.path.realpath(target)
    if not target.startswith(os.path.realpath(LEGACY_MOUNT)):
        return JSONResponse({"files": [], "folder_name": ""}, status_code=403)
    if not os.path.isdir(target):
        return JSONResponse({"files": [], "folder_name": ""})
    files = _legacy_list_files(target)
    return JSONResponse({"files": files, "folder_name": os.path.basename(target) if path else "Legacy Root", "path": path})


# ═════════════════════════════════════════════════════════════════════════════
# ONBOARDING CLUSTERS (C2) — exclusions surfaced per matter, one-click
# collection creation for production/client-document clusters.
# ═════════════════════════════════════════════════════════════════════════════

_CLUSTER_SOURCE_TYPE = {
    "production": "opposing_production",
    "client_documents": "client_documents",
    "pst_mailstore": "client_collection_dedicated",
    "forensic_image": "internal_collection",
}


@router.get("/clusters")
async def api_clusters_list(request: Request, status: str = "", user=Depends(get_current_user)):
    tid = _tid(request)
    where = "TRIM(oc.tenant_id) = :tid"
    params = {"tid": tid}
    if status:
        where += " AND oc.status = :st"
        params["st"] = status
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(f"""
            SELECT oc.id::text, oc.matter_id::text, oc.root_path, oc.cluster_type,
                   oc.detected_by, oc.evidence, oc.file_count, oc.total_bytes,
                   oc.status, oc.collection_id::text, oc.updated_at,
                   m.matter_name, c.client_name
            FROM onboarding_clusters oc
            LEFT JOIN matters m ON m.id = oc.matter_id
            LEFT JOIN clients c ON c.id = m.client_id
            WHERE {where}
            ORDER BY oc.file_count DESC
        """), params)).mappings().all()
        summary = (await session.execute(text("""
            SELECT COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                   COUNT(*) FILTER (WHERE status = 'ingested') AS ingested,
                   COUNT(*) FILTER (WHERE status = 'dismissed') AS dismissed,
                   COALESCE(SUM(file_count) FILTER (WHERE status = 'pending'), 0) AS pending_files,
                   COALESCE(SUM(total_bytes) FILTER (WHERE status = 'pending'), 0) AS pending_bytes
            FROM onboarding_clusters WHERE TRIM(tenant_id) = :tid
        """), {"tid": tid})).mappings().fetchone()
    return JSONResponse(_ser({"rows": [dict(r) for r in rows],
                              "summary": dict(summary) if summary else {}}))


@router.post("/clusters/{cluster_id}/status")
async def api_cluster_set_status(request: Request, cluster_id: str, user=Depends(get_current_user)):
    tid = _tid(request)
    body = await request.json()
    new_status = (body.get("status") or "").strip()
    if new_status not in ("pending", "dismissed"):
        return JSONResponse({"error": "status must be pending|dismissed"}, status_code=400)
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("""
            UPDATE onboarding_clusters SET status = :st, updated_at = NOW()
            WHERE id = CAST(:cid AS uuid) AND TRIM(tenant_id) = :tid
              AND status <> 'ingested'
        """), {"st": new_status, "cid": cluster_id, "tid": tid})
        await session.commit()
    if res.rowcount == 0:
        return JSONResponse({"error": "not found or already ingested"}, status_code=404)
    return JSONResponse({"status": new_status})


@router.post("/clusters/{cluster_id}/create-collection")
async def api_cluster_create_collection(request: Request, cluster_id: str, user=Depends(get_current_user)):
    """One-click: cluster row -> eDiscovery collection -> run_collection_full."""
    tid = _tid(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    async with AsyncSessionLocal() as session:
        cl = (await session.execute(text("""
            SELECT oc.id::text, oc.matter_id::text, oc.root_path, oc.cluster_type,
                   oc.status, m.matter_name
            FROM onboarding_clusters oc
            LEFT JOIN matters m ON m.id = oc.matter_id
            WHERE oc.id = CAST(:cid AS uuid) AND TRIM(oc.tenant_id) = :tid
        """), {"cid": cluster_id, "tid": tid})).mappings().fetchone()
    if not cl:
        return JSONResponse({"error": "cluster not found"}, status_code=404)
    if cl["status"] == "ingested":
        return JSONResponse({"error": "already ingested"}, status_code=409)
    if not cl["matter_id"]:
        return JSONResponse({"error": "cluster has no matter"}, status_code=400)
    if not os.path.exists(cl["root_path"]):
        return JSONResponse({"error": f"source path not found: {cl['root_path']}"}, status_code=400)

    leaf = os.path.basename(cl["root_path"].rstrip("/")) or "Onboarding"
    collection_name = (body.get("collection_name") or "").strip() or f"{leaf} (onboarding)"
    source_type = body.get("source_type") or _CLUSTER_SOURCE_TYPE.get(cl["cluster_type"], "opposing_production")

    import uuid as _u
    collection_id = str(_u.uuid4())
    ediscovery_root = os.environ.get("EDISCOVERY_STORAGE_ROOT", "/mnt/ediscovery")
    storage_path = os.path.join(ediscovery_root, tid, cl["matter_id"], collection_name.replace(" ", "_"))
    user_id = None
    try:
        user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    except Exception:
        pass

    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            INSERT INTO ediscovery_collections
                (id, tenant_id, matter_id, collection_name, storage_path,
                 source_type, source_party, dms_source_path, status, received_by)
            VALUES
                (CAST(:cid AS uuid), :tid, CAST(:mid AS uuid), :name, :spath,
                 :stype, NULL, :dms_path, 'collecting', :uid)
        """), {"cid": collection_id, "tid": tid, "mid": cl["matter_id"],
               "name": collection_name, "spath": storage_path,
               "stype": source_type, "dms_path": cl["root_path"], "uid": user_id})
        await session.execute(text("""
            UPDATE onboarding_clusters
            SET status = 'ingested', collection_id = CAST(:colid AS uuid), updated_at = NOW()
            WHERE id = CAST(:clid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"colid": collection_id, "clid": cluster_id, "tid": tid})
        await session.commit()

    try:
        from redis import Redis
        from rq import Queue
        REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
        q = Queue("ediscovery", connection=Redis.from_url(REDIS_URL))
        q.enqueue(
            "modules.ediscovery.jobs.ledger_dag.run_collection_full",
            tid, collection_id, user_id,
            spine_workers=6, ocr_workers=2, embed_workers=1,
            job_timeout="24h", result_ttl=3600,
        )
    except Exception as e:
        logger.error("cluster create-collection enqueue failed: %s", e)
        return JSONResponse({"id": collection_id, "status": "created_no_job", "error": str(e)})
    return JSONResponse({"id": collection_id, "status": "collecting",
                         "collection_name": collection_name, "source_type": source_type})

# ONCONFLICT_FPM_V1 — dms_folder_matches upserts target (tenant_id, folder_path, matter_id)
