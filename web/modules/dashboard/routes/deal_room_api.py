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

# ── Due-Diligence tags (reuse eDiscovery tags / document_tags) ──────
# Transactional DD review: a basic, user-extensible tag vocabulary applied to a
# deal's DMS documents. Reuses the eDiscovery `tags` (vocabulary) + `document_tags`
# (applied) tables. Transactional matters never have eDiscovery collections, so
# tagging targets DMS files directly (documents.id, created on first tag).
_TXN_TAGS = [
    ("Reviewed", "Status", "#16A34A"), ("Needs Review", "Status", "#D97706"),
    ("Issue Flagged", "Status", "#DC2626"), ("Cleared", "Status", "#2563EB"),
    ("Title", "Diligence", "#7C3AED"), ("Survey", "Diligence", "#7C3AED"),
    ("Financials", "Diligence", "#7C3AED"), ("Lease / Estoppel", "Diligence", "#7C3AED"),
    ("Environmental", "Diligence", "#7C3AED"), ("Zoning / Permits", "Diligence", "#7C3AED"),
    ("Litigation / Liens", "Diligence", "#7C3AED"), ("Entity / Org", "Diligence", "#7C3AED"),
    ("Tax", "Diligence", "#7C3AED"), ("Insurance", "Diligence", "#7C3AED"),
    ("Contract / PSA", "Doc Type", "#0891B2"), ("Amendment", "Doc Type", "#0891B2"),
    ("LOI", "Doc Type", "#0891B2"), ("Closing Doc", "Doc Type", "#0891B2"),
    ("Key Document", "Flag", "#CA8A04"), ("Privileged", "Flag", "#BE123C"),
    ("Confidential", "Flag", "#BE123C"),
]


async def _ensure_txn_tags(session, tid):
    """Idempotently seed the basic transactional DD tag set (tenant-shared,
    matter_id NULL, tag_set='transactional')."""
    for name, cat, color in _TXN_TAGS:
        await session.execute(sa_text("""
            INSERT INTO tags (tenant_id, matter_id, name, tag_set, category, color, is_system)
            SELECT :tid, NULL, :name, 'transactional', :cat, :color, TRUE
            WHERE NOT EXISTS (
                SELECT 1 FROM tags WHERE TRIM(tenant_id) = :tid AND tag_set = 'transactional'
                  AND matter_id IS NULL AND name = :name)
        """), {"tid": tid, "name": name, "cat": cat, "color": color})


async def _deal_doc_id(session, tid, matter_id, path, filename, create):
    """Resolve (or optionally create) documents.id for a deal DMS file."""
    from modules.dms.services.matter_workspace_api import _resolve_root, _safe_path
    root, _ = await _resolve_root(tid, matter_id)
    abs_path = None
    if root:
        try:
            abs_path = _safe_path(root, (path + "/" + filename) if path else filename)
        except Exception:
            abs_path = None
    if abs_path:
        r = await session.execute(sa_text(
            "SELECT id::text FROM documents WHERE storage_path = :sp AND TRIM(tenant_id) = :tid LIMIT 1"
        ), {"sp": abs_path, "tid": tid})
        row = r.fetchone()
        if row:
            return row[0]
    if not create:
        return None
    r = await session.execute(sa_text("""
        INSERT INTO documents (tenant_id, matter_id, filename, storage_path, document_type, status)
        VALUES (:tid, CAST(:mid AS uuid), :fn, :sp, 'deal_doc', 'active')
        RETURNING id::text
    """), {"tid": tid, "mid": matter_id, "fn": filename, "sp": abs_path})
    return r.scalar()


@router.get("/deals/{matter_id}/doc-tags")
async def deal_doc_tags(request: Request, matter_id: str, path: str = ""):
    """Available transactional tag vocabulary + tags applied to each file in a
    deal folder. One call powers the tag UI."""
    tid = _tid(request)
    try:
        async with AsyncSessionLocal() as session:
            await _ensure_txn_tags(session, tid)
            av = await session.execute(sa_text("""
                SELECT id::text AS id, name, category, color, (matter_id IS NOT NULL) AS custom
                FROM tags
                WHERE TRIM(tenant_id) = :tid AND tag_set = 'transactional'
                  AND (matter_id IS NULL OR matter_id = CAST(:mid AS uuid))
                ORDER BY category, name
            """), {"tid": tid, "mid": matter_id})
            available = [dict(r) for r in av.mappings().fetchall()]

            from modules.dms.services.matter_workspace_api import _resolve_root, _safe_path
            applied = {}
            root, _ = await _resolve_root(tid, matter_id)
            folder_abs = None
            if root:
                try:
                    folder_abs = _safe_path(root, path) if path else root
                except Exception:
                    folder_abs = None
            if folder_abs:
                like = folder_abs.rstrip("/") + "/%"
                deep = folder_abs.rstrip("/") + "/%/%"
                r = await session.execute(sa_text("""
                    SELECT d.storage_path, dt.tag_id::text AS tag_id, t.name, t.color
                    FROM documents d
                    JOIN document_tags dt ON dt.document_id = d.id AND TRIM(dt.tenant_id) = :tid
                    JOIN tags t ON t.id = dt.tag_id
                    WHERE TRIM(d.tenant_id) = :tid
                      AND d.storage_path LIKE :like AND d.storage_path NOT LIKE :deep
                """), {"tid": tid, "like": like, "deep": deep})
                for row in r.mappings().fetchall():
                    fn = row["storage_path"].rsplit("/", 1)[-1]
                    applied.setdefault(fn, []).append({"id": row["tag_id"], "name": row["name"], "color": row["color"]})
            await session.commit()
            return JSONResponse({"available": available, "applied": applied})
    except Exception as exc:
        logger.error("deal_doc_tags error: %s", exc)
        return JSONResponse({"available": [], "applied": {}, "error": str(exc)})


@router.post("/deals/{matter_id}/doc-tags")
async def deal_doc_tag_apply(request: Request, matter_id: str):
    """Apply/remove a tag on a deal document.
    Body: {path, filename, tag_id, action: 'apply'|'remove'}."""
    tid = _tid(request)
    body = await request.json()
    tag_id = body.get("tag_id"); action = body.get("action", "apply")
    path = body.get("path", "") or ""; filename = body.get("filename", "") or ""
    if not tag_id or not filename:
        return JSONResponse({"error": "tag_id and filename required"}, status_code=400)
    try:
        async with AsyncSessionLocal() as session:
            doc_id = await _deal_doc_id(session, tid, matter_id, path, filename, create=(action == "apply"))
            if not doc_id:
                await session.commit()
                return JSONResponse({"ok": True, "applied": False})
            if action == "remove":
                await session.execute(sa_text("""
                    DELETE FROM document_tags WHERE document_id = CAST(:did AS uuid)
                      AND tag_id = CAST(:tg AS uuid) AND TRIM(tenant_id) = :tid
                """), {"did": doc_id, "tg": tag_id, "tid": tid})
                applied = False
            else:
                ex = await session.execute(sa_text("""
                    SELECT 1 FROM document_tags WHERE document_id = CAST(:did AS uuid)
                      AND tag_id = CAST(:tg AS uuid) AND TRIM(tenant_id) = :tid LIMIT 1
                """), {"did": doc_id, "tg": tag_id, "tid": tid})
                if not ex.fetchone():
                    await session.execute(sa_text("""
                        INSERT INTO document_tags (tenant_id, document_id, tag_id, source, source_table, matter_id)
                        VALUES (:tid, CAST(:did AS uuid), CAST(:tg AS uuid), 'manual', 'dms', CAST(:mid AS uuid))
                    """), {"tid": tid, "did": doc_id, "tg": tag_id, "mid": matter_id})
                applied = True
            await session.commit()
            return JSONResponse({"ok": True, "applied": applied, "document_id": doc_id})
    except Exception as exc:
        logger.error("deal_doc_tag_apply error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/deals/{matter_id}/tags")
async def deal_create_tag(request: Request, matter_id: str):
    """User-add a transactional tag (matter-scoped). Body: {name, category?, color?}."""
    tid = _tid(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    cat = (body.get("category") or "Custom").strip()
    color = (body.get("color") or "#64748B").strip()
    try:
        async with AsyncSessionLocal() as session:
            ex = await session.execute(sa_text("""
                SELECT id::text FROM tags WHERE TRIM(tenant_id) = :tid AND tag_set = 'transactional'
                  AND lower(name) = lower(:name)
                  AND (matter_id IS NULL OR matter_id = CAST(:mid AS uuid)) LIMIT 1
            """), {"tid": tid, "name": name, "mid": matter_id})
            row = ex.fetchone()
            if row:
                tag_id = row[0]
            else:
                r = await session.execute(sa_text("""
                    INSERT INTO tags (tenant_id, matter_id, name, tag_set, category, color, is_system)
                    VALUES (:tid, CAST(:mid AS uuid), :name, 'transactional', :cat, :color, FALSE)
                    RETURNING id::text
                """), {"tid": tid, "mid": matter_id, "name": name, "cat": cat, "color": color})
                tag_id = r.scalar()
            await session.commit()
            return JSONResponse({"id": tag_id, "name": name, "category": cat, "color": color, "custom": True})
    except Exception as exc:
        logger.error("deal_create_tag error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/deals/{matter_id}/dd-collections")
async def deal_dd_collections(request: Request, matter_id: str):
    """Due-diligence collections for a deal, surfaced as virtual folders.

    eDiscovery collections are litigation-centric; transactional deals normally
    keep DD in the 03-Due Diligence DMS folder, so this is typically empty for
    deals. Built to light up automatically if a deal ever has collections."""
    tid = _tid(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT c.id::text AS id,
                       COALESCE(NULLIF(c.name, ''), NULLIF(c.collection_name, ''), 'Collection') AS name,
                       c.status,
                       c.parent_collection_id::text AS parent_id,
                       COALESCE(c.total_docs, 0) AS total_docs,
                       (SELECT count(*) FROM ediscovery_documents d
                          WHERE d.collection_id = c.id) AS doc_count,
                       (SELECT count(*) FROM ediscovery_stage_status ss
                          WHERE ss.collection_id = c.id AND ss.stage = 'embed_lane'
                            AND ss.state = 'done') AS embedded,
                       c.created_at::text AS created_at
                FROM ediscovery_collections c
                WHERE c.matter_id = CAST(:mid AS uuid) AND TRIM(c.tenant_id) = :tid
                ORDER BY c.created_at DESC NULLS LAST, name
            """), {"mid": matter_id, "tid": tid})
            cols = [dict(row) for row in r.mappings().fetchall()]
        return JSONResponse({"collections": cols})
    except Exception as exc:
        logger.error("deal_dd_collections error: %s", exc)
        return JSONResponse({"collections": [], "error": str(exc)})


@router.get("/deals/{matter_id}/dd-collections/{collection_id}/documents")
async def deal_dd_collection_docs(request: Request, matter_id: str, collection_id: str):
    """Documents in one DD collection (virtual folder contents)."""
    tid = _tid(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT id::text AS id, document_id::text AS document_id,
                       file_name, file_size, mime_type, review_status,
                       bates_start, bates_end
                FROM ediscovery_documents
                WHERE collection_id = CAST(:cid AS uuid) AND TRIM(tenant_id) = :tid
                ORDER BY file_name
                LIMIT 500
            """), {"cid": collection_id, "tid": tid})
            docs = [dict(row) for row in r.mappings().fetchall()]
        return JSONResponse({"documents": docs})
    except Exception as exc:
        logger.error("deal_dd_collection_docs error: %s", exc)
        return JSONResponse({"documents": [], "error": str(exc)})


# ── Document access log — attorney report (Deal Center §1.10 / §4) ──────────
@router.get("/deals/{matter_id}/access-log")
async def deal_access_log(request: Request, matter_id: str, limit: int = 300):
    """Attorney-accessible report over the append-only document_access_log for a
    deal. Answers the post-mortem questions: who accessed which DD/transaction
    document, how (view/download), and when — internal staff AND guests.

    Returns a summary, a per-document rollup, a per-actor rollup, and the most
    recent raw events."""
    tid = _tid(request)
    try:
        limit = max(1, min(int(limit or 300), 2000))
    except Exception:
        limit = 300
    try:
        async with AsyncSessionLocal() as session:
            params = {"mid": matter_id, "tid": tid, "lim": limit}

            r_sum = await session.execute(sa_text("""
                SELECT count(*) AS total_accesses,
                       count(DISTINCT al.ediscovery_document_id) AS unique_docs,
                       count(DISTINCT al.actor_user_id) AS unique_actors,
                       count(*) FILTER (WHERE al.actor_is_guest) AS guest_accesses,
                       count(*) FILTER (WHERE al.action = 'download') AS downloads,
                       min(al.accessed_at)::text AS first_access,
                       max(al.accessed_at)::text AS last_access
                FROM document_access_log al
                WHERE al.matter_id = CAST(:mid AS uuid) AND TRIM(al.tenant_id) = :tid
            """), params)
            summary = dict(r_sum.mappings().fetchone() or {})

            r_doc = await session.execute(sa_text("""
                SELECT al.ediscovery_document_id::text AS document_id,
                       COALESCE(max(al.document_name), '(unknown)') AS document_name,
                       count(*) AS accesses,
                       count(DISTINCT al.actor_user_id) AS viewers,
                       count(*) FILTER (WHERE al.action = 'download') AS downloads,
                       max(al.accessed_at)::text AS last_accessed
                FROM document_access_log al
                WHERE al.matter_id = CAST(:mid AS uuid) AND TRIM(al.tenant_id) = :tid
                GROUP BY al.ediscovery_document_id
                ORDER BY max(al.accessed_at) DESC NULLS LAST
                LIMIT 500
            """), params)
            by_document = [dict(x) for x in r_doc.mappings().fetchall()]

            r_act = await session.execute(sa_text("""
                SELECT al.actor_user_id,
                       COALESCE(u.full_name, u.email, al.actor_email, 'Unknown') AS actor,
                       bool_or(al.actor_is_guest) AS is_guest,
                       count(*) AS accesses,
                       count(DISTINCT al.ediscovery_document_id) AS docs,
                       max(al.accessed_at)::text AS last_active
                FROM document_access_log al
                LEFT JOIN users u ON u.id = al.actor_user_id
                WHERE al.matter_id = CAST(:mid AS uuid) AND TRIM(al.tenant_id) = :tid
                GROUP BY al.actor_user_id, u.full_name, u.email, al.actor_email
                ORDER BY count(*) DESC
                LIMIT 200
            """), params)
            by_actor = [dict(x) for x in r_act.mappings().fetchall()]

            r_ev = await session.execute(sa_text("""
                SELECT al.accessed_at::text AS accessed_at,
                       COALESCE(al.document_name, '(unknown)') AS document_name,
                       COALESCE(u.full_name, u.email, al.actor_email, 'Unknown') AS actor,
                       al.actor_is_guest AS is_guest,
                       al.action, al.context, al.ip
                FROM document_access_log al
                LEFT JOIN users u ON u.id = al.actor_user_id
                WHERE al.matter_id = CAST(:mid AS uuid) AND TRIM(al.tenant_id) = :tid
                ORDER BY al.accessed_at DESC
                LIMIT :lim
            """), params)
            events = [dict(x) for x in r_ev.mappings().fetchall()]

        return JSONResponse({
            "summary": summary, "by_document": by_document,
            "by_actor": by_actor, "events": events,
        })
    except Exception as exc:
        logger.error("deal_access_log error: %s", exc)
        return JSONResponse({
            "summary": {}, "by_document": [], "by_actor": [], "events": [],
            "error": str(exc),
        })


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


# ── Review sets = projects (Deal Center §1.11) ──────────────────────────────
# A "review set" is a project (project_type='review') with a review task scoped
# to it. DD collections are assigned to a review set via config.collection_ids.
# On create we reuse the matter's existing review project when asked, else make
# a new one (mirrors the from-template "reuse discovery review project" rule).

async def _review_set_payload(session, tid, matter_id):
    r = await session.execute(sa_text("""
        SELECT p.id::text AS id, p.title, p.status,
               COALESCE(p.config->'collection_ids', '[]'::jsonb)::text AS collection_ids,
               p.created_at::text AS created_at,
               (SELECT count(*) FROM tasks t WHERE t.project_id = p.id) AS task_count,
               (SELECT count(*) FROM tasks t WHERE t.project_id = p.id
                  AND lower(t.status) IN ('done','completed','complete','closed')) AS tasks_done
        FROM projects p
        WHERE p.matter_id = CAST(:mid AS uuid) AND TRIM(p.tenant_id) = :tid
          AND p.project_type = 'review'
        ORDER BY p.created_at DESC NULLS LAST
    """), {"mid": matter_id, "tid": tid})
    sets = []
    for row in r.mappings().fetchall():
        d = dict(row)
        try:
            d["collection_ids"] = json.loads(d.get("collection_ids") or "[]")
        except Exception:
            d["collection_ids"] = []
        sets.append(d)
    return sets


@router.get("/deals/{matter_id}/review-sets")
async def deal_review_sets(request: Request, matter_id: str):
    """List the deal's review sets (review-type projects) + which collections
    each one covers + review-task progress."""
    tid = _tid(request)
    try:
        async with AsyncSessionLocal() as session:
            sets = await _review_set_payload(session, tid, matter_id)
        return JSONResponse({"review_sets": sets})
    except Exception as exc:
        logger.error("deal_review_sets error: %s", exc)
        return JSONResponse({"review_sets": [], "error": str(exc)})


@router.post("/deals/{matter_id}/review-sets")
async def deal_create_review_set(request: Request, matter_id: str):
    """Create a review set (project_type='review') + a review task. If
    reuse_existing is set and the matter already has a review project, reuse it.
    Optionally seed it with a collection_id."""
    tid = _tid(request)
    uid = _uid(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    title = (body.get("title") or "Due Diligence Review").strip() or "Due Diligence Review"
    collection_id = body.get("collection_id")
    reuse = bool(body.get("reuse_existing"))
    try:
        async with AsyncSessionLocal() as session:
            existing = None
            if reuse:
                er = await session.execute(sa_text("""
                    SELECT id::text,
                           COALESCE(config->'collection_ids', '[]'::jsonb)::text AS collection_ids
                    FROM projects
                    WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
                      AND project_type = 'review'
                    ORDER BY created_at ASC NULLS LAST LIMIT 1
                """), {"mid": matter_id, "tid": tid})
                existing = er.mappings().fetchone()

            if existing:
                pid = existing["id"]
                try:
                    cids = json.loads(existing["collection_ids"] or "[]")
                except Exception:
                    cids = []
                if collection_id and collection_id not in cids:
                    cids.append(collection_id)
                    await session.execute(sa_text("""
                        UPDATE projects
                        SET config = jsonb_set(COALESCE(config, '{}'::jsonb),
                                               '{collection_ids}', CAST(:cids AS jsonb), true),
                            updated_at = NOW()
                        WHERE id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid
                    """), {"cids": json.dumps(cids), "pid": pid, "tid": tid})
                await session.commit()
                created = False
            else:
                cids = [collection_id] if collection_id else []
                config = {"kind": "dd_review", "collection_ids": cids}
                pr = await session.execute(sa_text("""
                    INSERT INTO projects
                        (tenant_id, matter_id, title, description, status, priority,
                         created_by, config, sort_order, project_type, created_at, updated_at)
                    VALUES
                        (:tid, CAST(:mid AS uuid), :title, :descr, 'active', 'medium',
                         :uid, CAST(:cfg AS jsonb), 0, 'review', NOW(), NOW())
                    RETURNING id::text
                """), {
                    "tid": tid, "mid": matter_id, "title": title,
                    "descr": "Due-diligence document review set",
                    "uid": uid, "cfg": json.dumps(config),
                })
                pid = pr.fetchone()[0]
                # Scoped review task
                await session.execute(sa_text("""
                    INSERT INTO tasks
                        (tenant_id, matter_id, project_id, title, description,
                         status, priority, task_type, created_by, created_at)
                    VALUES
                        (:tid, CAST(:mid AS uuid), CAST(:pid AS uuid),
                         :title, :descr, 'todo', 'medium', 'review', :uid, NOW())
                """), {
                    "tid": tid, "mid": matter_id, "pid": pid,
                    "title": "Review documents — " + title,
                    "descr": "Complete first-pass review of documents in this review set.",
                    "uid": uid,
                })
                await session.commit()
                created = True

            sets = await _review_set_payload(session, tid, matter_id)
        return JSONResponse({"id": pid, "created": created, "review_sets": sets})
    except Exception as exc:
        logger.error("deal_create_review_set error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/deals/{matter_id}/review-sets/{project_id}/assign")
async def deal_assign_collection(request: Request, matter_id: str, project_id: str):
    """Assign (or remove) a collection to/from a review set."""
    tid = _tid(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    collection_id = body.get("collection_id")
    action = (body.get("action") or "assign").lower()
    if not collection_id:
        return JSONResponse({"error": "collection_id required"}, status_code=400)
    try:
        async with AsyncSessionLocal() as session:
            cr = await session.execute(sa_text("""
                SELECT COALESCE(config->'collection_ids', '[]'::jsonb)::text AS collection_ids
                FROM projects
                WHERE id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid
                  AND project_type = 'review' LIMIT 1
            """), {"pid": project_id, "tid": tid})
            row = cr.mappings().fetchone()
            if not row:
                return JSONResponse({"error": "review set not found"}, status_code=404)
            try:
                cids = json.loads(row["collection_ids"] or "[]")
            except Exception:
                cids = []
            if action == "remove":
                cids = [c for c in cids if c != collection_id]
            elif collection_id not in cids:
                cids.append(collection_id)
            await session.execute(sa_text("""
                UPDATE projects
                SET config = jsonb_set(COALESCE(config, '{}'::jsonb),
                                       '{collection_ids}', CAST(:cids AS jsonb), true),
                    updated_at = NOW()
                WHERE id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid
            """), {"cids": json.dumps(cids), "pid": project_id, "tid": tid})
            await session.commit()
            sets = await _review_set_payload(session, tid, matter_id)
        return JSONResponse({"ok": True, "review_sets": sets})
    except Exception as exc:
        logger.error("deal_assign_collection error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)
