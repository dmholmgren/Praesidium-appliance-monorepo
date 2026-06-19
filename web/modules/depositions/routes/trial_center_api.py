"""
modules/depositions/routes/trial_center_api.py

Trial Center JSON API (Scope v2 §4). Read/write surface over the already-built
trial-exhibit register (trial_exhibits + trial_exhibit_usages), the net-new
objection overlay (trial_exhibit_objections), the derived color-state view
(trial_exhibit_state), and projections of the shared transcript substrate
(deposition_transcripts) + the DMS (documents) filtered to pleadings / motions.

Doctrine: surfaces are projections, never copies. Every read filters by tenant +
matter; exhibits reference documents (document_id, no copy -- S3-005).

Mirrors depo_api/exhibits_api conventions: AsyncSessionLocal + sa_text, _tenant
from request.state, get_current_user, _serialize.
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user
from modules.depositions.routes.depo_api import _tenant, _serialize

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/trial", tags=["trial-center-api"])

# Motion-practice tokens vs. true pleadings (Scope v2 §4.1 -- split the PDF lists).
_MOTION_RX = r"(motion|response|reply|brief)"
_PLEADING_RX = r"(petition|answer|complaint|counter[- ]?claim|cross[- ]?claim|" \
               r"plea to the juris|special appearance|pleading)"
# Litigation discovery: disclosures, requests (interrogatories / RFP / RFA /
# disclosure requests) and their responses. The taxonomy lumps disclosures +
# responses under category 'discovery_response'; served *requests* are usually
# unclassified, so fall back to the free-text document_type for those.
_DISCOVERY_RX = (r"(disclosure|interrogator|request(s)? for (production|admission|disclosure)"
                 r"|\yrfp\y|\yrfa\y|\yrog\y)")


# --------------------------------------------------------------------------- #
#  Overview                                                                    #
# --------------------------------------------------------------------------- #
@router.get("/matters/{matter_id}/overview")
async def overview(request: Request, matter_id: str, user=Depends(get_current_user)):
    """Trial Center landing counts: exhibits by color, transcripts by kind,
    open objections."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            ex = await session.execute(sa_text(
                "SELECT color_state, count(*) n FROM trial_exhibit_state "
                "WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = TRIM(:tid) "
                "GROUP BY color_state"),
                {"mid": matter_id, "tid": tid})
            colors = {r["color_state"]: r["n"] for r in ex.mappings().fetchall()}

            tr = await session.execute(sa_text(
                "SELECT transcript_kind, count(*) n FROM deposition_transcripts "
                "WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = TRIM(:tid) "
                "GROUP BY transcript_kind"),
                {"mid": matter_id, "tid": tid})
            kinds = {r["transcript_kind"]: r["n"] for r in tr.mappings().fetchall()}

            ob = await session.execute(sa_text(
                "SELECT COALESCE(SUM(open_objection_count),0) open, "
                "       COALESCE(SUM(objection_count),0) total "
                "FROM trial_exhibit_state "
                "WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"mid": matter_id, "tid": tid})
            obr = ob.mappings().fetchone()

        return JSONResponse(_serialize({
            "exhibits": {
                "total": sum(colors.values()),
                "admitted": colors.get("green", 0),
                "conditional": colors.get("yellow", 0),
                "not_admitted": colors.get("red", 0),
            },
            "transcripts": {
                "deposition": kinds.get("deposition", 0),
                "trial": kinds.get("trial", 0),
                "hearing": kinds.get("hearing", 0),
            },
            "objections": {"open": obr["open"], "total": obr["total"]},
        }))
    except Exception as e:
        logger.exception("trial overview failed")
        return JSONResponse({"error": str(e)}, 500)


# --------------------------------------------------------------------------- #
#  Exhibits -- grouped by party, with color state + objection overlay         #
# --------------------------------------------------------------------------- #
@router.get("/matters/{matter_id}/exhibits")
async def list_exhibits(request: Request, matter_id: str, user=Depends(get_current_user)):
    """Trial exhibits grouped + collapsible by offering party (§4.3 left nav).
    Each row carries the derived color_state, objection overlay, and the count of
    transcript usages."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT te.id::text, te.party, te.exhibit_number, te.exhibit_label, "
                "       te.document_id::text, te.document_source, te.sponsoring_witness, "
                "       te.status, te.admitted, te.conditional, "
                "       te.marked_page, te.marked_line, te.offered_page, te.offered_line, "
                "       te.ruling_page, te.ruling_line, te.offered_locus, te.ruling_locus, "
                "       te.rr_page_label, te.rr_record_id::text, te.rr_page_first, te.notes, "
                "       st.color_state, st.objection_count, st.open_objection_count, "
                "       st.sustained_count, "
                "       (SELECT count(*) FROM trial_exhibit_usages u WHERE u.exhibit_id = te.id) "
                "         AS usage_count "
                "FROM trial_exhibits te "
                "JOIN trial_exhibit_state st ON st.exhibit_id = te.id "
                "WHERE te.matter_id = CAST(:mid AS uuid) AND TRIM(te.tenant_id) = TRIM(:tid) "
                "ORDER BY te.party NULLS LAST, "
                "  COALESCE(NULLIF(regexp_replace(te.exhibit_number, '\\D', '', 'g'), '')::int, 0), "
                "  te.exhibit_number"),
                {"mid": matter_id, "tid": tid})
            rows = [dict(x) for x in r.mappings().fetchall()]

            # attach objections per exhibit (one extra query, joined in python).
            ids = [x["id"] for x in rows]
            obj_by_ex = {}
            if ids:
                ob = await session.execute(sa_text(
                    "SELECT id::text, exhibit_id::text, objecting_party, basis, source, "
                    "       ruling, ruling_locus, notes "
                    "FROM trial_exhibit_objections "
                    "WHERE exhibit_id = ANY(CAST(:ids AS uuid[])) "
                    "  AND TRIM(tenant_id) = TRIM(:tid) "
                    "ORDER BY created_at"),
                    {"ids": ids, "tid": tid})
                for o in ob.mappings().fetchall():
                    obj_by_ex.setdefault(o["exhibit_id"], []).append(dict(o))

        for x in rows:
            x["objections"] = obj_by_ex.get(x["id"], [])

        # group by party, preserving offering-party order.
        groups, order = {}, []
        for x in rows:
            p = x["party"] or "unassigned"
            if p not in groups:
                groups[p] = []
                order.append(p)
        for x in rows:
            groups[x["party"] or "unassigned"].append(x)

        return JSONResponse(_serialize({
            "parties": order,
            "groups": [{"party": p, "exhibits": groups[p]} for p in order],
            "total": len(rows),
        }))
    except Exception as e:
        logger.exception("trial list_exhibits failed")
        return JSONResponse({"error": str(e)}, 500)


class StatusPatch(BaseModel):
    status: Optional[str] = None          # marked|offered|admitted|withdrawn|...
    admitted: Optional[bool] = None
    conditional: Optional[bool] = None
    sponsoring_witness: Optional[str] = None
    notes: Optional[str] = None


class BulkStatus(BaseModel):
    ids: List[str]
    status: Optional[str] = None
    admitted: Optional[bool] = None
    conditional: Optional[bool] = None


def _status_sets(body) -> tuple:
    """Build SET clauses from any of the status fields present."""
    fields = {}
    for k in ("status", "admitted", "conditional", "sponsoring_witness", "notes"):
        v = getattr(body, k, None)
        if v is not None:
            fields[k] = v
    sets = [f"{k} = :{k}" for k in fields]
    return sets, fields


@router.patch("/exhibits/{exhibit_id}")
async def update_exhibit_status(request: Request, exhibit_id: str, body: StatusPatch,
                                user=Depends(get_current_user)):
    """Offer / admit / conditional / withdraw a single exhibit (§4.3 right rail)."""
    tid = _tenant(request)
    sets, fields = _status_sets(body)
    if not sets:
        return JSONResponse({"error": "no fields"}, 400)
    params = {**fields, "id": exhibit_id, "tid": tid}
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "UPDATE trial_exhibits SET " + ", ".join(sets) + ", updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid) "
                "RETURNING id::text"), params)
            row = r.mappings().fetchone()
            await session.commit()
        if not row:
            return JSONResponse({"error": "not found"}, 404)
        return JSONResponse({"id": row["id"]})
    except Exception as e:
        logger.exception("update_exhibit_status failed")
        return JSONResponse({"error": str(e)}, 500)


@router.post("/exhibits/bulk-status")
async def bulk_status(request: Request, body: BulkStatus, user=Depends(get_current_user)):
    """Bulk offer/admit/withdraw selected exhibits (§4.3 bulk-selectable)."""
    tid = _tenant(request)
    if not body.ids:
        return JSONResponse({"error": "no ids"}, 400)
    sets, fields = _status_sets(body)
    if not sets:
        return JSONResponse({"error": "no fields"}, 400)
    params = {**fields, "ids": body.ids, "tid": tid}
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "UPDATE trial_exhibits SET " + ", ".join(sets) + ", updated_at = now() "
                "WHERE id = ANY(CAST(:ids AS uuid[])) AND TRIM(tenant_id) = TRIM(:tid) "
                "RETURNING id::text"), params)
            n = len(r.mappings().fetchall())
            await session.commit()
        return JSONResponse({"updated": n})
    except Exception as e:
        logger.exception("bulk_status failed")
        return JSONResponse({"error": str(e)}, 500)


# --------------------------------------------------------------------------- #
#  Objection overlay (§2.4)                                                    #
# --------------------------------------------------------------------------- #
class ObjectionIn(BaseModel):
    objecting_party: Optional[str] = None
    basis: Optional[str] = None
    source: Optional[str] = "pretrial_served"      # pretrial_served | trial
    ruling: Optional[str] = "no_ruling"            # sustained|overruled|carried|conditional|no_ruling
    ruling_locus: Optional[str] = None
    notes: Optional[str] = None


@router.post("/exhibits/{exhibit_id}/objections")
async def add_objection(request: Request, exhibit_id: str, body: ObjectionIn,
                        user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            chk = await session.execute(sa_text(
                "SELECT 1 FROM trial_exhibits "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"id": exhibit_id, "tid": tid})
            if not chk.fetchone():
                return JSONResponse({"error": "exhibit not found"}, 404)
            r = await session.execute(sa_text(
                "INSERT INTO trial_exhibit_objections "
                "  (tenant_id, exhibit_id, objecting_party, basis, source, ruling, "
                "   ruling_locus, notes) "
                "VALUES (TRIM(:tid), CAST(:eid AS uuid), :party, :basis, :source, "
                "        :ruling, :locus, :notes) RETURNING id::text"),
                {"tid": tid, "eid": exhibit_id, "party": body.objecting_party,
                 "basis": body.basis, "source": body.source or "pretrial_served",
                 "ruling": body.ruling or "no_ruling", "locus": body.ruling_locus,
                 "notes": body.notes})
            new_id = r.mappings().fetchone()["id"]
            await session.commit()
        return JSONResponse({"id": new_id})
    except Exception as e:
        logger.exception("add_objection failed")
        return JSONResponse({"error": str(e)}, 500)


class ObjectionPatch(BaseModel):
    ruling: Optional[str] = None
    ruling_locus: Optional[str] = None
    basis: Optional[str] = None
    notes: Optional[str] = None


@router.patch("/objections/{objection_id}")
async def update_objection(request: Request, objection_id: str, body: ObjectionPatch,
                           user=Depends(get_current_user)):
    """Resolve a served objection live at trial (fill in the ruling)."""
    tid = _tenant(request)
    fields = {k: v for k, v in body.dict().items() if v is not None}
    if not fields:
        return JSONResponse({"error": "no fields"}, 400)
    sets = [f"{k} = :{k}" for k in fields]
    params = {**fields, "id": objection_id, "tid": tid}
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "UPDATE trial_exhibit_objections SET " + ", ".join(sets) +
                ", updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid) "
                "RETURNING id::text"), params)
            row = r.mappings().fetchone()
            await session.commit()
        if not row:
            return JSONResponse({"error": "not found"}, 404)
        return JSONResponse({"id": row["id"]})
    except Exception as e:
        logger.exception("update_objection failed")
        return JSONResponse({"error": str(e)}, 500)


@router.delete("/objections/{objection_id}")
async def delete_objection(request: Request, objection_id: str,
                           user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(
                "DELETE FROM trial_exhibit_objections "
                "WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = TRIM(:tid)"),
                {"id": objection_id, "tid": tid})
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.exception("delete_objection failed")
        return JSONResponse({"error": str(e)}, 500)


# --------------------------------------------------------------------------- #
#  Exhibit annotations (§4.3 right -- Annotations tab; reads DMS annotations)  #
# --------------------------------------------------------------------------- #
@router.get("/exhibits/{exhibit_id}/annotations")
async def exhibit_annotations(request: Request, exhibit_id: str,
                              user=Depends(get_current_user)):
    """Surface the annotations authored on this exhibit in the trial viewer.
    Exhibit annotations are keyed generically as (source_type='exhibit',
    source_id=exhibit_id) -- the same store the PdfAnnotationViewer writes to --
    so this list stays in sync with what the viewer shows."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            a = await session.execute(sa_text(
                "SELECT id::text, annotation_type, page_number, selected_text, "
                "       comment_text, highlight_color, created_by_name, created_at "
                "FROM doc_annotations "
                "WHERE source_type = 'exhibit' AND source_id = :exid "
                "  AND TRIM(tenant_id) = TRIM(:tid) AND deleted_at IS NULL "
                "ORDER BY page_number NULLS LAST, created_at"),
                {"exid": exhibit_id, "tid": tid})
            anns = [dict(x) for x in a.mappings().fetchall()]
        return JSONResponse(_serialize({"exhibit_id": exhibit_id, "annotations": anns}))
    except Exception as e:
        logger.exception("exhibit_annotations failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/documents/{document_id}/annotations")
async def document_annotations(request: Request, document_id: str,
                               user=Depends(get_current_user)):
    """DMS annotations on a document (Scope v2 §4.1 motion-space addition:
    surface existing DMS annotations in the motion viewer)."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            a = await session.execute(sa_text(
                "SELECT id::text, annotation_type, page_number, selected_text, "
                "       comment_text, highlight_color, created_by_name, created_at "
                "FROM doc_annotations "
                "WHERE document_id = CAST(:doc AS uuid) AND TRIM(tenant_id) = TRIM(:tid) "
                "  AND deleted_at IS NULL "
                "ORDER BY page_number NULLS LAST, created_at"),
                {"doc": document_id, "tid": tid})
            anns = [dict(x) for x in a.mappings().fetchall()]
        return JSONResponse(_serialize({"document_id": document_id, "annotations": anns}))
    except Exception as e:
        logger.exception("document_annotations failed")
        return JSONResponse({"error": str(e)}, 500)


# --------------------------------------------------------------------------- #
#  Pleadings / Motions -- filtered PDF lists (§4.1, "just the PDFs")           #
# --------------------------------------------------------------------------- #
@router.get("/matters/{matter_id}/documents")
async def list_documents(request: Request, matter_id: str,
                         kind: str = Query("pleading"),
                         user=Depends(get_current_user)):
    """Pleadings or Motions PDF list. Effective classification = the live
    classification_results -> taxonomy join when present, else the documents
    free-text type. Surfaces are projections -- docs appear here as they get
    classified upstream."""
    tid = _tenant(request)
    # classification lives on the curated registry: documents.legal_category
    # (taxonomy code). Split the pleading category into motion-practice vs. true
    # pleadings; fall back to the file-type hint for not-yet-classified docs.
    params = {"mid": matter_id, "tid": tid, "motion_rx": _MOTION_RX}
    if kind == "motion":
        cond = ("( (tx.category = 'pleading' AND COALESCE(tx.display_name,'') ~* :motion_rx) "
                "  OR (d.legal_category IS NULL AND COALESCE(d.document_type,'') ~* :motion_rx) )")
    elif kind == "discovery":
        params["disc_rx"] = _DISCOVERY_RX
        cond = ("( tx.category = 'discovery_response' "
                "  OR (d.legal_category IS NULL AND COALESCE(d.document_type,'') ~* :disc_rx) )")
    else:
        cond = "( tx.category = 'pleading' AND COALESCE(tx.display_name,'') !~* :motion_rx )"
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT d.id::text, "
                "       COALESCE(NULLIF(d.title,''), d.original_filename, d.filename) AS title, "
                "       d.mime_type, d.page_count, d.created_at, "
                "       COALESCE(tx.display_name, d.document_type) AS kind_label, tx.category AS tcat, "
                "       d.legal_meta->>'responds_to' AS parent_id, d.legal_category, "
                "       COALESCE(d.legal_meta->>'filing_party', d.legal_meta->>'party', "
                "                d.legal_meta->>'filed_by') AS party, "
                "       COALESCE((d.legal_meta->>'filed_date')::date, d.created_at::date) AS doc_date "
                "FROM documents d "
                "LEFT JOIN document_type_taxonomy tx ON tx.code = d.legal_category "
                "WHERE d.matter_id = CAST(:mid AS uuid) AND TRIM(d.tenant_id) = TRIM(:tid) "
                "  AND " + cond +
                " ORDER BY d.created_at DESC NULLS LAST"),
                params)
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse(_serialize({"kind": kind, "documents": rows}))
    except Exception as e:
        logger.exception("list_documents failed")
        return JSONResponse({"error": str(e)}, 500)


# --------------------------------------------------------------------------- #
#  Transcript projections (§4.2) -- depo / hearing, no second viewer          #
# --------------------------------------------------------------------------- #
@router.get("/matters/{matter_id}/transcripts")
async def list_transcripts(request: Request, matter_id: str,
                           kind: str = Query("deposition"),
                           user=Depends(get_current_user)):
    """Projection of the shared transcript substrate filtered by transcript_kind.
    Rows link out to the canonical viewer at /depositions/transcript/{id}."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT id::text, deponent, title, transcript_kind, volume, trial_day, "
                "       page_first, page_last, qa_count, has_video, source_format, "
                "       status, imported_at "
                "FROM deposition_transcripts "
                "WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = TRIM(:tid) "
                "  AND transcript_kind = :kind "
                "ORDER BY volume NULLS LAST, trial_day NULLS LAST, deponent"),
                {"mid": matter_id, "tid": tid, "kind": kind})
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse(_serialize({"kind": kind, "transcripts": rows}))
    except Exception as e:
        logger.exception("trial list_transcripts failed")
        return JSONResponse({"error": str(e)}, 500)


# --------------------------------------------------------------------------- #
#  Witness-prep projects (§4.3 left, below the party groups)                   #
# --------------------------------------------------------------------------- #
@router.get("/matters/{matter_id}/witness-prep")
async def witness_prep(request: Request, matter_id: str, user=Depends(get_current_user)):
    """Existing witness-prep project objects for this matter (Trial-Mode prep is
    closed-record and reads *from* the exhibit register)."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(
                "SELECT id::text, title, project_type, status "
                "FROM projects "
                "WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = TRIM(:tid) "
                "  AND (project_type ~* 'witness' OR title ~* 'witness') "
                "ORDER BY title"),
                {"mid": matter_id, "tid": tid})
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse(_serialize({"projects": rows}))
    except Exception as e:
        logger.exception("witness_prep failed")
        return JSONResponse({"error": str(e)}, 500)


# --------------------------------------------------------------------------- #
#  Unified search -- pleadings + motions + discovery (right-rail search box)    #
# --------------------------------------------------------------------------- #
# (corpus key, documents kind) -- all three are DMS `documents`, split by the
# same category filters the per-tab list endpoint uses.
_SEARCH_CORPORA = [("pleadings", "pleading"), ("motions", "motion"), ("discovery", "discovery")]


def _doc_cond(kind: str) -> str:
    """The category/type WHERE fragment for a documents `kind` (shared by the
    list and search endpoints). Binds :motion_rx / :disc_rx as needed."""
    if kind == "motion":
        return ("( (tx.category = 'pleading' AND COALESCE(tx.display_name,'') ~* :motion_rx) "
                "  OR (d.legal_category IS NULL AND COALESCE(d.document_type,'') ~* :motion_rx) )")
    if kind == "discovery":
        return ("( tx.category = 'discovery_response' "
                "  OR (d.legal_category IS NULL AND COALESCE(d.document_type,'') ~* :disc_rx) )")
    return "( tx.category = 'pleading' AND COALESCE(tx.display_name,'') !~* :motion_rx )"


@router.get("/matters/{matter_id}/search")
async def search_documents(request: Request, matter_id: str,
                           q: str = Query(""),
                           scopes: str = Query("pleadings,motions,discovery"),
                           user=Depends(get_current_user)):
    """Cross-corpus search backing the right-rail search box. Searches the
    matter's pleadings / motions / discovery DMS documents by title / filename,
    scoped to the matter. Returns grouped, capped hits each carrying the corpus
    needed to open it in the annotation viewer."""
    tid = _tenant(request)
    term = (q or "").strip()
    want = {s.strip() for s in (scopes or "").split(",") if s.strip()}
    out = {"pleadings": [], "motions": [], "discovery": []}
    if len(term) < 2:
        return JSONResponse(_serialize({"query": term, "results": out}))
    like = "%" + term + "%"
    params = {"mid": matter_id, "tid": tid, "motion_rx": _MOTION_RX,
              "disc_rx": _DISCOVERY_RX, "like": like}
    try:
        async with AsyncSessionLocal() as session:
            for key, kind in _SEARCH_CORPORA:
                if key not in want:
                    continue
                r = await session.execute(sa_text(
                    "SELECT d.id::text, "
                    "       COALESCE(NULLIF(d.title,''), d.original_filename, d.filename) AS title, "
                    "       d.mime_type, d.page_count, d.created_at::date AS doc_date, "
                    "       COALESCE(tx.display_name, d.document_type) AS kind_label, "
                    "       COALESCE(d.legal_meta->>'filing_party', d.legal_meta->>'party') AS party "
                    "FROM documents d "
                    "LEFT JOIN document_type_taxonomy tx ON tx.code = d.legal_category "
                    "WHERE d.matter_id = CAST(:mid AS uuid) AND TRIM(d.tenant_id) = TRIM(:tid) "
                    "  AND " + _doc_cond(kind) +
                    "  AND (COALESCE(d.title,'') ILIKE :like OR COALESCE(d.original_filename,'') ILIKE :like "
                    "       OR COALESCE(d.filename,'') ILIKE :like) "
                    "ORDER BY d.created_at DESC NULLS LAST LIMIT 25"),
                    params)
                out[key] = [{**dict(x), "corpus": key} for x in r.mappings().fetchall()]
        return JSONResponse(_serialize({"query": term, "results": out}))
    except Exception as e:
        logger.exception("search_documents failed")
        return JSONResponse({"error": str(e)}, 500)


# --------------------------------------------------------------------------- #
#  Global landing -- matters with trial activity + matter picker search        #
# --------------------------------------------------------------------------- #
@router.get("/matters")
async def trial_matters(request: Request, user=Depends(get_current_user)):
    """Matters with any trial activity (an exhibit register, or trial/hearing
    transcripts) -- backs the top-level Trial Center landing, which drills into
    /trial/home/{matter_id}. Mirrors the depositions module landing feed."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT m.id::text AS matter_id, m.matter_name, m.matter_number,
                       cl.client_name,
                       COALESCE(ex.n, 0) AS exhibit_count,
                       COALESCE(ex.admitted, 0) AS admitted_count,
                       COALESCE(tx.n, 0) AS transcript_count,
                       GREATEST(COALESCE(ex.last_at, '1970-01-01'::timestamptz),
                                COALESCE(tx.last_at, '1970-01-01'::timestamptz)) AS last_activity
                FROM matters m
                LEFT JOIN clients cl ON cl.id = m.client_id AND TRIM(cl.tenant_id) = TRIM(:tid)
                LEFT JOIN (
                    SELECT matter_id, count(*) n,
                           count(*) FILTER (WHERE admitted) admitted,
                           max(updated_at) last_at
                    FROM trial_exhibits WHERE TRIM(tenant_id) = TRIM(:tid)
                    GROUP BY matter_id
                ) ex ON ex.matter_id = m.id
                LEFT JOIN (
                    SELECT matter_id, count(*) n, max(imported_at) last_at
                    FROM deposition_transcripts
                    WHERE TRIM(tenant_id) = TRIM(:tid)
                      AND transcript_kind IN ('trial','hearing')
                    GROUP BY matter_id
                ) tx ON tx.matter_id = m.id
                WHERE TRIM(m.tenant_id) = TRIM(:tid)
                  AND (ex.n IS NOT NULL OR tx.n IS NOT NULL)
                ORDER BY last_activity DESC, m.matter_name
            """), {"tid": tid})
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse(_serialize(rows))
    except Exception as e:
        logger.exception("trial_matters failed")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/matters/search")
async def trial_matters_search(request: Request, q: str = Query(""),
                               user=Depends(get_current_user)):
    """Matter picker for opening any matter in the Trial Center (or dropping a
    transcript onto it). Returns {matters:[{id,number,name,client}]}."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT m.id::text AS id, m.matter_number, m.matter_name, c.client_name
                FROM matters m
                LEFT JOIN clients c ON c.id = m.client_id AND TRIM(c.tenant_id) = TRIM(:tid)
                WHERE TRIM(m.tenant_id) = TRIM(:tid)
                  AND (m.matter_name ILIKE :q OR m.matter_number ILIKE :q
                       OR c.client_name ILIKE :q)
                ORDER BY m.matter_name LIMIT 20
            """), {"tid": tid, "q": f"%{q}%"})
            rows = [dict(x) for x in r.mappings().fetchall()]
        return JSONResponse({"matters": [
            {"id": x["id"], "number": x["matter_number"],
             "name": x["matter_name"], "client": x["client_name"]} for x in rows]})
    except Exception as e:
        logger.exception("trial_matters_search failed")
        return JSONResponse({"error": str(e)}, 500)


# --------------------------------------------------------------------------- #
#  Dashboard -- the Trial Center landing panels (Scope v2 landing redesign).   #
#  Backs BOTH the firm-level landing (/trial, no matter_id -> tenant-wide)     #
#  and the per-matter home (/trial/home/{id} Dashboard tab, matter-scoped).    #
#  Panels: Alerts | Upcoming Hearings & Trials | Hearing & Trial Calendar |    #
#          Trial Tasks | Trial Projects. (Drop Zone is a pure frontend panel.) #
#  Every list is a projection -- read-only, tenant + (optional) matter scoped. #
# --------------------------------------------------------------------------- #
@router.get("/dashboard")
async def dashboard(request: Request, matter_id: Optional[str] = Query(None),
                    user=Depends(get_current_user)):
    tid = _tenant(request)
    mid = matter_id or None
    # matter filter fragment reused across every panel query
    m_and = "AND {col} = CAST(:mid AS uuid)" if mid else ""
    params = {"tid": tid}
    if mid:
        params["mid"] = mid
    try:
        async with AsyncSessionLocal() as session:
            # 1. ALERTS -- active (open, or snoozed-but-expired) routing alerts
            alerts = (await session.execute(sa_text(f"""
                SELECT a.id::text AS id, a.matter_id::text AS matter_id,
                       m.matter_name, a.alert_type, a.title, a.severity,
                       a.status, a.snoozed_until
                FROM routing_alerts a
                LEFT JOIN matters m ON m.id = a.matter_id
                WHERE TRIM(a.tenant_id) = TRIM(:tid)
                  {m_and.format(col='a.matter_id')}
                  AND (a.status = 'open'
                       OR (a.status = 'snoozed' AND a.snoozed_until <= CURRENT_DATE))
                ORDER BY (a.severity = 'critical') DESC,
                         (a.severity = 'warning') DESC, a.created_at DESC
                LIMIT 25
            """), params)).mappings().fetchall()

            # 2. UPCOMING HEARINGS & TRIALS -- future-dated hearing primitives;
            #    trials are hearings whose type matches 'trial'.
            upcoming = (await session.execute(sa_text(f"""
                SELECT h.id::text AS id, h.matter_id::text AS matter_id,
                       m.matter_name, h.hearing_type, h.current_start_at AS start_at,
                       h.judge, h.courtroom, h.status,
                       (h.hearing_type ILIKE '%trial%') AS is_trial
                FROM hearings h
                LEFT JOIN matters m ON m.id = h.matter_id
                WHERE TRIM(h.tenant_id) = TRIM(:tid)
                  {m_and.format(col='h.matter_id')}
                  AND h.current_start_at >= NOW()
                  AND COALESCE(h.status, '') NOT IN ('cancelled', 'concluded')
                ORDER BY h.current_start_at ASC
                LIMIT 20
            """), params)).mappings().fetchall()

            # 3. HEARING & TRIAL CALENDAR -- upcoming calendar events typed
            #    hearing/trial (active lifecycle only).
            calendar = (await session.execute(sa_text(f"""
                SELECT c.id::text AS id, c.matter_id::text AS matter_id,
                       m.matter_name, c.subject, c.start_at, c.end_at,
                       c.event_type, c.location
                FROM calendar_events c
                LEFT JOIN matters m ON m.id = c.matter_id
                WHERE TRIM(c.tenant_id) = TRIM(:tid)
                  {m_and.format(col='c.matter_id')}
                  AND c.event_type IN ('hearing', 'trial')
                  AND COALESCE(c.lifecycle_state, 'active') = 'active'
                  AND c.start_at >= (CURRENT_DATE - INTERVAL '1 day')
                ORDER BY c.start_at ASC
                LIMIT 25
            """), params)).mappings().fetchall()

            # 4. TRIAL TASKS -- open, not-completed tasks (due first).
            tasks = (await session.execute(sa_text(f"""
                SELECT t.id::text AS id, t.matter_id::text AS matter_id,
                       m.matter_name, t.title, t.due_date, t.status, t.priority
                FROM tasks t
                LEFT JOIN matters m ON m.id = t.matter_id
                WHERE TRIM(t.tenant_id) = TRIM(:tid)
                  {m_and.format(col='t.matter_id')}
                  AND COALESCE(t.status, 'open') NOT IN ('completed', 'done', 'cancelled')
                  AND t.completed_at IS NULL
                ORDER BY (t.due_date IS NULL), t.due_date ASC,
                         (t.priority = 'high') DESC
                LIMIT 20
            """), params)).mappings().fetchall()

            # 5. TRIAL PROJECTS -- active projects (witness prep, trial binders…).
            projects = (await session.execute(sa_text(f"""
                SELECT p.id::text AS id, p.matter_id::text AS matter_id,
                       m.matter_name, p.title, p.project_type, p.status, p.due_date
                FROM projects p
                LEFT JOIN matters m ON m.id = p.matter_id
                WHERE TRIM(p.tenant_id) = TRIM(:tid)
                  {m_and.format(col='p.matter_id')}
                  AND COALESCE(p.status, 'active') NOT IN ('closed', 'completed', 'archived')
                  AND p.completed_at IS NULL
                ORDER BY (p.due_date IS NULL), p.due_date ASC, p.created_at DESC
                LIMIT 20
            """), params)).mappings().fetchall()

        return JSONResponse(_serialize({
            "scope": "matter" if mid else "firm",
            "alerts": [dict(x) for x in alerts],
            "upcoming": [dict(x) for x in upcoming],
            "calendar": [dict(x) for x in calendar],
            "tasks": [dict(x) for x in tasks],
            "projects": [dict(x) for x in projects],
        }))
    except Exception as e:
        logger.exception("trial dashboard failed")
        return JSONResponse({"error": str(e)}, 500)


# --------------------------------------------------------------------------- #
#  Exhibit file — page-isolated to the exhibit's Bates range                   #
#  When an exhibit's pages live inside a larger (combined) production PDF, we   #
#  extract just that exhibit's pages (1 Bates/page: page = bates - doc_start)   #
#  and serve the slice, cached. Falls back to the whole document when the      #
#  range can't be mapped. DMS-sourced exhibits stream whole via the DMS route. #
# --------------------------------------------------------------------------- #
@router.get("/exhibits/{exhibit_id}/file")
async def exhibit_file(request: Request, exhibit_id: str, user=Depends(get_current_user)):
    import os
    import re as _re
    from pathlib import Path
    from fastapi.responses import FileResponse, RedirectResponse

    tid = _tenant(request)
    EROOT = os.environ.get("CIFS_EDISCOVERY_MOUNT", "/mnt/ediscovery")
    def _pfxnum(s):
        m = _re.match(r"\s*([A-Za-z][A-Za-z ]*?)\s*0*(\d+)\s*$", s or "")
        return (m.group(1).strip().upper(), int(m.group(2))) if m else (None, None)
    try:
        async with AsyncSessionLocal() as s:
            ex = (await s.execute(sa_text(
                "SELECT id::text, document_id::text, document_source, notes "
                "FROM trial_exhibits WHERE id = CAST(:e AS uuid) AND TRIM(tenant_id) = TRIM(:t)"),
                {"e": exhibit_id, "t": tid})).mappings().fetchone()
            if not ex or not ex["document_id"]:
                return JSONResponse({"error": "no document linked"}, 404)
            if ex["document_source"] != "ediscovery":
                return RedirectResponse("/dms/document/" + ex["document_id"] + "/stream")
            doc = (await s.execute(sa_text(
                "SELECT ed.file_path, ed.native_path, ed.working_path, ed.bates_begin, "
                "       ed.page_count, ed.mime_type, ed.normalized_metadata, ed.rendition_path, ec.storage_path "
                "FROM ediscovery_documents ed "
                "JOIN ediscovery_collections ec ON ec.id = ed.collection_id "
                "WHERE ed.id = CAST(:d AS uuid)"), {"d": ex["document_id"]})).mappings().fetchone()
        # Whole-document serving (HMSA renditions, exact-match exhibits, non-PLF,
        # any mapping failure) is delegated to the proven ediscovery route, which
        # handles renditions/format conversion. We ONLY serve directly when we
        # successfully carve out a true Bates sub-range from a multi-page PDF.
        whole = RedirectResponse("/ediscovery/documents/" + ex["document_id"] + "/file")
        if not doc:
            return whole

        storage = doc["storage_path"] or EROOT
        def _abs(rel):
            if not rel:
                return None
            p = Path(rel) if Path(rel).is_absolute() else Path(storage) / rel
            try:
                if str(p.resolve()).startswith(str(Path(EROOT).resolve())) and p.is_file():
                    return p
            except Exception:
                return None
            return None

        src = doc["file_path"] or doc["native_path"] or doc["working_path"]
        prefix, start = _pfxnum(doc["bates_begin"])
        # explicit per-page Bates map for combined/non-linear productions
        pmap = doc["normalized_metadata"]
        if isinstance(pmap, str):
            import json as _json
            try:
                pmap = _json.loads(pmap)
            except Exception:
                pmap = None
        pmap = pmap.get("bates_page_map") if isinstance(pmap, dict) else None
        if pmap:
            # prefix-agnostic: keep only Bates numbers present in the page map
            cand = [int(x) for x in _re.findall(r"[A-Za-z]{2,}\s*0*(\d+)", ex["notes"] or "")]
            exnums = [n for n in cand if str(n) in pmap]
        elif prefix:
            exnums = [int(x) for x in _re.findall(_re.escape(prefix) + r"\s*0*(\d+)", ex["notes"] or "", _re.I)]
        else:
            exnums = []

        # Resolve a servable PDF. The trial viewer is pdf.js, so we MUST hand it
        # a PDF -- never a raw tiff/jpeg (which the ediscovery route serves for
        # single-page images). Native PDFs (MAM/PLF/ALTA) serve directly; image
        # productions (HMSA &c.) serve their pipeline rendition. The stored
        # rendition_path lane can be stale, so probe both lanes by doc id.
        did = ex["document_id"]
        pdf_path = None
        if (doc["mime_type"] or "").lower() == "application/pdf" or str(src or "").lower().endswith(".pdf"):
            pdf_path = _abs(src)
        if pdf_path is None:
            for rel in (doc["rendition_path"],
                        "renditions/image_stitch/%s.pdf" % did,
                        "renditions/render/%s.pdf" % did):
                pdf_path = _abs(rel)
                if pdf_path:
                    break
        if pdf_path is None:
            return whole   # office docs &c. -> ediscovery route converts/serves

        import fitz
        p0 = p1 = None
        if exnums and (pmap or start is not None):
            eb, ee = min(exnums), max(exnums)
            if pmap:
                ps = [pmap[str(b)] for b in range(eb, ee + 1) if str(b) in pmap]
                if ps:
                    p0, p1 = min(ps), max(ps)
            elif start is not None:
                p0, p1 = eb - start, ee - start
        pdf = None
        try:
            pdf = fitz.open(str(pdf_path))
            pc = pdf.page_count
            if p0 is not None and 0 <= p0 <= p1 < pc and not (p0 == 0 and p1 == pc - 1):
                cache_dir = Path(EROOT) / (tid or "_") / "_trial_exhibit_cache"
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache = cache_dir / (ex["id"] + "_" + did[:8] + "_" + str(p0) + "_" + str(p1) + ".pdf")
                if not cache.exists():
                    out = fitz.open()
                    out.insert_pdf(pdf, from_page=p0, to_page=p1)
                    out.save(str(cache))
                    out.close()
                return FileResponse(str(cache), media_type="application/pdf",
                                    headers={"Content-Disposition": "inline"})
        except Exception:
            logger.exception("exhibit page-slice failed; serving whole pdf")
        finally:
            if pdf is not None:
                pdf.close()
        # whole document -> serve the resolved PDF directly (FileResponse, not a
        # redirect) so the viewer always receives a PDF.
        return FileResponse(str(pdf_path), media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})
    except Exception as e:
        logger.exception("exhibit_file failed")
        return JSONResponse({"error": str(e)}, 500)
