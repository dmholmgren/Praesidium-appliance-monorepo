"""
modules/property/extract_api.py

Property extraction endpoint — reads a DMS file, runs LOI regex extraction,
creates a matter_properties row.

POST /api/v1/ai/extract-property
Body: { "matter_id": "...", "file_path": "...", "filename": "..." }

Uses the same text extractors from dms_extract_job for file reading,
then the LOI extractor for field extraction + county identification.

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
"""

from __future__ import annotations
import logging
import os
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.modules.property.extract_api")
router = APIRouter(prefix="/api/v1/ai", tags=["property-extract"])

PRAESIDIUM_ROOT = "/mnt/praesidium"


# ═══════════════════════════════════════════════════════════════════
# TEXT EXTRACTORS (inline — same logic as dms_extract_job)
# ═══════════════════════════════════════════════════════════════════

def _extract_pdf(filepath):
    try:
        import fitz
        doc = fitz.open(filepath)
        pages = []
        for page in doc:
            text = page.get_text()
            if text and text.strip():
                pages.append(text.strip())
        doc.close()
        if pages:
            return "\n\n".join(pages)
    except Exception:
        pass
    return ""


def _extract_docx(filepath):
    try:
        from docx import Document
        doc = Document(filepath)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception:
        return ""


def _extract_text_file(filepath):
    try:
        with open(filepath, "rb") as f:
            raw = f.read(200000)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1", errors="replace")
    except Exception:
        return ""


def _extract_rtf(filepath):
    try:
        with open(filepath, "rb") as f:
            raw = f.read()
        try:
            from striprtf.striprtf import rtf_to_text
            return rtf_to_text(raw.decode("utf-8", errors="replace"))
        except ImportError:
            import re
            text = raw.decode("utf-8", errors="replace")
            text = re.sub(r'\\[a-z]+\d*\s?', '', text)
            text = re.sub(r'[{}]', '', text)
            return text.strip()
    except Exception:
        return ""


EXTRACTORS = {
    "pdf": _extract_pdf, "docx": _extract_docx, "doc": _extract_docx,
    "txt": _extract_text_file, "rtf": _extract_rtf,
}


def _extract_text(filepath):
    ext = filepath.rsplit(".", 1)[-1].lower() if "." in filepath else ""
    fn = EXTRACTORS.get(ext)
    if not fn:
        return ""
    return fn(filepath)


# ═══════════════════════════════════════════════════════════════════
# REQUEST MODEL
# ═══════════════════════════════════════════════════════════════════

class ExtractPropertyRequest(BaseModel):
    matter_id: str
    file_path: str  # relative path within the matter folder
    filename: str


# ═══════════════════════════════════════════════════════════════════
# ENDPOINT
# ═══════════════════════════════════════════════════════════════════

@router.post("/extract-property")
async def extract_property(body: ExtractPropertyRequest, request: Request):
    tid = (getattr(request.state, "tenant_id", "") or "").strip()

    # Resolve the absolute file path
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.matter_name, c.client_name
            FROM matters m LEFT JOIN clients c ON m.client_id = c.id
              AND TRIM(m.tenant_id) = TRIM(c.tenant_id)
            WHERE m.id = CAST(:mid AS uuid) AND TRIM(m.tenant_id) = :tid
        """), {"mid": body.matter_id, "tid": tid})
        row = r.mappings().fetchone()

    if not row:
        raise HTTPException(404, "Matter not found")

    matter_root = os.path.join(PRAESIDIUM_ROOT, tid, "matters",
                                row["client_name"], row["matter_name"])
    if not os.path.isdir(matter_root):
        raise HTTPException(404, f"Matter disk root not found")

    filepath = os.path.join(matter_root, body.file_path)
    # Security: ensure resolved path is under matter root
    resolved = os.path.realpath(filepath)
    if not resolved.startswith(os.path.realpath(matter_root)):
        raise HTTPException(403, "Path traversal denied")
    if not os.path.isfile(resolved):
        raise HTTPException(404, f"File not found: {body.filename}")

    # Extract text
    text = _extract_text(resolved)
    if not text or len(text.strip()) < 50:
        raise HTTPException(422, "Could not extract sufficient text from file")

    # Run LOI field extraction
    from modules.property.loi_extractor import (
        extract_loi_property_fields, identify_county, get_cad_urls
    )
    fields = extract_loi_property_fields(text)

    if not fields:
        raise HTTPException(422, "No property fields found in document")

    county = fields.get("county")
    cad_info = get_cad_urls(county) if county else {}

    # Create matter_properties row
    prop_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as session:
        # Check if property already exists
        existing = await session.execute(sa_text("""
            SELECT id::text FROM matter_properties
            WHERE TRIM(tenant_id) = :tid AND matter_id = CAST(:mid AS uuid)
            LIMIT 1
        """), {"tid": tid, "mid": body.matter_id})
        existing_row = existing.mappings().fetchone()

        if existing_row:
            # Update existing property with extracted fields
            sets = []
            params = {"tid": tid, "pid": existing_row["id"]}
            field_map = {
                "property_location": "property_name",
                "acreage": "acreage",
                "purchase_price": "purchase_price",
                "price_per_unit": "price_per_unit",
                "price_unit": "price_unit",
                "earnest_money": "earnest_money",
                "feasibility_days": "feasibility_days",
                "closing_days": "closing_days",
                "title_company": "title_company",
                "title_officer": "title_officer",
                "broker_name": "broker_name",
                "commission_rate": "commission_rate",
                "zoning": "zoning",
                "county": "county",
            }
            for src_key, db_col in field_map.items():
                val = fields.get(src_key)
                if val is not None:
                    sets.append(f"{db_col} = :{db_col}")
                    params[db_col] = val

            if cad_info.get("search") and "cad_url" not in params:
                sets.append("cad_url = :cad_url")
                params["cad_url"] = cad_info.get("search")
            if cad_info.get("gis"):
                sets.append("gis_url = :gis_url")
                params["gis_url"] = cad_info.get("gis")

            if sets:
                sql = f"UPDATE matter_properties SET {', '.join(sets)}, updated_at = NOW() WHERE TRIM(tenant_id) = :tid AND id = CAST(:pid AS uuid)"
                await session.execute(sa_text(sql), params)
                await session.commit()

            return JSONResponse({
                "status": "updated",
                "property_id": existing_row["id"],
                "fields_extracted": list(fields.keys()),
                "county": county,
            })

        else:
            # Create new property
            await session.execute(sa_text("""
                INSERT INTO matter_properties (
                    id, tenant_id, matter_id, property_name,
                    street_address, county, acreage, zoning,
                    purchase_price, price_per_unit, price_unit,
                    earnest_money, feasibility_days, closing_days,
                    title_company, title_officer,
                    broker_name, commission_rate,
                    cad_url, gis_url, gis_embed_url,
                    scrape_status
                ) VALUES (
                    CAST(:id AS uuid), :tid, CAST(:mid AS uuid), :pname,
                    :addr, :county, :acreage, :zoning,
                    :price, :ppu, :punit,
                    :earnest, :feas, :closing,
                    :title_co, :title_off,
                    :broker, :commission,
                    :cad_url, :gis_url, :gis_embed,
                    'pending'
                )
            """), {
                "id": prop_id, "tid": tid, "mid": body.matter_id,
                "pname": fields.get("property_location"),
                "addr": fields.get("property_location"),
                "county": county,
                "acreage": fields.get("acreage"),
                "zoning": fields.get("zoning"),
                "price": fields.get("purchase_price"),
                "ppu": fields.get("price_per_unit"),
                "punit": fields.get("price_unit"),
                "earnest": fields.get("earnest_money"),
                "feas": fields.get("feasibility_days"),
                "closing": fields.get("closing_days"),
                "title_co": fields.get("title_company"),
                "title_off": fields.get("title_officer"),
                "broker": fields.get("broker_name"),
                "commission": fields.get("commission_rate"),
                "cad_url": cad_info.get("search"),
                "gis_url": cad_info.get("gis"),
                "gis_embed": cad_info.get("gis"),
            })
            await session.commit()

        return JSONResponse({
            "status": "created",
            "property_id": prop_id,
            "fields_extracted": list(fields.keys()),
            "county": county,
        })
