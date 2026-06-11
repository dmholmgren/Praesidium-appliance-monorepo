"""
modules/property/loi_extractor.py

Post-extraction hook that bridges the extraction template engine output
into matter_properties rows.  When the extraction engine processes a
document classified as 'loi' or 'purchase_agreement', this module
maps the extracted fields to the property intelligence schema.

Called from dms_extract_job.py after extraction completes, when the
document_type_code maps to 'loi' or the parse_type is 'loi'.

Also handles: geocoding from property description text,
county identification for CAD routing.

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
"""

from __future__ import annotations
import json
import logging
import re
import uuid
from typing import Optional

log = logging.getLogger("praesidium.modules.property.loi_extractor")


# ═══════════════════════════════════════════════════════════════════
# TEXAS COUNTY LOOKUP (for CAD routing)
# ═══════════════════════════════════════════════════════════════════

# Map of city/area → county for DFW-area targets
CITY_TO_COUNTY = {
    "sherman": "grayson",
    "denison": "grayson",
    "whitesboro": "grayson",
    "howe": "grayson",
    "van alstyne": "grayson",
    "gunter": "grayson",
    "tom bean": "grayson",
    "pottsboro": "grayson",
    "gordonville": "grayson",
    "bells": "grayson",
    "mckinney": "collin",
    "plano": "collin",
    "frisco": "collin",
    "allen": "collin",
    "anna": "collin",
    "celina": "collin",
    "prosper": "collin",
    "princeton": "collin",
    "melissa": "collin",
    "wylie": "collin",
    "lucas": "collin",
    "fairview": "collin",
    "lavon": "collin",
    "blue ridge": "collin",
    "farmersville": "collin",
    "josephine": "collin",
    "denton": "denton",
    "lewisville": "denton",
    "flower mound": "denton",
    "little elm": "denton",
    "the colony": "denton",
    "corinth": "denton",
    "aubrey": "denton",
    "pilot point": "denton",
    "sanger": "denton",
    "krum": "denton",
    "ponder": "denton",
    "argyle": "denton",
    "justin": "denton",
    "northlake": "denton",
    "trophy club": "denton",
    "roanoke": "denton",
    "forney": "kaufman",
    "terrell": "kaufman",
    "kaufman": "kaufman",
    "crandall": "kaufman",
    "heath": "rockwall",
    "rockwall": "rockwall",
    "royse city": "rockwall",
    "dallas": "dallas",
    "garland": "dallas",
    "mesquite": "dallas",
    "irving": "dallas",
    "grand prairie": "dallas",
    "richardson": "dallas",
    "carrollton": "dallas",
    "farmers branch": "dallas",
    "fort worth": "tarrant",
    "arlington": "tarrant",
    "mansfield": "tarrant",
    "keller": "tarrant",
    "southlake": "tarrant",
    "colleyville": "tarrant",
    "grapevine": "tarrant",
    "waxahachie": "ellis",
    "midlothian": "ellis",
    "ennis": "ellis",
}

# CAD website patterns per county
CAD_URLS = {
    "collin": {
        "search": "https://www.collincad.org/property-search",
        "detail": "https://www.collincad.org/propertysearch?prop_id={account}",
        "gis": "https://maps.collincountytx.gov/",
    },
    "denton": {
        "search": "https://www.dentoncad.com/",
        "detail": "https://www.dentoncad.com/Property/View/{account}",
        "gis": "https://gis.dentoncounty.gov/",
    },
    "grayson": {
        "search": "https://www.graysoncad.org/",
        "detail": "https://www.graysoncad.org/PropertyDetail.aspx?PropertyID={account}",
        "gis": "https://gis.graysoncountytx.gov/",
    },
    "kaufman": {
        "search": "https://www.kaufmancad.org/",
        "detail": "https://www.kaufmancad.org/Property/View/{account}",
        "gis": None,
    },
    "dallas": {
        "search": "https://www.dallascad.org/",
        "detail": "https://www.dallascad.org/AcctDetailRes.aspx?ID={account}",
        "gis": "https://gis.dallascounty.org/",
    },
    "tarrant": {
        "search": "https://www.tad.org/",
        "detail": "https://www.tad.org/Property/View/{account}",
        "gis": "https://gis.tarrantcounty.com/",
    },
}


def identify_county(text: str) -> Optional[str]:
    """
    Attempt to identify the Texas county from a property location string.
    Checks city names and county name mentions.
    """
    if not text:
        return None
    lower = text.lower()

    # Direct county mention
    for county in CAD_URLS:
        if county in lower:
            return county
        if f"{county} county" in lower:
            return county

    # City lookup
    for city, county in CITY_TO_COUNTY.items():
        if city in lower:
            return county

    return None


def get_cad_urls(county: str) -> dict:
    """Get the CAD/GIS URLs for a county."""
    return CAD_URLS.get(county.lower(), {})


# ═══════════════════════════════════════════════════════════════════
# LOI FIELD EXTRACTION HELPER
# ═══════════════════════════════════════════════════════════════════

def extract_loi_property_fields(text: str) -> dict:
    """
    Extract property transaction fields from LOI/contract text
    using regex patterns. This runs as a supplement to the main
    extraction engine — specifically pulling fields that map
    to matter_properties columns.

    Returns dict of field_name → extracted_value.
    """
    fields = {}

    if not text:
        return fields

    # ── Property location ────────────────────────────────────────
    loc_patterns = [
        r"(?i)(?:located\s+(?:at|on|in))\s+(.+?)(?:\.|,\s+(?:being|containing|consisting))",
        r"(?i)(?:(?:NW|NE|SW|SE|North|South|East|West)\s+(?:corner|quadrant)\s+of\s+)(.+?)(?:\.|,\s+in)",
        r"(?i)(?:Property|Location|Site|Premises)[:\s]+(.+?)(?:\n|Size|Acres|Purchase|Price|Seller|Buyer)",
    ]
    garbage_words = {"described", "herein", "below", "above", "follows", "property", "land", "the"}
    for pat in loc_patterns:
        m = re.search(pat, text[:5000])
        if m:
            loc = m.group(1).strip()
            # Skip single-word garbage captures
            if loc.lower() in garbage_words or len(loc) < 5:
                continue
            # Skip if it starts with common filler
            if re.match(r"(?i)^(the\s+)?(?:property|land)\s+described", loc):
                continue
            fields["property_location"] = loc
            break

    # ── Acreage ──────────────────────────────────────────────────
    m = re.search(r"(?i)(?:approximately|approx\.?|\+/-)?\s*([\d,.]+)\s*(?:acres?|ac\.?)", text[:5000])
    if m:
        try:
            fields["acreage"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # ── Purchase price (lump sum) ────────────────────────────────
    price_patterns = [
        r"(?i)Purchase\s+Price[^$\d]*?\$\s*([\d,]+(?:\.\d{2})?)",
        r"(?i)Price[:\s]+\$\s*([\d,]+(?:\.\d{2})?)",
        r"(?i)for\s+the\s+(?:sum|amount)\s+of\s+\$\s*([\d,]+(?:\.\d{2})?)",
    ]
    for pat in price_patterns:
        m = re.search(pat, text[:5000])
        if m:
            try:
                fields["purchase_price"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
            break

    # ── Price per unit ───────────────────────────────────────────
    m = re.search(r"(?i)\$\s*([\d,.]+)\s+(?:Per|per)\s+((?:Net\s+)?(?:Usable\s+)?(?:Square\s+Foot|SF|sq\.?\s*ft\.?|Acre))", text[:5000])
    if m:
        try:
            fields["price_per_unit"] = float(m.group(1).replace(",", ""))
            unit_raw = m.group(2).lower()
            if "acre" in unit_raw:
                fields["price_unit"] = "acre"
            else:
                fields["price_unit"] = "sqft"
        except ValueError:
            pass

    # ── Earnest money ────────────────────────────────────────────
    m = re.search(r"(?i)(?:Earnest\s+Money|Initial\s+Earnest|escrow|deposit)[^$]*?\$\s*([\d,]+(?:\.\d{2})?)", text[:5000])
    if m:
        try:
            fields["earnest_money"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # ── Feasibility period ───────────────────────────────────────
    m = re.search(r"(?i)(?:Feasibility|Due\s+Diligence|Inspection)\s+(?:Period)?[^(]*?(\d+)\s*(?:\(\d+\)\s*)?(?:days?)", text[:5000])
    if m:
        fields["feasibility_days"] = int(m.group(1))

    # ── Closing days ─────────────────────────────────────────────
    m = re.search(r"(?i)Closing[^.]*?(\d+)\s*(?:business\s+)?days?", text[:5000])
    if m:
        fields["closing_days"] = int(m.group(1))

    # ── Title company ────────────────────────────────────────────
    m = re.search(r"(?i)Title(?:\s+Company)?[:\s]+([A-Z][\w\s,&.\'-]+?)(?:\s*[-–,]|\n|$)", text[:5000])
    if m:
        fields["title_company"] = m.group(1).strip()

    # ── Title officer ────────────────────────────────────────────
    m = re.search(r"(?i)Title[^.]*?[-–]\s*([A-Z][a-z]+\s+[A-Z][a-z]+)", text[:5000])
    if m:
        fields["title_officer"] = m.group(1).strip()

    # ── Zoning ───────────────────────────────────────────────────
    m = re.search(r"(?i)Zoning[:\s]+([A-Z][\w\s-]+?)(?:\n|$)", text[:5000])
    if m:
        fields["zoning"] = m.group(1).strip()

    # ── Commission ───────────────────────────────────────────────
    m = re.search(r"(?i)commission\s+of\s+([\d.]+)\s*%", text[:8000])
    if m:
        try:
            fields["commission_rate"] = float(m.group(1))
        except ValueError:
            pass

    # ── Broker ───────────────────────────────────────────────────
    m = re.search(r"(?i)(?:broker|brokerage)[^.]*?(?:due\s+to|paid\s+to|payable\s+to)\s+([A-Z][\w\s,&.]+?)(?:\.|,\s+paid|$)", text[:8000])
    if m:
        fields["broker_name"] = m.group(1).strip()

    # ── Buyer / Seller ───────────────────────────────────────────
    m = re.search(r"(?i)Buyer[:\s]+([A-Z][\w\s,&.]+?)(?:\s+and/or|\n|$)", text[:3000])
    if m:
        fields["buyer"] = m.group(1).strip()
    m = re.search(r"(?i)Seller[:\s]+([A-Z][\w\s,&.]+?)(?:\n|$)", text[:3000])
    if m:
        fields["seller"] = m.group(1).strip()

    # ── CAD Parcel / Account IDs ──────────────────────────────────
    # Match patterns like "Parcel IDs: 1055690 and 1056029"
    # or "CAD Account: R12345" or "Property ID: 1234567"
    parcel_patterns = [
        r"(?i)(?:Parcel|Account|Property)\s*(?:ID|IDs|No|Nos|Numbers?|#)[:\s]+([\d]+(?:\s*(?:and|,|&)\s*[\d]+)*)",
        r"(?i)CAD\s+(?:Parcel|Account)\s*(?:ID|IDs|No|Nos)?[:\s]+([\d]+(?:\s*(?:and|,|&)\s*[\d]+)*)",
        r"(?i)(?:Collin|Denton|Dallas|Tarrant|Grayson|Kaufman)\s+CAD\s+(?:Parcel|Account)\s*(?:ID|IDs)?[:\s]+([\d]+(?:\s*(?:and|,|&)\s*[\d]+)*)",
    ]
    for pat in parcel_patterns:
        m = re.search(pat, text[:8000])
        if m:
            raw = m.group(1).strip()
            # Parse out individual IDs
            ids = [x.strip() for x in re.split(r"\s*(?:and|,|&)\s*", raw) if x.strip().isdigit()]
            if ids:
                fields["cad_account_number"] = ids[0]
                if len(ids) > 1:
                    fields["parcel_ids"] = ids
            break

    # ── County identification ────────────────────────────────────
    location_text = fields.get("property_location", "") + " " + text[:3000]
    county = identify_county(location_text)
    if county:
        fields["county"] = county

    return fields


# ═══════════════════════════════════════════════════════════════════
# POST-EXTRACTION HOOK
# ═══════════════════════════════════════════════════════════════════

def should_create_property(document_type_code: str) -> bool:
    """Check if this document type should trigger property creation."""
    return document_type_code in (
        "loi", "letter_of_intent",
        "purchase_agreement", "purchase_contract",
        "contract",  # only if matter_type is real estate
    )


async def create_property_from_extraction(
    pool,
    tenant_id: str,
    matter_id: str,
    document_id: str,
    extraction_run_id: str,
    content_text: str,
    matter_type: str = None,
) -> Optional[str]:
    """
    Post-extraction hook: create a matter_properties row from
    extracted LOI/contract fields.

    Returns property_id if created, None if skipped.
    """
    # Only create for real estate matters
    if matter_type and "real" not in matter_type.lower():
        return None

    fields = extract_loi_property_fields(content_text)
    if not fields.get("property_location") and not fields.get("acreage"):
        log.info("No property fields extracted from doc %s, skipping property creation", document_id)
        return None

    tid = (tenant_id or "").strip()
    prop_id = uuid.uuid4()

    county = fields.get("county")
    cad_info = get_cad_urls(county) if county else {}

    async with pool.acquire() as conn:
        # Check if property already exists for this matter
        existing = await conn.fetchval(
            """SELECT id FROM matter_properties
               WHERE TRIM(tenant_id) = $1 AND matter_id = $2
               LIMIT 1""",
            tid, uuid.UUID(matter_id),
        )
        if existing:
            log.info("Property already exists for matter %s, skipping auto-create", matter_id)
            return str(existing)

        await conn.execute(
            """INSERT INTO matter_properties (
                   id, tenant_id, matter_id, property_name,
                   street_address, county, acreage, zoning,
                   purchase_price, price_per_unit, price_unit,
                   earnest_money, feasibility_days, closing_days,
                   title_company, title_officer,
                   broker_name, commission_rate,
                   cad_url, gis_url, gis_embed_url,
                   extracted_from_doc_id, extraction_run_id,
                   scrape_status
               ) VALUES (
                   $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,'pending'
               )""",
            prop_id, tid, uuid.UUID(matter_id),
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
            uuid.UUID(document_id),
            uuid.UUID(extraction_run_id),
        )

    log.info("Created property %s for matter %s from extraction", prop_id, matter_id)
    return str(prop_id)
