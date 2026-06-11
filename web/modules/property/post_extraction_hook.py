"""
modules/property/post_extraction_hook.py

Hook called after dms_extract_job completes text extraction on a single file.
If the file is classified as an LOI / purchase agreement, runs LOI regex
extraction and auto-creates/updates a matter_properties row.

Called from run_extract_single() after text extraction succeeds.

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
"""

from __future__ import annotations
import logging
import os
import uuid

import psycopg2
import psycopg2.extras

log = logging.getLogger("praesidium.modules.property.post_extraction_hook")

# Document types that trigger property extraction
LOI_TYPES = {"loi", "letter_of_intent", "purchase_agreement"}

# Filename patterns that suggest LOI / purchase agreement
LOI_FILENAME_PATTERNS = [
    "loi", "letter of intent", "letter_of_intent",
    "purchase agreement", "purchase_agreement",
    "contract of sale", "contract_of_sale",
    "psa", "earnest money", "earnest_money",
]


def _get_db_conn():
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "")
    at_idx = url.rfind("@")
    userinfo = url[:at_idx]
    hostinfo = url[at_idx + 1:]
    user, password = userinfo.split(":", 1)
    host_port, dbname = hostinfo.split("/", 1)
    host, port = host_port.rsplit(":", 1)
    return psycopg2.connect(
        host=host, port=int(port),
        dbname=dbname.split("?")[0],
        user=user, password=password,
    )


def should_extract_property(tenant_id: str, dms_doc_id: str, file_path: str, content_text: str) -> bool:
    """
    Determine if a document should trigger property extraction.
    Checks document_type classification AND filename patterns.
    """
    # Check filename patterns first (fast, no DB)
    fn_lower = os.path.basename(file_path).lower()
    for pat in LOI_FILENAME_PATTERNS:
        if pat in fn_lower:
            return True

    # Check if document has been classified as LOI type
    conn = _get_db_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """SELECT document_type FROM dms_documents
               WHERE id = CAST(%s AS uuid) AND TRIM(tenant_id) = %s""",
            (dms_doc_id, tenant_id.strip())
        )
        row = cur.fetchone()
        if row and row.get("document_type") and row["document_type"].lower() in LOI_TYPES:
            return True

        # Also check the documents table
        cur.execute(
            """SELECT document_type FROM documents
               WHERE TRIM(tenant_id) = %s AND storage_path = %s""",
            (tenant_id.strip(), file_path)
        )
        doc_row = cur.fetchone()
        if doc_row and doc_row.get("document_type") and doc_row["document_type"].lower() in LOI_TYPES:
            return True

    except Exception as e:
        log.warning("should_extract_property check failed: %s", e)
    finally:
        cur.close()
        conn.close()

    # Content heuristic: look for LOI-specific phrases in first 2000 chars
    if content_text:
        sample = content_text[:2000].lower()
        loi_signals = [
            "letter of intent", "purchase price", "earnest money",
            "feasibility period", "option period", "closing date",
            "title commitment", "seller agrees", "buyer agrees",
            "due diligence", "inspection period",
        ]
        hits = sum(1 for s in loi_signals if s in sample)
        if hits >= 3:
            return True

    return False


def run_property_extraction_hook(tenant_id: str, dms_doc_id: str, file_path: str, content_text: str):
    """
    Post-extraction hook: if this file is an LOI/purchase agreement,
    run LOI regex extraction and create/update matter_properties.

    Synchronous — runs in the same RQ worker context as run_extract_single.
    """
    tid = tenant_id.strip()

    if not should_extract_property(tid, dms_doc_id, file_path, content_text):
        return

    log.info("Property extraction triggered for %s", file_path)

    # Import LOI extractor
    try:
        from modules.property.loi_extractor import (
            extract_loi_property_fields, identify_county, get_cad_urls
        )
    except ImportError:
        log.warning("LOI extractor not available — skipping property hook")
        return

    # Extract fields
    fields = extract_loi_property_fields(content_text)
    if not fields:
        log.info("No property fields extracted from %s", file_path)
        return

    county = fields.get("county")
    cad_info = get_cad_urls(county) if county else {}

    conn = _get_db_conn()
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        # Resolve matter_id from dms_documents
        cur.execute(
            """SELECT matter_id::text FROM dms_documents
               WHERE id = CAST(%s AS uuid) AND TRIM(tenant_id) = %s""",
            (dms_doc_id, tid)
        )
        row = cur.fetchone()
        if not row or not row.get("matter_id"):
            # Try documents table via storage_path
            cur.execute(
                """SELECT matter_id::text FROM documents
                   WHERE TRIM(tenant_id) = %s AND storage_path = %s LIMIT 1""",
                (tid, file_path)
            )
            row = cur.fetchone()

        if not row or not row.get("matter_id"):
            log.warning("Cannot resolve matter_id for %s — skipping property hook", file_path)
            return

        matter_id = row["matter_id"]

        # Check if property already exists for this matter
        cur.execute(
            """SELECT id::text FROM matter_properties
               WHERE TRIM(tenant_id) = %s AND matter_id = CAST(%s AS uuid)
               LIMIT 1""",
            (tid, matter_id)
        )
        existing = cur.fetchone()

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
            "cad_account_number": "cad_account_number",
        }

        if existing:
            # Update — only fill in NULL fields (don't overwrite manual edits)
            existing_id = existing["id"]
            cur.execute(
                """SELECT * FROM matter_properties
                   WHERE id = CAST(%s AS uuid)""",
                (existing_id,)
            )
            current = cur.fetchone()

            sets = []
            params = {"tid": tid, "pid": existing_id}
            for src_key, db_col in field_map.items():
                val = fields.get(src_key)
                if val is not None and (current.get(db_col) is None or current.get(db_col) == ''):
                    sets.append(f"{db_col} = %({db_col})s")
                    params[db_col] = val

            if cad_info.get("search") and not current.get("cad_url"):
                sets.append("cad_url = %(cad_url)s")
                params["cad_url"] = cad_info["search"]
            if cad_info.get("gis") and not current.get("gis_url"):
                sets.append("gis_url = %(gis_url)s")
                params["gis_url"] = cad_info["gis"]
                sets.append("gis_embed_url = %(gis_embed)s")
                params["gis_embed"] = cad_info["gis"]

            if sets:
                sql = f"UPDATE matter_properties SET {', '.join(sets)}, updated_at = NOW() WHERE TRIM(tenant_id) = %(tid)s AND id = CAST(%(pid)s AS uuid)"
                cur.execute(sql, params)
                conn.commit()
                log.info("Updated property %s for matter %s from %s (%d fields)",
                         existing_id, matter_id, os.path.basename(file_path), len(sets))
            else:
                log.info("Property %s already has all extracted fields — no update", existing_id)

        else:
            # Create new property
            prop_id = str(uuid.uuid4())
            cur.execute(
                """INSERT INTO matter_properties (
                    id, tenant_id, matter_id, property_name,
                    street_address, county, acreage, zoning,
                    purchase_price, price_per_unit, price_unit,
                    earnest_money, feasibility_days, closing_days,
                    title_company, title_officer,
                    broker_name, commission_rate,
                    cad_url, gis_url, gis_embed_url,
                    scrape_status
                ) VALUES (
                    CAST(%s AS uuid), %s, CAST(%s AS uuid), %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s, %s,
                    'pending'
                )""",
                (
                    prop_id, tid, matter_id,
                    fields.get("property_location"),
                    fields.get("property_location"),
                    county,
                    fields.get("acreage"),
                    fields.get("zoning"),
                    fields.get("purchase_price"),
                    fields.get("price_per_unit"),
                    fields.get("price_unit"),
                    fields.get("earnest_money"),
                    fields.get("feasibility_days"),
                    fields.get("closing_days"),
                    fields.get("title_company"),
                    fields.get("title_officer"),
                    fields.get("broker_name"),
                    fields.get("commission_rate"),
                    cad_info.get("search"),
                    cad_info.get("gis"),
                    cad_info.get("gis"),
                )
            )
            conn.commit()
            log.info("Created property %s for matter %s from %s (%d fields)",
                     prop_id, matter_id, os.path.basename(file_path),
                     len([v for v in fields.values() if v is not None]))

    except Exception as exc:
        conn.rollback()
        log.exception("Property extraction hook failed for %s: %s", file_path, exc)
    finally:
        cur.close()
        conn.close()
