"""
modules/property/router.py

Property Intelligence API — CRUD for matter_properties, property_parcels,
property_exceptions, property_deed_chain.

Uses AsyncSessionLocal + sa_text() — same pattern as all other Praesidium routers.

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
"""

from __future__ import annotations
import logging
import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.modules.property")
router = APIRouter(prefix="/api/property", tags=["property"])


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


# ═══════════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ═══════════════════════════════════════════════════════════════════

class PropertyCreate(BaseModel):
    property_name: Optional[str] = None
    street_address: Optional[str] = None
    city: Optional[str] = None
    county: Optional[str] = None
    state: str = "TX"
    zip_code: Optional[str] = None
    cad_account_number: Optional[str] = None
    legal_description: Optional[str] = None
    acreage: Optional[float] = None
    zoning: Optional[str] = None
    purchase_price: Optional[float] = None
    price_per_unit: Optional[float] = None
    price_unit: Optional[str] = None
    earnest_money: Optional[float] = None
    feasibility_days: Optional[int] = None
    closing_days: Optional[int] = None
    effective_date: Optional[str] = None
    title_company: Optional[str] = None
    title_officer: Optional[str] = None
    broker_name: Optional[str] = None
    broker_company: Optional[str] = None
    commission_rate: Optional[float] = None
    gis_embed_url: Optional[str] = None


class ExceptionCreate(BaseModel):
    exception_number: Optional[int] = None
    exception_type: Optional[str] = None
    description: Optional[str] = None
    recording_info: Optional[str] = None
    source_url: Optional[str] = None
    status: str = "open"
    notes: Optional[str] = None


class ExceptionUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None


class DeedChainCreate(BaseModel):
    deed_type: Optional[str] = None
    grantor: Optional[str] = None
    grantee: Optional[str] = None
    recording_date: Optional[str] = None
    recording_info: Optional[str] = None
    consideration: Optional[float] = None
    source_url: Optional[str] = None
    sort_order: Optional[int] = None


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def _serialize(row):
    """Convert a SQLAlchemy row mapping to JSON-safe dict."""
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, (date,)):
            d[k] = v.isoformat()
        elif hasattr(v, 'isoformat'):
            d[k] = v.isoformat()
        elif isinstance(v, uuid.UUID):
            d[k] = str(v)
        elif v is not None and not isinstance(v, (str, int, float, bool, list, dict)):
            d[k] = str(v)
    return d


# ═══════════════════════════════════════════════════════════════════
# LIST PROPERTIES FOR A MATTER
# ═══════════════════════════════════════════════════════════════════

@router.get("/{matter_id}")
async def list_properties(matter_id: str, request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT id, property_name, street_address, city, county, state,
                   acreage, purchase_price, zoning, cad_account_number,
                   scrape_status, last_cad_scrape, created_at
            FROM matter_properties
            WHERE TRIM(tenant_id) = :tid AND matter_id = CAST(:mid AS uuid)
            ORDER BY created_at
        """), {"tid": tid, "mid": matter_id})
        rows = [_serialize(row) for row in r.mappings()]
    return JSONResponse(rows)


# ═══════════════════════════════════════════════════════════════════
# PROPERTY DETAIL
# ═══════════════════════════════════════════════════════════════════

@router.get("/detail/{property_id}")
async def property_detail(property_id: str, request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT * FROM matter_properties
            WHERE TRIM(tenant_id) = :tid AND id = CAST(:pid AS uuid)
        """), {"tid": tid, "pid": property_id})
        prop = r.mappings().fetchone()
        if not prop:
            raise HTTPException(404, "Property not found")

        parcels_r = await session.execute(sa_text("""
            SELECT * FROM property_parcels
            WHERE TRIM(tenant_id) = :tid AND property_id = CAST(:pid AS uuid)
            ORDER BY cad_account_number
        """), {"tid": tid, "pid": property_id})

        exceptions_r = await session.execute(sa_text("""
            SELECT * FROM property_exceptions
            WHERE TRIM(tenant_id) = :tid AND property_id = CAST(:pid AS uuid)
            ORDER BY exception_number
        """), {"tid": tid, "pid": property_id})

        deeds_r = await session.execute(sa_text("""
            SELECT * FROM property_deed_chain
            WHERE TRIM(tenant_id) = :tid AND property_id = CAST(:pid AS uuid)
            ORDER BY sort_order, recording_date DESC
        """), {"tid": tid, "pid": property_id})

        result = _serialize(prop)
        result["parcels"] = [_serialize(row) for row in parcels_r.mappings()]
        result["exceptions"] = [_serialize(row) for row in exceptions_r.mappings()]
        result["deed_chain"] = [_serialize(row) for row in deeds_r.mappings()]
    return JSONResponse(result)


# ═══════════════════════════════════════════════════════════════════
# CREATE PROPERTY
# ═══════════════════════════════════════════════════════════════════

@router.post("/{matter_id}")
async def create_property(matter_id: str, body: PropertyCreate, request: Request):
    tid = _tid(request)
    prop_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            INSERT INTO matter_properties (
                id, tenant_id, matter_id, property_name, street_address,
                city, county, state, zip_code, cad_account_number,
                legal_description, acreage, zoning,
                purchase_price, price_per_unit, price_unit,
                earnest_money, feasibility_days, closing_days,
                title_company, title_officer, broker_name, broker_company,
                commission_rate, gis_embed_url
            ) VALUES (
                CAST(:id AS uuid), :tid, CAST(:mid AS uuid), :property_name, :street_address,
                :city, :county, :state, :zip_code, :cad_account_number,
                :legal_description, :acreage, :zoning,
                :purchase_price, :price_per_unit, :price_unit,
                :earnest_money, :feasibility_days, :closing_days,
                :title_company, :title_officer, :broker_name, :broker_company,
                :commission_rate, :gis_embed_url
            )
        """), {
            "id": prop_id, "tid": tid, "mid": matter_id,
            **body.dict(),
        })
        await session.commit()
    return JSONResponse({"id": prop_id, "status": "created"})


# ═══════════════════════════════════════════════════════════════════
# UPDATE PROPERTY
# ═══════════════════════════════════════════════════════════════════

@router.put("/{property_id}")
async def update_property(property_id: str, body: PropertyCreate, request: Request):
    tid = _tid(request)
    data = {k: v for k, v in body.dict().items() if v is not None}
    if not data:
        return JSONResponse({"status": "no changes"})

    sets = ", ".join(f"{k} = :{k}" for k in data)
    sql = f"UPDATE matter_properties SET {sets}, updated_at = NOW() WHERE TRIM(tenant_id) = :tid AND id = CAST(:pid AS uuid)"
    data["tid"] = tid
    data["pid"] = property_id

    async with AsyncSessionLocal() as session:
        await session.execute(sa_text(sql), data)
        await session.commit()
    return JSONResponse({"status": "updated"})


# ═══════════════════════════════════════════════════════════════════
# TRIGGER SCRAPE
# ═══════════════════════════════════════════════════════════════════

@router.post("/{property_id}/scrape")
async def trigger_scrape(property_id: str, request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            UPDATE matter_properties
            SET scrape_status = 'queued', updated_at = NOW()
            WHERE TRIM(tenant_id) = :tid AND id = CAST(:pid AS uuid)
        """), {"tid": tid, "pid": property_id})
        await session.commit()
    return JSONResponse({"status": "queued"})


# ═══════════════════════════════════════════════════════════════════
# EXCEPTIONS CRUD
# ═══════════════════════════════════════════════════════════════════

@router.get("/{property_id}/exceptions")
async def list_exceptions(property_id: str, request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT * FROM property_exceptions
            WHERE TRIM(tenant_id) = :tid AND property_id = CAST(:pid AS uuid)
            ORDER BY exception_number
        """), {"tid": tid, "pid": property_id})
    return JSONResponse([_serialize(row) for row in r.mappings()])


@router.post("/{property_id}/exceptions")
async def create_exception(property_id: str, body: ExceptionCreate, request: Request):
    tid = _tid(request)
    exc_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            INSERT INTO property_exceptions (
                id, tenant_id, property_id, exception_number,
                exception_type, description, recording_info,
                source_url, status, notes
            ) VALUES (
                CAST(:id AS uuid), :tid, CAST(:pid AS uuid), :exception_number,
                :exception_type, :description, :recording_info,
                :source_url, :status, :notes
            )
        """), {"id": exc_id, "tid": tid, "pid": property_id, **body.dict()})
        await session.commit()
    return JSONResponse({"id": exc_id, "status": "created"})


@router.put("/exception/{exception_id}")
async def update_exception(exception_id: str, body: ExceptionUpdate, request: Request):
    tid = _tid(request)
    data = {k: v for k, v in body.dict().items() if v is not None}
    if not data:
        return JSONResponse({"status": "no changes"})
    sets = ", ".join(f"{k} = :{k}" for k in data)
    sql = f"UPDATE property_exceptions SET {sets}, updated_at = NOW() WHERE TRIM(tenant_id) = :tid AND id = CAST(:eid AS uuid)"
    data["tid"] = tid
    data["eid"] = exception_id
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text(sql), data)
        await session.commit()
    return JSONResponse({"status": "updated"})


# ═══════════════════════════════════════════════════════════════════
# DEED CHAIN
# ═══════════════════════════════════════════════════════════════════

@router.get("/{property_id}/deed-chain")
async def get_deed_chain(property_id: str, request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT * FROM property_deed_chain
            WHERE TRIM(tenant_id) = :tid AND property_id = CAST(:pid AS uuid)
            ORDER BY sort_order, recording_date DESC
        """), {"tid": tid, "pid": property_id})
    return JSONResponse([_serialize(row) for row in r.mappings()])


@router.post("/{property_id}/deed-chain")
async def add_deed(property_id: str, body: DeedChainCreate, request: Request):
    tid = _tid(request)
    deed_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            INSERT INTO property_deed_chain (
                id, tenant_id, property_id, deed_type,
                grantor, grantee, recording_date, recording_info,
                consideration, source_url, sort_order
            ) VALUES (
                CAST(:id AS uuid), :tid, CAST(:pid AS uuid), :deed_type,
                :grantor, :grantee, CAST(NULLIF(:recording_date, '') AS date), :recording_info,
                :consideration, :source_url, :sort_order
            )
        """), {"id": deed_id, "tid": tid, "pid": property_id, **body.dict()})
        await session.commit()
    return JSONResponse({"id": deed_id, "status": "created"})
