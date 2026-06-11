"""DMS Home JSON API — GET /api/v1/dms/home"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/dms", tags=["dms-api"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()
def _fmt(b):
    if not b: return "—"
    if b < 1024: return f"{b} B"
    if b < 1048576: return f"{b/1024:.1f} KB"
    if b < 1073741824: return f"{b/1048576:.1f} MB"
    return f"{b/1073741824:.1f} GB"
def _rel(dt):
    if not dt: return ""
    try:
        now = datetime.now(timezone.utc)
        if not dt.tzinfo: dt = dt.replace(tzinfo=timezone.utc)
        m = int((now-dt).total_seconds()/60)
        if m < 1: return "just now"
        if m < 60: return f"{m}m ago"
        h = m//60
        if h < 24: return f"{h}h ago"
        d = h//24
        return "yesterday" if d==1 else f"{d}d ago" if d<7 else dt.strftime("%b %-d")
    except: return ""

async def _stats(tid):
    try:
        async with AsyncSessionLocal() as s:
            sr = (await s.execute(sa_text("SELECT COUNT(*) AS ti, COALESCE(SUM(file_size_bytes),0) AS tb, COUNT(*) FILTER (WHERE ocr_status='pending') AS op, COUNT(*) FILTER (WHERE ocr_status='done') AS od, COUNT(*) FILTER (WHERE indexed_at >= NOW()-INTERVAL '7 days') AS iw FROM dms_documents WHERE trim(tenant_id)=trim(:t)"), {"t": tid})).mappings().fetchone()
            mc = (await s.execute(sa_text("SELECT COUNT(*) FROM matters WHERE trim(tenant_id)=trim(:t) AND status='active'"), {"t": tid})).scalar() or 0
            cc = (await s.execute(sa_text("SELECT COUNT(*) FROM clients WHERE trim(tenant_id)=trim(:t)"), {"t": tid})).scalar() or 0
            nc = (await s.execute(sa_text("SELECT COUNT(*) FROM documents WHERE trim(tenant_id)=trim(:t)"), {"t": tid})).scalar() or 0
            oq = (await s.execute(sa_text("SELECT COUNT(*) FROM dms_ocr_queue WHERE trim(tenant_id)=trim(:t) AND status IN ('pending','processing')"), {"t": tid})).scalar() or 0
        return {"total_indexed":int(sr["ti"] or 0),"total_bytes":int(sr["tb"] or 0),"total_bytes_fmt":_fmt(int(sr["tb"] or 0)),
            "ocr_pending":int(sr["op"] or 0),"ocr_done":int(sr["od"] or 0),"indexed_this_week":int(sr["iw"] or 0),
            "total_matters":mc,"total_clients":cc,"ocr_queued":oq,"native_count":nc}
    except Exception as e:
        logger.error("dms _stats: %s",e); return {"total_indexed":0,"total_bytes":0,"total_bytes_fmt":"—","error":str(e)}

async def _tree(tid):
    try:
        async with AsyncSessionLocal() as s:
            rows = await s.execute(sa_text("""
                SELECT m.id::text,m.matter_name,m.matter_number,c.id::text AS cid,c.client_name,COUNT(d.id) AS dc
                FROM matters m LEFT JOIN clients c ON m.client_id=c.id AND m.tenant_id=c.tenant_id
                LEFT JOIN documents d ON d.matter_id=m.id AND d.tenant_id=m.tenant_id
                WHERE trim(m.tenant_id)=trim(:t) AND m.status='active'
                GROUP BY m.id,m.matter_name,m.matter_number,c.id,c.client_name
                ORDER BY c.client_name NULLS LAST,m.matter_name"""), {"t": tid})
            cm={}
            for r in rows.mappings():
                cid=r["cid"] or "unknown"
                if cid not in cm: cm[cid]={"client_id":cid,"client_name":r["client_name"] or "Unknown Client","matters":[]}
                cm[cid]["matters"].append({"id":r["id"],"matter_name":r["matter_name"] or "Untitled","matter_number":r["matter_number"] or "","doc_count":int(r["dc"] or 0)})
        cl=sorted(cm.values(),key=lambda x:x["client_name"])
        return {"clients":cl,"total_matters":sum(len(c["matters"]) for c in cl),"total_clients":len(cl)}
    except Exception as e:
        logger.error("dms _tree: %s",e); return {"clients":[],"total_matters":0,"total_clients":0,"error":str(e)}

async def _recent(tid):
    try:
        async with AsyncSessionLocal() as s:
            rows = await s.execute(sa_text("""
                SELECT d.id::text,d.matter_id::text AS matter_id,d.filename AS title,
                    d.document_type AS dt,d.file_size,d.updated_at,
                    m.matter_name,m.matter_number,c.client_name
                FROM documents d LEFT JOIN matters m ON d.matter_id=m.id AND d.tenant_id=m.tenant_id
                LEFT JOIN clients c ON m.client_id=c.id AND m.tenant_id=c.tenant_id
                WHERE trim(d.tenant_id)=trim(:t) ORDER BY d.updated_at DESC LIMIT 20"""), {"t": tid})
            return [{"id":r["id"],"matter_id":r["matter_id"] or "","title":r["title"] or "Untitled",
                "doc_type":r["dt"] or "","file_size":_fmt(r["file_size"]),
                "updated_at":r["updated_at"].isoformat() if r["updated_at"] else None,"time_label":_rel(r["updated_at"]),
                "matter_name":r["matter_name"] or "","matter_number":r["matter_number"] or "",
                "client_name":r["client_name"] or ""} for r in rows.mappings()]
    except Exception as e:
        logger.error("dms _recent: %s",e); return []

async def _activity(tid):
    try:
        async with AsyncSessionLocal() as s:
            rows = await s.execute(sa_text("""
                SELECT file_path,file_size_bytes,indexed_at,ocr_status,source
                FROM dms_documents WHERE trim(tenant_id)=trim(:t) ORDER BY indexed_at DESC LIMIT 15"""), {"t": tid})
            items=[]
            for r in rows.mappings():
                fp=r["file_path"] or ""; fn=fp.replace("\\","/").split("/")[-1] if fp else "?"
                items.append({"filename":fn,"file_size":_fmt(r["file_size_bytes"]),"indexed_at":r["indexed_at"].isoformat() if r["indexed_at"] else None,
                    "time_label":_rel(r["indexed_at"]),"ocr_status":r["ocr_status"] or "","source":r["source"] or ""})
        return items
    except Exception as e:
        logger.error("dms _activity: %s",e); return []

@router.get("/home")
async def dms_home_data(request: Request):
    tid = _tid(request)
    return JSONResponse({"stats":await _stats(tid),"matter_tree":await _tree(tid),
        "recent_docs":await _recent(tid),"activity_feed":await _activity(tid)})
