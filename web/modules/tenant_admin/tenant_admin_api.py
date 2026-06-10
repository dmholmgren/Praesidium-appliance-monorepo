"""
modules/tenant_admin/tenant_admin_api.py
JSON API for the React tenant admin page.

DMS pipeline: scan → extract_fast → extract_ocr → parse+analyze
  scan:         register disk files into documents table
  extract_fast: text extraction from native formats (pdf, docx, txt, etc.)
  extract_ocr:  OCR extraction from image files (jpg, png, tif) — multiprocess
  parse:        copy content_text → documents.extracted_text + push to Elasticsearch

Auto-chaining: extract_fast → extract_ocr → parse (each stage auto-enqueues next).
Manual /dms/pipeline endpoint runs the full scan → extract → parse chain.

All are RQ background jobs tracked in dms_scan_jobs, polled by React UI.
"""
import json
import logging
import os
import uuid as _uuid
from datetime import datetime, date
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/tenant-admin", tags=["tenant-admin-api"])


def _tid(request):
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _ser(obj):
    if obj is None: return None
    if isinstance(obj, dict): return {k: _ser(v) for k, v in obj.items()}
    if isinstance(obj, list): return [_ser(v) for v in obj]
    if isinstance(obj, _uuid.UUID): return str(obj)
    if isinstance(obj, (datetime, date)): return obj.isoformat()
    if isinstance(obj, Decimal): return float(obj)
    if isinstance(obj, bytes): return obj.decode("utf-8", errors="replace")
    return obj


def _get_rq_queue():
    from redis import Redis
    from rq import Queue
    redis_url = os.environ.get("REDIS_URL", "redis://praesidium-redis:6379/0")
    parts = redis_url.replace("redis://", "").split("/")
    host_port = parts[0].split(":")
    host = host_port[0]
    port = int(host_port[1]) if len(host_port) > 1 else 6379
    db = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    conn = Redis(host=host, port=port, db=db)
    return Queue("default", connection=conn)


@router.get("/stats")
async def api_stats(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        uc = (await session.execute(text("SELECT COUNT(*) FROM users WHERE tenant_id=:tid AND is_active=true"), {"tid": tid})).scalar() or 0
        mc = (await session.execute(text("SELECT COUNT(*) FROM matters WHERE trim(tenant_id)=trim(:tid)"), {"tid": tid})).scalar() or 0
        dc = (await session.execute(text("SELECT COUNT(*) FROM dms_documents WHERE trim(tenant_id)=trim(:tid)"), {"tid": tid})).scalar() or 0
        cc = (await session.execute(text("SELECT COUNT(*) FROM ediscovery_collections WHERE trim(tenant_id)=trim(:tid)"), {"tid": tid})).scalar() or 0
        try:
            from modules.dms.jobs.dms_disk_scanner import get_unindexed_count
            dms_stats = await get_unindexed_count(tid, session)
            disk_files = dms_stats.get("disk_files", 0)
            unindexed = dms_stats.get("unindexed_files", 0)
        except Exception:
            disk_files = 0
            unindexed = 0
        doc_registered = (await session.execute(text(
            "SELECT COUNT(*) FROM documents WHERE TRIM(tenant_id)=:tid AND storage_path LIKE '/mnt/praesidium%%'"
        ), {"tid": tid})).scalar() or 0
        # Text extracted in dms_documents
        extracted = (await session.execute(text(
            "SELECT COUNT(*) FROM dms_documents WHERE TRIM(tenant_id)=:tid "
            "AND content_text IS NOT NULL AND content_text != ''"
        ), {"tid": tid})).scalar() or 0
        not_extracted = (await session.execute(text(
            "SELECT COUNT(*) FROM dms_documents WHERE TRIM(tenant_id)=:tid "
            "AND (content_text IS NULL OR content_text = '') AND extraction_status = 'pending'"
        ), {"tid": tid})).scalar() or 0
        # Parsed into documents table
        parsed = (await session.execute(text(
            "SELECT COUNT(*) FROM documents WHERE TRIM(tenant_id)=:tid "
            "AND storage_path LIKE '/mnt/praesidium%%' "
            "AND extracted_text IS NOT NULL AND extracted_text != ''"
        ), {"tid": tid})).scalar() or 0
        unparsed = (await session.execute(text(
            "SELECT COUNT(*) FROM documents WHERE TRIM(tenant_id)=:tid "
            "AND storage_path LIKE '/mnt/praesidium%%' "
            "AND (extracted_text IS NULL OR extracted_text = '')"
        ), {"tid": tid})).scalar() or 0
    # ES indexed count
    es_indexed = 0
    try:
        from elasticsearch import Elasticsearch
        _es = Elasticsearch([os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch:9200")], request_timeout=5)
        if _es.indices.exists(index="praesidium_documents"):
            _cnt = _es.count(index="praesidium_documents", body={"query": {"term": {"tenant_id": tid}}})
            es_indexed = _cnt.get("count", 0)
    except Exception:
        pass
    return JSONResponse({
        "user_count": uc, "matter_count": mc, "doc_count": dc,
        "collection_count": cc, "disk_files": disk_files,
        "unindexed_files": unindexed, "doc_registered": doc_registered,
        "extracted": extracted, "not_extracted": not_extracted,
        "parsed_files": parsed, "unparsed_files": unparsed,
        "es_indexed": es_indexed,
    })


@router.get("/users")
async def api_users(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(text(
            "SELECT id, email, role, is_active, created_at, auth_provider "
            "FROM users WHERE tenant_id = :tid ORDER BY created_at"
        ), {"tid": tid})
        users = [dict(row) for row in r.mappings().fetchall()]
    return JSONResponse(_ser({"users": users}))


@router.get("/byok")
async def api_byok(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(text(
            "SELECT provider, key_hint FROM credentials_vault "
            "WHERE tenant_id = :tid AND key_type = 'api_key' ORDER BY provider"
        ), {"tid": tid})
        keys = [dict(row) for row in r.mappings().fetchall()]
    return JSONResponse({"keys": keys})


@router.get("/features")
async def api_features(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(text("SELECT feature_flags FROM tenants WHERE id = :tid"), {"tid": tid})
        row = r.fetchone()
        flags = row[0] if row and row[0] else {}
    features = [{"feature_key": k, "enabled": v} for k, v in sorted(flags.items())]
    return JSONResponse({"features": features})


@router.get("/profile")
async def api_profile(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(text(
            "SELECT platform_name, platform_short_name, suppress_attribution "
            "FROM tenant_branding WHERE tenant_id = :tid"
        ), {"tid": tid})
        row = r.mappings().fetchone()
    if not row:
        return JSONResponse({"platform_name": "", "platform_short_name": "", "suppress_attribution": False})
    return JSONResponse(_ser(dict(row)))


@router.get("/connectors")
async def api_connectors(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        reg = await session.execute(text("""
            SELECT connector_type, display_name, description, icon, sync_type,
                   config_fields, credential_fields, connector_group, is_active, display_order
            FROM connector_registry WHERE is_active = true
            ORDER BY display_order, display_name
        """))
        registry = []
        for row in reg.mappings().fetchall():
            d = dict(row)
            for col in ("config_fields", "credential_fields"):
                v = d.get(col)
                if isinstance(v, str):
                    try: d[col] = json.loads(v)
                    except: d[col] = []
                elif v is None:
                    d[col] = []
            registry.append(d)
        tc = await session.execute(text("""
            SELECT connector, is_active, last_sync_at, status
            FROM tenant_connectors WHERE trim(tenant_id) = trim(:tid)
        """), {"tid": tid})
        tenant_map = {}
        for row in tc.mappings().fetchall():
            tenant_map[row["connector"]] = _ser(dict(row))
    return JSONResponse({"registry": _ser(registry), "tenant_map": tenant_map})


@router.get("/connectors/{connector_type}/config")
async def api_connector_config(request: Request, connector_type: str, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(text("""
            SELECT config, is_active FROM tenant_connectors
            WHERE trim(tenant_id) = trim(:tid) AND connector = :ct LIMIT 1
        """), {"tid": tid, "ct": connector_type})
        row = r.mappings().fetchone()
        if not row:
            return JSONResponse({"config": {}})
        config = row["config"] or {}
        if isinstance(config, str):
            config = json.loads(config)
        creds = await session.execute(text("""
            SELECT key_type FROM credentials_vault
            WHERE trim(tenant_id) = trim(:tid) AND provider = :ct
        """), {"tid": tid, "ct": connector_type})
        for cr in creds.mappings().fetchall():
            config[cr["key_type"]] = "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"
    return JSONResponse(_ser({"config": config, "is_active": row["is_active"]}))


@router.get("/email/config")
async def api_email_config(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(text(
            "SELECT config, is_active FROM tenant_connectors "
            "WHERE trim(tenant_id) = trim(:tid) AND connector = 'smtp_relay' LIMIT 1"
        ), {"tid": tid})
        row = r.mappings().fetchone()
        if not row:
            return JSONResponse({})
        config = row["config"] or {}
        if isinstance(config, str):
            config = json.loads(config)
        r2 = await session.execute(text(
            "SELECT key_type FROM credentials_vault "
            "WHERE trim(tenant_id) = trim(:tid) AND provider = 'smtp_relay'"
        ), {"tid": tid})
        for cr in r2.mappings().fetchall():
            if cr["key_type"] == "relay_username": config["has_username"] = True
            if cr["key_type"] == "relay_password": config["has_password"] = True
        config["is_active"] = row["is_active"]
    return JSONResponse(_ser(config))


# ═══════════════════════════════════════════════════════════════════════════════
# DMS Background Jobs — Two-Pass Extraction Pipeline
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/dms/unindexed-count")
async def api_dms_unindexed_count(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        from modules.dms.jobs.dms_disk_scanner import get_unindexed_count
        stats = await get_unindexed_count(tid, session)
    return JSONResponse(stats)


@router.post("/dms/scan")
async def api_dms_scan(request: Request, user=Depends(get_current_user)):
    """Enqueue a DMS disk scan as an RQ background job."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        running = (await session.execute(text(
            "SELECT id FROM dms_scan_jobs "
            "WHERE TRIM(tenant_id)=:tid AND job_type='scan' AND status IN ('queued','running') "
            "LIMIT 1"
        ), {"tid": tid})).fetchone()
        if running:
            return JSONResponse({"status": "already_running", "job_id": str(running[0])}, status_code=409)
        job_id = str(_uuid.uuid4())
        await session.execute(text("""
            INSERT INTO dms_scan_jobs (id, tenant_id, job_type, status, queued_at)
            VALUES (CAST(:jid AS uuid), :tid, 'scan', 'queued', NOW())
        """), {"jid": job_id, "tid": tid})
        await session.commit()
    q = _get_rq_queue()
    q.enqueue("jobs.dms_scan_job.run_scan", job_id, job_timeout=7200, result_ttl=86400)
    return JSONResponse({"status": "queued", "job_id": job_id})


@router.post("/dms/extract")
async def api_dms_extract(request: Request, user=Depends(get_current_user)):
    """Enqueue two-pass text extraction: fast (native) then OCR (images).
    Auto-chains: extract_fast → extract_ocr → parse_analyze."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        running = (await session.execute(text(
            "SELECT id FROM dms_scan_jobs "
            "WHERE TRIM(tenant_id)=:tid AND job_type IN ('extract', 'extract_fast') "
            "AND status IN ('queued','running') LIMIT 1"
        ), {"tid": tid})).fetchone()
        if running:
            return JSONResponse({"status": "already_running", "job_id": str(running[0])}, status_code=409)
        job_id = str(_uuid.uuid4())
        await session.execute(text("""
            INSERT INTO dms_scan_jobs (id, tenant_id, job_type, status, queued_at)
            VALUES (CAST(:jid AS uuid), :tid, 'extract_fast', 'queued', NOW())
        """), {"jid": job_id, "tid": tid})
        await session.commit()
    q = _get_rq_queue()
    q.enqueue("jobs.dms_extract_job.run_extract_fast", job_id, job_timeout=7200, result_ttl=86400)
    return JSONResponse({"status": "queued", "job_id": job_id, "pipeline": "extract_fast → extract_ocr → parse"})



@router.post("/dms/ocr")
async def api_dms_ocr(request: Request, user=Depends(get_current_user)):
    """Enqueue OCR extraction for image files (jpg, png, tif). Auto-chains parse on completion."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        running = (await session.execute(text(
            "SELECT id FROM dms_scan_jobs "
            "WHERE TRIM(tenant_id)=:tid AND job_type='extract_ocr' AND status IN ('queued','running') "
            "LIMIT 1"
        ), {"tid": tid})).fetchone()
        if running:
            return JSONResponse({"status": "already_running", "job_id": str(running[0])}, status_code=409)
        job_id = str(_uuid.uuid4())
        await session.execute(text("""
            INSERT INTO dms_scan_jobs (id, tenant_id, job_type, status, queued_at)
            VALUES (CAST(:jid AS uuid), :tid, 'extract_ocr', 'queued', NOW())
        """), {"jid": job_id, "tid": tid})
        await session.commit()
    q = _get_rq_queue()
    q.enqueue("jobs.dms_extract_job.run_extract_ocr", job_id, job_timeout=86400, result_ttl=86400)
    return JSONResponse({"status": "queued", "job_id": job_id, "pipeline": "extract_ocr -> parse"})


@router.post("/dms/parse")
async def api_dms_parse(request: Request, user=Depends(get_current_user)):
    """Enqueue parse+analyze: copy text to documents table + push to Elasticsearch."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        running = (await session.execute(text(
            "SELECT id FROM dms_scan_jobs "
            "WHERE TRIM(tenant_id)=:tid AND job_type='parse' AND status IN ('queued','running') "
            "LIMIT 1"
        ), {"tid": tid})).fetchone()
        if running:
            return JSONResponse({"status": "already_running", "job_id": str(running[0])}, status_code=409)
        job_id = str(_uuid.uuid4())
        await session.execute(text("""
            INSERT INTO dms_scan_jobs (id, tenant_id, job_type, status, queued_at)
            VALUES (CAST(:jid AS uuid), :tid, 'parse', 'queued', NOW())
        """), {"jid": job_id, "tid": tid})
        await session.commit()
    q = _get_rq_queue()
    q.enqueue("jobs.dms_parse_analyze_job.run_parse_analyze", job_id, job_timeout=14400, result_ttl=86400)
    return JSONResponse({"status": "queued", "job_id": job_id})


@router.post("/dms/pipeline")
async def api_dms_pipeline(request: Request, user=Depends(get_current_user)):
    """Full pipeline: scan → extract_fast → extract_ocr → parse.
    Each stage auto-chains the next. Only enqueues scan; the rest chain automatically."""
    tid = _tid(request)

    # Check nothing is already running
    async with AsyncSessionLocal() as session:
        running = (await session.execute(text(
            "SELECT id, job_type FROM dms_scan_jobs "
            "WHERE TRIM(tenant_id)=:tid AND status IN ('queued','running') "
            "LIMIT 1"
        ), {"tid": tid})).fetchone()
        if running:
            return JSONResponse({
                "status": "already_running",
                "job_id": str(running[0]),
                "job_type": running[1],
            }, status_code=409)

        # Enqueue scan first — it will complete, then we chain extract
        scan_job_id = str(_uuid.uuid4())
        await session.execute(text("""
            INSERT INTO dms_scan_jobs (id, tenant_id, job_type, status, queued_at)
            VALUES (CAST(:jid AS uuid), :tid, 'scan', 'queued', NOW())
        """), {"jid": scan_job_id, "tid": tid})
        await session.commit()

    q = _get_rq_queue()
    q.enqueue("jobs.dms_pipeline_runner.run_pipeline", scan_job_id, tid,
              job_timeout=86400, result_ttl=86400)
    return JSONResponse({
        "status": "queued",
        "scan_job_id": scan_job_id,
        "pipeline": "scan → extract_fast → extract_ocr → parse",
    })


@router.post("/dms/geometry-segment")
async def api_dms_geometry_segment(request: Request, user=Depends(get_current_user)):
    """Enqueue DMS geometry build + paragraph segmentation (gated, idempotent).
    Auto-chains after parse too; this endpoint is for manual/standalone runs."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        running = (await session.execute(text(
            "SELECT id FROM dms_scan_jobs WHERE TRIM(tenant_id)=:tid "
            "AND job_type='geometry_segment' AND status IN ('queued','running') LIMIT 1"
        ), {"tid": tid})).fetchone()
        if running:
            return JSONResponse({"status": "already_running", "job_id": str(running[0])}, status_code=409)
        job_id = str(_uuid.uuid4())
        await session.execute(text("""
            INSERT INTO dms_scan_jobs (id, tenant_id, job_type, status, queued_at)
            VALUES (CAST(:jid AS uuid), :tid, 'geometry_segment', 'queued', NOW())
        """), {"jid": job_id, "tid": tid})
        await session.commit()
    q = _get_rq_queue()
    q.enqueue("jobs.dms_geometry_segment_job.run_geometry_segment", job_id, tid,
              job_timeout=86400, result_ttl=86400)
    return JSONResponse({"status": "queued", "job_id": job_id, "stage": "geometry_segment"})


@router.post("/dms/cancel/{job_id}")
async def api_dms_cancel(request: Request, job_id: str, user=Depends(get_current_user)):
    """Cancel a running/queued DMS job."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(
            "UPDATE dms_scan_jobs SET status='cancelled', completed_at=NOW() "
            "WHERE id=CAST(:jid AS uuid) AND TRIM(tenant_id)=:tid "
            "AND status IN ('queued', 'running') RETURNING id"
        ), {"jid": job_id, "tid": tid})
        row = result.fetchone()
        await session.commit()
    if row:
        return JSONResponse({"status": "cancelled", "job_id": job_id})
    return JSONResponse({"status": "not_found"}, status_code=404)


@router.get("/dms/job-status")
async def api_dms_job_status(request: Request, user=Depends(get_current_user)):
    """Return the most recent job status for each pipeline stage."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        scan = (await session.execute(text("""
            SELECT id, status, queued_at, started_at, completed_at,
                   files_discovered, files_indexed, files_skipped, error_message
            FROM dms_scan_jobs
            WHERE TRIM(tenant_id)=:tid AND job_type='scan'
            ORDER BY queued_at DESC LIMIT 1
        """), {"tid": tid})).mappings().fetchone()

        # Check both old 'extract' and new 'extract_fast' types
        extract = (await session.execute(text("""
            SELECT id, job_type, status, queued_at, started_at, completed_at,
                   files_discovered, files_indexed, files_skipped, error_message
            FROM dms_scan_jobs
            WHERE TRIM(tenant_id)=:tid AND job_type IN ('extract', 'extract_fast')
            ORDER BY queued_at DESC LIMIT 1
        """), {"tid": tid})).mappings().fetchone()

        extract_ocr = (await session.execute(text("""
            SELECT id, status, queued_at, started_at, completed_at,
                   files_discovered, files_indexed, files_skipped, error_message
            FROM dms_scan_jobs
            WHERE TRIM(tenant_id)=:tid AND job_type='extract_ocr'
            ORDER BY queued_at DESC LIMIT 1
        """), {"tid": tid})).mappings().fetchone()

        parse = (await session.execute(text("""
            SELECT id, status, queued_at, started_at, completed_at,
                   files_discovered, files_indexed, files_skipped, error_message
            FROM dms_scan_jobs
            WHERE TRIM(tenant_id)=:tid AND job_type='parse'
            ORDER BY queued_at DESC LIMIT 1
        """), {"tid": tid})).mappings().fetchone()

        geometry_segment = (await session.execute(text("""
            SELECT id, status, queued_at, started_at, completed_at,
                   files_discovered, files_indexed, files_skipped, error_message
            FROM dms_scan_jobs
            WHERE TRIM(tenant_id)=:tid AND job_type='geometry_segment'
            ORDER BY queued_at DESC LIMIT 1
        """), {"tid": tid})).mappings().fetchone()

    return JSONResponse({
        "scan": _ser(dict(scan)) if scan else None,
        "extract": _ser(dict(extract)) if extract else None,
        "extract_ocr": _ser(dict(extract_ocr)) if extract_ocr else None,
        "parse": _ser(dict(parse)) if parse else None,
        "geometry_segment": _ser(dict(geometry_segment)) if geometry_segment else None,
    })
