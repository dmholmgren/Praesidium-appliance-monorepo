"""Bill Run JSON APIs for React pages.
GET  /api/v1/billing/bill-runs              -> list all runs
GET  /api/v1/billing/bill-runs/{id}         -> detail + matters
GET  /api/v1/billing/bill-runs/{id}/matter/{brm_id}/slips -> prebill slips
POST /api/v1/billing/bill-runs              -> create new run (JSON)
"""
from __future__ import annotations
import logging
import uuid as _uuid
from datetime import date
from decimal import Decimal
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing", tags=["billing-bill-runs-api"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()

def _ser(row_dict):
    import datetime as dt
    out = {}
    for k, v in row_dict.items():
        if isinstance(v, Decimal): out[k] = float(v)
        elif isinstance(v, (dt.date, dt.datetime)): out[k] = v.isoformat()
        elif isinstance(v, _uuid.UUID): out[k] = str(v)
        else: out[k] = v
    return out

@router.get("/bill-runs")
async def bill_runs_list_api(request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        rows = await db.execute(sa_text("""
            SELECT br.id, br.run_name, br.billing_period_start, br.billing_period_end,
                   br.status, br.total_matters, br.total_fees, br.total_invoiced,
                   br.total_writeoffs, br.created_at, br.finalized_at,
                   u.full_name AS created_by_name
            FROM bill_runs br
            JOIN users u ON u.id = br.created_by_id
            WHERE br.tenant_id = :tid
            ORDER BY br.created_at DESC
        """), {"tid": tid})
        runs = [_ser(dict(r)) for r in rows.mappings()]
    return JSONResponse({"runs": runs})

@router.get("/bill-runs/{bill_run_id}")
async def bill_run_detail_api(request: Request, bill_run_id: int):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        run_row = await db.execute(sa_text("""
            SELECT br.*, u.full_name AS created_by_name
            FROM bill_runs br JOIN users u ON u.id = br.created_by_id
            WHERE br.id = :brid AND br.tenant_id = :tid
        """), {"brid": bill_run_id, "tid": tid})
        run = run_row.mappings().fetchone()
        if not run:
            return JSONResponse({"error": "Not found"}, status_code=404)
        matters_rows = await db.execute(sa_text("""
            SELECT * FROM bill_run_matters
            WHERE bill_run_id = :brid AND tenant_id = :tid
            ORDER BY client_name, matter_number
        """), {"brid": bill_run_id, "tid": tid})
        matters = [_ser(dict(r)) for r in matters_rows.mappings()]
    return JSONResponse({"bill_run": _ser(dict(run)), "matters": matters})

@router.get("/bill-runs/{bill_run_id}/matter/{brm_id}/slips")
async def prebill_slips_api(request: Request, bill_run_id: int, brm_id: int):
    tid = _tid(request)
    from modules.billing.services.bill_run_service import get_prebill_slips
    data = await get_prebill_slips(tid, brm_id)
    def ser_list(lst):
        return [_ser(dict(r) if not isinstance(r, dict) else r) for r in lst]
    return JSONResponse({
        "matter": _ser(data.get("matter", {})),
        "slips": ser_list(data.get("slips", [])),
        "adjustments": ser_list(data.get("adjustments", [])),
        "total_hours": data.get("total_hours", 0),
        "total_fees": data.get("total_fees", 0),
        "bill_run_id": bill_run_id, "brm_id": brm_id,
    })

@router.post("/bill-runs")
async def create_bill_run_api(request: Request):
    from modules.billing.services.bill_run_service import create_bill_run
    tid = _tid(request)
    user = getattr(request.state, "current_user", None)
    uid = user.id if user and hasattr(user, 'id') else 0
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid body"}, status_code=400)
    result = await create_bill_run(
        tenant_id=tid, period_start=date.fromisoformat(body["period_start"]),
        period_end=date.fromisoformat(body["period_end"]), created_by_id=uid,
        run_name=body.get("run_name") or None, notes=body.get("notes") or None,
    )
    return JSONResponse(result, status_code=201)
