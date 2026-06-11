"""
dashboard/routes/deal_room_api.py
==================================
Key Documents + Deal Points + Subject API

Serves both litigation and transactional matters.
Key documents are universal (complaint, scheduling order, contract, survey).
Deal points and subjects are primarily transactional but available for any matter.

GET  /api/v1/matter/{mid}/deal-room           — composite payload
POST /api/v1/matter/{mid}/key-documents       — link a key document
DELETE /api/v1/matter/{mid}/key-documents/{id} — unlink
POST /api/v1/matter/{mid}/deal-points         — upsert a deal point
DELETE /api/v1/matter/{mid}/deal-points/{id}  — remove
POST /api/v1/matter/{mid}/subject             — upsert matter subject

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations
import json, logging
from datetime import datetime, timezone
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["deal-room"])


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()

def _uid(request: Request):
    u = getattr(request.state, "current_user", None)
    return getattr(u, "id", None) if u else None


# ── Canonical document roles by matter type ──────────────────────
DOCUMENT_ROLES = {
    "litigation": [
        {"code": "complaint",            "label": "Complaint / Petition"},
        {"code": "amended_complaint",    "label": "Amended Complaint"},
        {"code": "answer",               "label": "Answer"},
        {"code": "counterclaim",         "label": "Counterclaim"},
        {"code": "scheduling_order",     "label": "Scheduling Order"},
        {"code": "disclosures",          "label": "Initial Disclosures"},
        {"code": "expert_report",        "label": "Expert Report"},
        {"code": "msj",                  "label": "Motion for Summary Judgment"},
        {"code": "msj_response",         "label": "MSJ Response"},
        {"code": "mediation_brief",      "label": "Mediation Brief"},
        {"code": "settlement_agreement", "label": "Settlement Agreement"},
        {"code": "final_judgment",       "label": "Final Judgment"},
        {"code": "engagement_letter",    "label": "Engagement Letter"},
        {"code": "other",                "label": "Other"},
    ],
    "transactional": [
        {"code": "loi",                  "label": "Letter of Intent"},
        {"code": "contract",             "label": "Contract / PSA"},
        {"code": "amendment",            "label": "Amendment / Addendum"},
        {"code": "title_commitment",     "label": "Title Commitment"},
        {"code": "survey",               "label": "Survey"},
        {"code": "closing_checklist",    "label": "Closing Checklist"},
        {"code": "financing_commitment", "label": "Financing Commitment"},
        {"code": "deed",                 "label": "Deed"},
        {"code": "assignment",           "label": "Assignment"},
        {"code": "easement",             "label": "Easement"},
        {"code": "plat",                 "label": "Plat"},
        {"code": "environmental",        "label": "Environmental Report"},
        {"code": "appraisal",            "label": "Appraisal"},
        {"code": "insurance",            "label": "Insurance Certificate"},
        {"code": "engagement_letter",    "label": "Engagement Letter"},
        {"code": "other",                "label": "Other"},
    ],
}


# ── GET composite deal-room payload ──────────────────────────────

@router.get("/matter/{matter_id}/deal-room")
async def get_deal_room(request: Request, matter_id: str):
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, 400)

    result = {}
    try:
        async with AsyncSessionLocal() as session:
            # Matter type for role list
            mt_r = await session.execute(sa_text("""
                SELECT matter_type FROM matters
                WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
            """), {"mid": matter_id, "tid": tid})
            mt_row = mt_r.fetchone()
            matter_type = mt_row[0] if mt_row else "litigation"
            result["matter_type"] = matter_type
            result["document_roles"] = DOCUMENT_ROLES.get(matter_type, DOCUMENT_ROLES["litigation"])

            # ── Key documents ──
            kd_r = await session.execute(sa_text("""
                SELECT mkd.id, mkd.document_role, mkd.label,
                       mkd.display_order, mkd.notes, mkd.linked_at,
                       d.id::text AS document_id, d.filename,
                       d.original_filename, d.mime_type, d.file_size,
                       d.page_count, d.status AS doc_status,
                       d.storage_path,
                       u.full_name AS linked_by_name
                FROM matter_key_documents mkd
                JOIN documents d ON d.id = mkd.document_id
                LEFT JOIN users u ON u.id = mkd.linked_by
                WHERE mkd.matter_id = CAST(:mid AS uuid)
                  AND TRIM(mkd.tenant_id) = :tid
                ORDER BY mkd.display_order, mkd.linked_at
            """), {"mid": matter_id, "tid": tid})
            key_docs = []
            for row in kd_r.mappings():
                key_docs.append({
                    "id": row["id"],
                    "document_id": row["document_id"],
                    "document_role": row["document_role"],
                    "label": row["label"] or row["original_filename"] or row["filename"],
                    "filename": row["original_filename"] or row["filename"],
                    "mime_type": row["mime_type"],
                    "file_size": row["file_size"],
                    "page_count": row["page_count"],
                    "display_order": row["display_order"],
                    "linked_by": row["linked_by_name"],
                    "linked_at": row["linked_at"].isoformat() if row["linked_at"] else None,
                    "notes": row["notes"],
                    "storage_path": row.get("storage_path") or row.get("storage_path", ""),
                })
            result["key_documents"] = key_docs

            # ── Deal points (both types — litigation can have settlement terms) ──
            dp_r = await session.execute(sa_text("""
                SELECT mdp.id, mdp.point_key, mdp.point_label,
                       mdp.point_value, mdp.point_type, mdp.display_order,
                       mdp.source_clause, mdp.updated_at,
                       d.id::text AS source_doc_id,
                       d.original_filename AS source_doc_name,
                       u.full_name AS updated_by_name
                FROM matter_deal_points mdp
                LEFT JOIN documents d ON d.id = mdp.source_document_id
                LEFT JOIN users u ON u.id = mdp.updated_by
                WHERE mdp.matter_id = CAST(:mid AS uuid)
                  AND TRIM(mdp.tenant_id) = :tid
                ORDER BY mdp.display_order, mdp.created_at
            """), {"mid": matter_id, "tid": tid})
            deal_points = []
            for row in dp_r.mappings():
                deal_points.append({
                    "id": row["id"],
                    "point_key": row["point_key"],
                    "point_label": row["point_label"],
                    "point_value": row["point_value"],
                    "point_type": row["point_type"],
                    "display_order": row["display_order"],
                    "source_doc_id": row["source_doc_id"],
                    "source_doc_name": row["source_doc_name"],
                    "source_clause": row["source_clause"],
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                    "updated_by": row["updated_by_name"],
                })
            result["deal_points"] = deal_points

            # ── Matter subject ──
            ms_r = await session.execute(sa_text("""
                SELECT ms.id, ms.tenant_id, ms.matter_id::text,
                       ms.subject_type, ms.display_name,
                       ms.address_line1, ms.address_line2,
                       ms.city, ms.state, ms.zip_code, ms.county,
                       ms.latitude, ms.longitude, ms.parcel_id,
                       ms.legal_description, ms.acreage,
                       ms.photo_document_id::text,
                       ms.details, ms.data_sources,
                       ms.created_at, ms.updated_at,
                       d.original_filename AS photo_filename
                FROM matter_subjects ms
                LEFT JOIN documents d ON d.id = ms.photo_document_id
                WHERE ms.matter_id = CAST(:mid AS uuid)
                  AND TRIM(ms.tenant_id) = :tid
            """), {"mid": matter_id, "tid": tid})
            ms_row = ms_r.mappings().fetchone()
            if ms_row:
                subj = dict(ms_row)
                # Convert non-serializable types
                for k in list(subj.keys()):
                    v = subj[k]
                    if hasattr(v, 'hex'):  # UUID
                        subj[k] = str(v)
                for k in ("created_at", "updated_at"):
                    if subj.get(k) and hasattr(subj[k], "isoformat"):
                        subj[k] = subj[k].isoformat()
                for k in ("latitude", "longitude", "acreage"):
                    if subj.get(k) is not None:
                        subj[k] = float(subj[k])
                result["subject"] = subj
            else:
                result["subject"] = None


            # ── Key Contacts ──
            kc_r = await session.execute(sa_text("""
                SELECT c.full_name, c.email, c.phone, c.company, mc.role
                FROM matter_contacts mc
                JOIN contacts c ON c.id = mc.contact_id
                WHERE mc.matter_id = CAST(:mid AS uuid)
                  AND TRIM(mc.tenant_id) = :tid
                  AND mc.role IN ('buyer','seller','client','broker','title_company','opposing_counsel','other')
                  AND c.full_name NOT ILIKE '%by and between%'
                  AND c.full_name NOT ILIKE '%LAND DEVELOPMENT, LLC'
                  AND LENGTH(c.full_name) > 3
                ORDER BY CASE mc.role
                    WHEN 'seller' THEN 1 WHEN 'buyer' THEN 2
                    WHEN 'client' THEN 3 WHEN 'other' THEN 4
                    WHEN 'title_company' THEN 5 WHEN 'broker' THEN 6
                    ELSE 7 END, c.full_name
                LIMIT 8
            """), {"mid": matter_id, "tid": tid})
            key_contacts = []
            for row in kc_r.mappings():
                key_contacts.append({
                    "full_name": row["full_name"],
                    "email": row["email"],
                    "phone": row["phone"],
                    "company": row["company"],
                    "role": row["role"],
                })
            result["key_contacts"] = key_contacts

        return JSONResponse(result)

    except Exception as exc:
        logger.error("get_deal_room error: %s", exc)
        return JSONResponse({"error": str(exc)}, 500)


# ── POST link key document ───────────────────────────────────────

@router.post("/matter/{matter_id}/key-documents")
async def link_key_document(request: Request, matter_id: str):
    tid = _tid(request)
    uid = _uid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, 400)

    body = await request.json()
    doc_id = body.get("document_id")
    role = body.get("document_role", "other")
    label = body.get("label")
    notes = body.get("notes")
    order = body.get("display_order", 0)

    if not doc_id:
        return JSONResponse({"error": "document_id required"}, 400)

    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(sa_text("""
                    INSERT INTO matter_key_documents
                        (tenant_id, matter_id, document_id, document_role,
                         label, display_order, linked_by, notes)
                    VALUES (:tid, CAST(:mid AS uuid), CAST(:did AS uuid),
                            :role, :label, :ord, :uid, :notes)
                    ON CONFLICT ON CONSTRAINT uq_mkd_tenant_matter_doc
                    DO UPDATE SET document_role = :role,
                                  label = :label,
                                  display_order = :ord,
                                  notes = :notes
                """), {
                    "tid": tid, "mid": matter_id, "did": doc_id,
                    "role": role, "label": label, "ord": order,
                    "uid": int(uid) if uid else None, "notes": notes,
                })
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error("link_key_document error: %s", exc)
        return JSONResponse({"error": str(exc)}, 500)


# ── DELETE unlink key document ───────────────────────────────────

@router.delete("/matter/{matter_id}/key-documents/{kd_id}")
async def unlink_key_document(request: Request, matter_id: str, kd_id: int):
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, 400)
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(sa_text("""
                    DELETE FROM matter_key_documents
                    WHERE id = :kid AND matter_id = CAST(:mid AS uuid)
                      AND TRIM(tenant_id) = :tid
                """), {"kid": kd_id, "mid": matter_id, "tid": tid})
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error("unlink_key_document error: %s", exc)
        return JSONResponse({"error": str(exc)}, 500)


# ── POST upsert deal point ───────────────────────────────────────

@router.post("/matter/{matter_id}/deal-points")
async def upsert_deal_point(request: Request, matter_id: str):
    tid = _tid(request)
    uid = _uid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, 400)

    body = await request.json()
    point_key = body.get("point_key")
    if not point_key:
        return JSONResponse({"error": "point_key required"}, 400)

    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(sa_text("""
                    INSERT INTO matter_deal_points
                        (tenant_id, matter_id, point_key, point_label,
                         point_value, point_type, source_document_id,
                         source_clause, display_order, updated_by)
                    VALUES (:tid, CAST(:mid AS uuid), :pk, :pl,
                            :pv, :pt,
                            CASE WHEN :sdid = '' THEN NULL
                                 ELSE CAST(:sdid AS uuid) END,
                            :sc, :ord, :uid)
                    ON CONFLICT ON CONSTRAINT uq_mdp_tenant_matter_key
                    DO UPDATE SET point_label = :pl,
                                  point_value = :pv,
                                  point_type = :pt,
                                  source_document_id = CASE WHEN :sdid = '' THEN NULL
                                      ELSE CAST(:sdid AS uuid) END,
                                  source_clause = :sc,
                                  display_order = :ord,
                                  updated_by = :uid,
                                  updated_at = NOW()
                """), {
                    "tid": tid, "mid": matter_id,
                    "pk": point_key,
                    "pl": body.get("point_label", point_key),
                    "pv": body.get("point_value"),
                    "pt": body.get("point_type", "text"),
                    "sdid": body.get("source_document_id", ""),
                    "sc": body.get("source_clause"),
                    "ord": body.get("display_order", 0),
                    "uid": int(uid) if uid else None,
                })
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error("upsert_deal_point error: %s", exc)
        return JSONResponse({"error": str(exc)}, 500)


# ── DELETE deal point ────────────────────────────────────────────

@router.delete("/matter/{matter_id}/deal-points/{dp_id}")
async def delete_deal_point(request: Request, matter_id: str, dp_id: int):
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, 400)
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(sa_text("""
                    DELETE FROM matter_deal_points
                    WHERE id = :dpid AND matter_id = CAST(:mid AS uuid)
                      AND TRIM(tenant_id) = :tid
                """), {"dpid": dp_id, "mid": matter_id, "tid": tid})
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error("delete_deal_point error: %s", exc)
        return JSONResponse({"error": str(exc)}, 500)


# ── POST upsert matter subject ───────────────────────────────────

@router.post("/matter/{matter_id}/subject")
async def upsert_subject(request: Request, matter_id: str):
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, 400)

    body = await request.json()
    subject_type = body.get("subject_type", "real_property")
    display_name = body.get("display_name", "")

    if not display_name:
        return JSONResponse({"error": "display_name required"}, 400)

    details_json = json.dumps(body.get("details", {}))
    data_sources_json = json.dumps(body.get("data_sources", {}))

    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(sa_text("""
                    INSERT INTO matter_subjects
                        (tenant_id, matter_id, subject_type, display_name,
                         address_line1, address_line2, city, state, zip_code,
                         county, latitude, longitude, parcel_id,
                         legal_description, acreage, details, data_sources)
                    VALUES (:tid, CAST(:mid AS uuid), :st, :dn,
                            :a1, :a2, :city, :state, :zip, :county,
                            :lat, :lng, :pid, :ld, :ac,
                            CAST(:det AS jsonb), CAST(:ds AS jsonb))
                    ON CONFLICT ON CONSTRAINT uq_ms_tenant_matter
                    DO UPDATE SET subject_type = :st,
                                  display_name = :dn,
                                  address_line1 = :a1,
                                  address_line2 = :a2,
                                  city = :city,
                                  state = :state,
                                  zip_code = :zip,
                                  county = :county,
                                  latitude = :lat,
                                  longitude = :lng,
                                  parcel_id = :pid,
                                  legal_description = :ld,
                                  acreage = :ac,
                                  details = CAST(:det AS jsonb),
                                  data_sources = CAST(:ds AS jsonb),
                                  updated_at = NOW()
                """), {
                    "tid": tid, "mid": matter_id,
                    "st": subject_type, "dn": display_name,
                    "a1": body.get("address_line1"),
                    "a2": body.get("address_line2"),
                    "city": body.get("city"),
                    "state": body.get("state"),
                    "zip": body.get("zip_code"),
                    "county": body.get("county"),
                    "lat": body.get("latitude"),
                    "lng": body.get("longitude"),
                    "pid": body.get("parcel_id"),
                    "ld": body.get("legal_description"),
                    "ac": body.get("acreage"),
                    "det": details_json,
                    "ds": data_sources_json,
                })
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error("upsert_subject error: %s", exc)
        return JSONResponse({"error": str(exc)}, 500)
