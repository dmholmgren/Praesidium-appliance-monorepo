"""
modules/tenant_admin/onboarding_api.py
JSON API for the React Onboarding page (Data Quality client list).
"""
import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tenant-admin/client-matter-cleanup/api", tags=["onboarding-api"])


def _tid(request):
    return (getattr(request.state, "tenant_id", "") or "").strip()


@router.get("/list")
async def api_client_list(request: Request, page: int = 1, status: str = "active",
                           user=Depends(get_current_user)):
    """Paginated client list with matters for Data Quality tab."""
    tid = _tid(request)
    per_page = 50
    offset = (page - 1) * per_page

    async with AsyncSessionLocal() as session:
        # Total clients
        total_row = (await session.execute(text(
            "SELECT COUNT(*) FROM clients WHERE TRIM(tenant_id) = :tid"
        ), {"tid": tid})).fetchone()
        total_clients = total_row[0] if total_row else 0

        # Total matters + typed count
        matter_stats = (await session.execute(text("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE matter_type IS NOT NULL AND matter_type != '') AS typed
            FROM matters WHERE TRIM(tenant_id) = :tid
        """), {"tid": tid})).fetchone()
        total_matters = matter_stats[0] if matter_stats else 0
        typed_count = matter_stats[1] if matter_stats else 0

        # Clients page
        clients_rows = (await session.execute(text("""
            SELECT c.id::text, c.client_name,
                   COUNT(m.id) AS matter_count,
                   COUNT(m.id) FILTER (WHERE m.matter_type IS NULL OR m.matter_type = '') AS unclassified_count
            FROM clients c
            LEFT JOIN matters m ON m.client_id = c.id AND TRIM(m.tenant_id) = :tid
            WHERE TRIM(c.tenant_id) = :tid
            GROUP BY c.id, c.client_name
            ORDER BY c.client_name
            LIMIT :lim OFFSET :off
        """), {"tid": tid, "lim": per_page, "off": offset})).mappings().all()

        # Get matters for these clients
        client_ids = [r["id"] for r in clients_rows]
        matters_rows = []
        if client_ids:
            status_filter = ""
            params = {"tid": tid}
            if status == "active":
                status_filter = "AND (m.status IS NULL OR m.status = 'active')"
            elif status == "closed":
                status_filter = "AND m.status = 'closed'"

            # Build IN clause safely
            placeholders = ",".join(["CAST(:cid_" + str(i) + " AS uuid)" for i in range(len(client_ids))])
            for i, cid in enumerate(client_ids):
                params["cid_" + str(i)] = cid

            matters_rows = (await session.execute(text(f"""
                SELECT m.id::text, m.client_id::text, m.matter_name, m.matter_number,
                       m.matter_type, m.status
                FROM matters m
                WHERE TRIM(m.tenant_id) = :tid AND m.client_id IN ({placeholders})
                {status_filter}
                ORDER BY m.matter_name
            """), params)).mappings().all()

    # Group matters by client
    matters_by_client = {}
    for m in matters_rows:
        cid = m["client_id"]
        if cid not in matters_by_client:
            matters_by_client[cid] = []
        matters_by_client[cid].append(dict(m))

    clients = []
    for r in clients_rows:
        d = dict(r)
        d["matters"] = matters_by_client.get(d["id"], [])
        clients.append(d)

    total_pages = max(1, (total_clients + per_page - 1) // per_page)

    return JSONResponse({
        "clients": clients,
        "page": page,
        "total_pages": total_pages,
        "total_clients": total_clients,
        "total_matters": total_matters,
        "typed_count": typed_count,
    })
