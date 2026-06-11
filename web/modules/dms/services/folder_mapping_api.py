"""
modules/dms/services/folder_mapping_api.py
Read + decide API for the legacy folder->matter mapping review UI (file-import).
Backs three views over dms_folder_matches:
  * /list           flat rows (filterable) -> View 1 (group by matter in JS) + View 2 (by folder)
  * /summary        status counts for the metric cards
  * /legacy-general Legacy-General rows grouped by client -> View 3 (promote-from-general)
  * /decide (POST)  confirm/reject (the match + hide actions); never touches synced rows
The move/sync trigger is a separate endpoint (added next).
"""
import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/folder-map", tags=["folder-map"])

LEGGEN = "Legacy - General"


def _tenant(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


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
    return obj


def _status(row):
    if row.get("synced_at") is not None: return "synced"
    if row.get("accepted") is True: return "confirmed"
    if row.get("accepted") is False: return "rejected"
    if row.get("is_leggen"): return "legacy_general"
    return "pending"


# server-controlled status -> WHERE fragment (no user SQL)
_STATUS_SQL = {
    "pending":        "fm.accepted IS NULL AND m.matter_name <> :leg",
    "confirmed":      "fm.accepted IS TRUE",
    "rejected":       "fm.accepted IS FALSE",
    "synced":         "fm.synced_at IS NOT NULL",
    "legacy_general": "m.matter_name = :leg",
}

_BASE = """
  FROM dms_folder_matches fm
  JOIN matters m ON m.id = fm.matter_id
       AND trim(m.tenant_id::text) = trim(fm.tenant_id::text)
  LEFT JOIN clients c ON c.id = m.client_id
  WHERE trim(fm.tenant_id::text) = trim(:tid)
"""


@router.get("/list")
async def list_map(request: Request, status: str = "all", client_id: str = "",
                   q: str = "", limit: int = 500, offset: int = 0,
                   user=Depends(get_current_user)):
    tid = _tenant(request)
    where = _BASE
    params = {"tid": tid, "leg": LEGGEN, "limit": min(limit, 2000), "offset": offset}
    if status in _STATUS_SQL:
        where += " AND " + _STATUS_SQL[status]
    if client_id:
        where += " AND c.id = CAST(:cid AS uuid)"; params["cid"] = client_id
    if q:
        where += " AND (m.matter_name ILIKE :q OR fm.folder_path ILIKE :q)"
        params["q"] = f"%{q}%"
    sql = ("SELECT fm.id::text AS map_id, fm.folder_path, fm.best_disk_path, fm.score, "
           "fm.accepted, fm.synced_at, fm.disk_file_count, "
           "m.id::text AS matter_id, m.matter_name, m.matter_number, "
           "(m.matter_name = :leg) AS is_leggen, "
           "c.id::text AS client_id, c.client_name "
           + where +
           " ORDER BY c.client_name NULLS LAST, m.matter_name, fm.folder_path "
           "LIMIT :limit OFFSET :offset")
    try:
        async with AsyncSessionLocal() as s:
            r = await s.execute(sa_text(sql), params)
            rows = [dict(x) for x in r.mappings().fetchall()]
        for row in rows:
            row["status"] = _status(row)
        return JSONResponse(_serialize({"rows": rows, "count": len(rows)}))
    except Exception as e:
        logger.error("folder-map list: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.get("/summary")
async def summary(request: Request, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as s:
            r = await s.execute(sa_text(
                "SELECT "
                "count(*) AS total, "
                "count(*) FILTER (WHERE fm.accepted IS TRUE) AS confirmed, "
                "count(*) FILTER (WHERE fm.accepted IS FALSE) AS rejected, "
                "count(*) FILTER (WHERE fm.accepted IS NULL AND m.matter_name <> :leg) AS pending, "
                "count(*) FILTER (WHERE m.matter_name = :leg) AS legacy_general, "
                "count(*) FILTER (WHERE fm.synced_at IS NOT NULL) AS synced, "
                "count(DISTINCT fm.matter_id) FILTER (WHERE (SELECT count(*) FROM dms_folder_matches f2 "
                "  WHERE f2.matter_id = fm.matter_id AND trim(f2.tenant_id::text)=trim(:tid)) > 1) AS multi_folder "
                + _BASE), {"tid": tid, "leg": LEGGEN})
            row = dict(r.mappings().fetchone())
        return JSONResponse(_serialize(row))
    except Exception as e:
        logger.error("folder-map summary: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.get("/legacy-general")
async def legacy_general(request: Request, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as s:
            r = await s.execute(sa_text(
                "SELECT fm.id::text AS map_id, fm.folder_path, fm.score, fm.disk_file_count, "
                "fm.accepted, c.id::text AS client_id, c.client_name, m.id::text AS matter_id "
                + _BASE + " AND m.matter_name = :leg AND fm.synced_at IS NULL "
                "ORDER BY c.client_name NULLS LAST, fm.folder_path"),
                {"tid": tid, "leg": LEGGEN})
            rows = [dict(x) for x in r.mappings().fetchall()]
        groups = {}
        for row in rows:
            k = row["client_id"] or "__none__"
            g = groups.setdefault(k, {"client_id": row["client_id"],
                                      "client_name": row["client_name"],
                                      "leggen_matter_id": row["matter_id"], "folders": []})
            g["folders"].append({"map_id": row["map_id"], "folder_path": row["folder_path"],
                                 "score": row["score"], "disk_file_count": row["disk_file_count"]})
        return JSONResponse(_serialize({"groups": list(groups.values())}))
    except Exception as e:
        logger.error("folder-map legacy-general: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.post("/decide")
async def decide(request: Request, user=Depends(get_current_user)):
    """Body: {map_ids: [...], decision: 'accept'|'reject'}. Never touches synced rows."""
    tid = _tenant(request)
    body = await request.json()
    ids = body.get("map_ids") or []
    decision = body.get("decision")
    if not ids or decision not in ("accept", "reject"):
        return JSONResponse({"error": "map_ids and decision ('accept'|'reject') required"}, 400)
    val = True if decision == "accept" else False
    try:
        async with AsyncSessionLocal() as s:
            res = await s.execute(sa_text(
                "UPDATE dms_folder_matches SET accepted = :val "
                "WHERE trim(tenant_id::text) = trim(:tid) "
                "AND synced_at IS NULL "
                "AND id = ANY(CAST(:ids AS uuid[]))"),
                {"val": val, "tid": tid, "ids": list(ids)})
            await s.commit()
            n = res.rowcount
        return JSONResponse({"ok": True, "updated": n})
    except Exception as e:
        logger.error("folder-map decide: %s", e)
        return JSONResponse({"error": str(e)}, 500)
