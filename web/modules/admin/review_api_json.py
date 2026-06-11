"""
modules/admin/review_api_json.py
================================

JSON API backing the React review surfaces (PII / Contacts / Spine).
Prefix: /api/review   Auth: request.state.current_user + tenant_id.

Reads (all three) + writes that need no migration:
  - spine: POST status update (accept / reject / re-propose)
  - contacts: POST dedup resolve (merge / keep_separate / reject)
PII triage writes are deferred to migration 0053 (pii_review_decisions).

Patent Pending - Series 2/3 - D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.admin.review_json")
router = APIRouter(prefix="/api/review", tags=["review-json"])

MARCUS = "d389d889-4fe9-41c9-b74e-622d93d244f8"
_MASK_TYPES = {"ssn", "credit_card", "bank_routing", "ein", "passport",
               "drivers_license"}

# contact_id FK tables (bigint, no unique constraint) — straight re-point
_FK_SIMPLE = [
    ("communication_log", "contact_id"),
    ("deal_party_roles", "contact_id"),
    ("document_contacts", "promoted_to_contact_id"),
    ("document_parties", "promoted_to_contact_id"),
    ("expert_witnesses", "contact_id"),
    ("mediations", "mediator_contact_id"),
]


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _uid(request: Request):
    u = getattr(request.state, "current_user", None)
    return getattr(u, "id", None) if u else None


def _require(request: Request) -> str:
    if not getattr(request.state, "current_user", None):
        raise HTTPException(401, "Not authenticated")
    tid = _tid(request)
    if not tid:
        raise HTTPException(401, "Tenant not resolved")
    return tid


def _mask(pii_type: str, value: str) -> str:
    if not value:
        return value
    if pii_type in _MASK_TYPES:
        digits = "".join(ch for ch in value if ch.isalnum())
        tail = digits[-4:] if len(digits) >= 4 else digits
        return f"\u2022\u2022\u2022\u2022 {tail}"
    return value


# ===========================================================================
# PII  (read-only; triage writes -> migration 0053)
# ===========================================================================
@router.get("/pii/rollup")
async def pii_rollup(request: Request, pii_type: Optional[str] = None):
    tid = _require(request)
    where = ["TRIM(tenant_id) = :tid", "is_pii = true", "superseded_by_run_id IS NULL"]
    params: Dict[str, Any] = {"tid": tid}
    if pii_type:
        where.append("pii_type = :pt")
        params["pt"] = pii_type
    async with AsyncSessionLocal() as db:
        summary = [dict(m) for m in (await db.execute(text("""
            SELECT pii_type,
                   COUNT(*) AS n_rows,
                   COUNT(DISTINCT normalized_value) AS n_values,
                   COUNT(DISTINCT dms_document_id) AS n_docs,
                   ROUND(AVG(confidence)::numeric, 2) AS avg_conf
            FROM document_entities
            WHERE TRIM(tenant_id) = :tid AND is_pii = true
              AND superseded_by_run_id IS NULL
            GROUP BY pii_type ORDER BY n_rows DESC
        """), {"tid": tid})).mappings().fetchall()]
        rows = (await db.execute(text(f"""
            SELECT pii_type, normalized_value,
                   COUNT(*) AS occurrences,
                   COUNT(DISTINCT dms_document_id) AS doc_count,
                   MAX(confidence) AS max_conf, MIN(confidence) AS min_conf
            FROM document_entities
            WHERE {' AND '.join(where)}
            GROUP BY pii_type, normalized_value
            ORDER BY pii_type, doc_count DESC, max_conf DESC, normalized_value
        """), params)).mappings().fetchall()
        decisions = (await db.execute(text("""
            SELECT pii_type, normalized_value, decision, redact
            FROM pii_review_decisions WHERE TRIM(tenant_id) = :tid
        """), {"tid": tid})).mappings().fetchall()
    dec_val = {(d["pii_type"], d["normalized_value"]): d for d in decisions if d["normalized_value"] is not None}
    dec_type = {d["pii_type"]: d for d in decisions if d["normalized_value"] is None}
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r["pii_type"], []).append({
            "normalized_value": r["normalized_value"],
            "display_value": _mask(r["pii_type"], r["normalized_value"] or ""),
            "occurrences": int(r["occurrences"]),
            "doc_count": int(r["doc_count"]),
            "max_conf": float(r["max_conf"] or 0),
            "min_conf": float(r["min_conf"] or 0),
            "decision": (dec_val.get((r["pii_type"], r["normalized_value"])) or {}).get("decision"),
        })
    return JSONResponse({"type_summary": [
        {**s, "n_rows": int(s["n_rows"]), "n_values": int(s["n_values"]),
         "n_docs": int(s["n_docs"]), "avg_conf": float(s["avg_conf"] or 0),
         "type_decision": (dec_type.get(s["pii_type"]) or {}).get("decision")}
        for s in summary], "groups": groups})


@router.get("/pii/detail")
async def pii_detail(request: Request, pii_type: str, value: str):
    tid = _require(request)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT de.id, de.dms_document_id, d.file_path, de.char_start,
                   de.char_end, de.page_number, de.confidence, de.entity_text,
                   de.extraction_method
            FROM document_entities de
            LEFT JOIN dms_documents d ON d.id = de.dms_document_id
            WHERE TRIM(de.tenant_id) = :tid AND de.is_pii = true
              AND de.superseded_by_run_id IS NULL
              AND de.pii_type = :pt AND de.normalized_value = :val
            ORDER BY de.confidence DESC, d.file_path
            LIMIT 500
        """), {"tid": tid, "pt": pii_type, "val": value})).mappings().fetchall()
    occ = [{
        "id": str(r["id"]),
        "dms_document_id": str(r["dms_document_id"]) if r["dms_document_id"] else None,
        "file_name": os.path.basename(r["file_path"]) if r["file_path"] else "(unknown)",
        "char_start": r["char_start"], "char_end": r["char_end"],
        "page_number": r["page_number"], "confidence": float(r["confidence"] or 0),
        "entity_text": r["entity_text"], "extraction_method": r["extraction_method"],
    } for r in rows]
    return JSONResponse({"pii_type": pii_type, "value": value,
                         "display_value": _mask(pii_type, value), "occurrences": occ})


@router.post("/pii/decision")
async def pii_decision(request: Request, payload: Dict[str, Any] = Body(...)):
    tid = _require(request)
    uid = _uid(request)
    pii_type = (payload.get("pii_type") or "").strip()
    value = payload.get("value")  # None / omitted => whole-type decision
    decision = (payload.get("decision") or "").strip()
    if not pii_type or decision not in ("confirmed", "dismissed", "excluded"):
        raise HTTPException(400, "pii_type required; decision must be confirmed|dismissed|excluded")
    redact = payload.get("redact")
    if redact is None:
        redact = (decision == "confirmed")
    notes = payload.get("notes")
    async with AsyncSessionLocal() as db:
        if value is None:
            await db.execute(text("""
                INSERT INTO pii_review_decisions
                    (tenant_id, pii_type, normalized_value, decision, redact, reviewed_by, review_notes)
                VALUES (:tid, :pt, NULL, :dec, :red, :uid, :notes)
                ON CONFLICT (tenant_id, pii_type) WHERE normalized_value IS NULL
                DO UPDATE SET decision=EXCLUDED.decision, redact=EXCLUDED.redact,
                    reviewed_by=EXCLUDED.reviewed_by, review_notes=EXCLUDED.review_notes,
                    reviewed_at=now(), updated_at=now()
            """), {"tid": tid, "pt": pii_type, "dec": decision, "red": redact,
                   "uid": uid, "notes": notes})
        else:
            await db.execute(text("""
                INSERT INTO pii_review_decisions
                    (tenant_id, pii_type, normalized_value, decision, redact, reviewed_by, review_notes)
                VALUES (:tid, :pt, :val, :dec, :red, :uid, :notes)
                ON CONFLICT (tenant_id, pii_type, normalized_value) WHERE normalized_value IS NOT NULL
                DO UPDATE SET decision=EXCLUDED.decision, redact=EXCLUDED.redact,
                    reviewed_by=EXCLUDED.reviewed_by, review_notes=EXCLUDED.review_notes,
                    reviewed_at=now(), updated_at=now()
            """), {"tid": tid, "pt": pii_type, "val": value, "dec": decision,
                   "red": redact, "uid": uid, "notes": notes})
        await db.commit()
    return JSONResponse({"ok": True, "pii_type": pii_type, "value": value,
                         "decision": decision, "redact": bool(redact),
                         "scope": "type" if value is None else "value"})


# ===========================================================================
# CONTACTS  (dedup conflict queue + resolve)
# ===========================================================================
@router.get("/contacts/dedup")
async def contacts_dedup(request: Request):
    tid = _require(request)
    async with AsyncSessionLocal() as db:
        cands = (await db.execute(text("""
            SELECT id, contact_ids, signal_type, confidence, notes, detected_at
            FROM contact_dedup_candidates
            WHERE TRIM(tenant_id) = :tid AND review_outcome IS NULL
            ORDER BY confidence DESC, detected_at DESC LIMIT 200
        """), {"tid": tid})).mappings().fetchall()
        all_ids: set = set()
        for c in cands:
            for i in (c["contact_ids"] or []):
                all_ids.add(int(i))
        by_id: Dict[int, Dict] = {}
        if all_ids:
            for r in (await db.execute(text("""
                SELECT c.id, c.full_name, c.email, c.phone, c.company, c.firm_name,
                       (SELECT COUNT(*) FROM matter_contacts mc
                         WHERE mc.contact_id = c.id AND TRIM(mc.tenant_id)=TRIM(c.tenant_id)) AS n_matters
                FROM contacts c WHERE TRIM(c.tenant_id) = :tid AND c.id = ANY(:ids)
            """), {"tid": tid, "ids": list(all_ids)})).mappings().fetchall():
                by_id[int(r["id"])] = {
                    "id": int(r["id"]), "full_name": r["full_name"], "email": r["email"],
                    "phone": r["phone"], "company": r["company"], "firm_name": r["firm_name"],
                    "n_matters": int(r["n_matters"] or 0)}
    clusters = []
    for c in cands:
        members = [by_id[int(i)] for i in (c["contact_ids"] or []) if int(i) in by_id]
        if len(members) < 2:
            continue
        clusters.append({
            "id": c["id"], "signal_type": c["signal_type"],
            "confidence": float(c["confidence"] or 0), "notes": c["notes"],
            "contacts": members,
            "suggested_canonical_id": max(
                members, key=lambda m: (1 if m["email"] else 0, m["n_matters"], -m["id"]))["id"],
        })
    return JSONResponse({"clusters": clusters})


async def _merge_contacts(db, tid: str, canonical_id: int, loser_ids: List[int]):
    # matter_contacts (dedup-safe)
    for link in (await db.execute(text("""
        SELECT id, matter_id FROM matter_contacts
        WHERE contact_id = ANY(:l) AND TRIM(tenant_id) = :tid
    """), {"l": loser_ids, "tid": tid})).fetchall():
        ex = (await db.execute(text("""
            SELECT 1 FROM matter_contacts WHERE contact_id = :c AND matter_id = :m
              AND TRIM(tenant_id) = :tid
        """), {"c": canonical_id, "m": link.matter_id, "tid": tid})).fetchone()
        if ex:
            await db.execute(text("DELETE FROM matter_contacts WHERE id = :id"), {"id": link.id})
        else:
            await db.execute(text("UPDATE matter_contacts SET contact_id = :c WHERE id = :id"),
                             {"c": canonical_id, "id": link.id})
    # matter_contact_proposals (dedup-safe; uq_mcp_pending_pair)
    for prop in (await db.execute(text("""
        SELECT id, matter_id FROM matter_contact_proposals
        WHERE contact_id = ANY(:l) AND TRIM(tenant_id) = :tid
    """), {"l": loser_ids, "tid": tid})).fetchall():
        ex = (await db.execute(text("""
            SELECT 1 FROM matter_contact_proposals WHERE contact_id = :c
              AND matter_id = :m AND TRIM(tenant_id) = :tid
        """), {"c": canonical_id, "m": prop.matter_id, "tid": tid})).fetchone()
        if ex:
            await db.execute(text("DELETE FROM matter_contact_proposals WHERE id = :id"), {"id": prop.id})
        else:
            await db.execute(text("UPDATE matter_contact_proposals SET contact_id = :c WHERE id = :id"),
                             {"c": canonical_id, "id": prop.id})
    for tbl, col in _FK_SIMPLE:
        await db.execute(text(f"UPDATE {tbl} SET {col} = :c WHERE {col} = ANY(:l)"),
                         {"c": canonical_id, "l": loser_ids})
    await db.execute(text("""
        UPDATE contacts SET contact_type = 'archived', updated_at = NOW()
        WHERE id = ANY(:l) AND TRIM(tenant_id) = :tid
    """), {"l": loser_ids, "tid": tid})


@router.post("/contacts/dedup/{candidate_id}/resolve")
async def contacts_dedup_resolve(request: Request, candidate_id: str,
                                 payload: Dict[str, Any] = Body(...)):
    tid = _require(request)
    uid = _uid(request)
    action = (payload.get("action") or "").strip()
    async with AsyncSessionLocal() as db:
        cand = (await db.execute(text("""
            SELECT contact_ids FROM contact_dedup_candidates
            WHERE id = :id AND TRIM(tenant_id) = :tid AND review_outcome IS NULL
        """), {"id": candidate_id, "tid": tid})).fetchone()
        if not cand:
            raise HTTPException(404, "Candidate not found or already reviewed")
        ids = [int(i) for i in (cand.contact_ids or [])]

        if action == "keep_separate":
            outcome = "kept_separate"
        elif action == "reject":
            outcome = "rejected_false_positive"
        elif action == "merge":
            canon = int(payload.get("canonical_contact_id") or 0)
            if canon not in ids:
                raise HTTPException(400, "canonical_contact_id not in cluster")
            losers = [i for i in ids if i != canon]
            await _merge_contacts(db, tid, canon, losers)
            await db.execute(text("""
                UPDATE contact_dedup_candidates
                SET review_outcome = 'merged', canonical_contact_id = :c,
                    reviewed_by = :u, reviewed_at = NOW()
                WHERE id = :id
            """), {"c": canon, "u": uid, "id": candidate_id})
            await db.commit()
            return JSONResponse({"ok": True, "action": "merged", "canonical": canon})
        else:
            raise HTTPException(400, "action must be merge|keep_separate|reject")

        await db.execute(text("""
            UPDATE contact_dedup_candidates
            SET review_outcome = :o, reviewed_by = :u, reviewed_at = NOW()
            WHERE id = :id
        """), {"o": outcome, "u": uid, "id": candidate_id})
        await db.commit()
    return JSONResponse({"ok": True, "action": outcome})


# ===========================================================================
# SPINE  (accepted allegations + status triage)
# ===========================================================================
@router.get("/spine")
async def spine_list(request: Request, matter_id: str = MARCUS,
                     allegation_type: Optional[str] = None, limit: int = 1000):
    import json
    tid = _require(request)
    where = ["TRIM(a.tenant_id) = :tid", "a.matter_id = CAST(:m AS uuid)",
             "a.superseded_by_run_id IS NULL", "a.status <> 'rejected'"]
    params: Dict[str, Any] = {"tid": tid, "m": matter_id, "lim": limit}
    if allegation_type:
        where.append("a.allegation_type = :at")
        params["at"] = allegation_type

    def _as_list(v):
        if v is None:
            return []
        if isinstance(v, str):
            try:
                return json.loads(v) or []
            except Exception:
                return []
        return list(v)

    async with AsyncSessionLocal() as db:
        summary = [dict(m) for m in (await db.execute(text("""
            SELECT allegation_type, status, COUNT(*) AS n,
                   ROUND(AVG(confidence)::numeric, 2) AS avg_conf
            FROM allegations
            WHERE TRIM(tenant_id) = :tid AND matter_id = CAST(:m AS uuid)
              AND superseded_by_run_id IS NULL
            GROUP BY allegation_type, status ORDER BY allegation_type, status
        """), {"tid": tid, "m": matter_id})).mappings().fetchall()]
        rows = (await db.execute(text(f"""
            SELECT a.id, a.allegation_type, a.status, a.confidence,
                   a.allegation_text, a.source_doc_id, a.section_id,
                   a.source_char_start, a.source_char_end, a.attribution,
                   a.attributes->>'source_filename' AS source_filename,
                   a.attributes->'sali_iris' AS sali_iris,
                   ct.topic_label
            FROM allegations a
            LEFT JOIN case_topics ct ON ct.id = a.case_topic_id
            WHERE {' AND '.join(where)}
            ORDER BY a.allegation_type, a.confidence DESC
            LIMIT :lim
        """), params)).mappings().fetchall()

        iris = set()
        for r in rows:
            for iri in _as_list(r["sali_iris"]):
                if iri:
                    iris.add(iri)
        sali_map: Dict[str, Any] = {}
        if iris:
            cons = (await db.execute(text("""
                SELECT iri, sali_code, pref_label, branch
                FROM sali_concepts WHERE iri = ANY(:iris)
            """), {"iris": list(iris)})).mappings().fetchall()
            sali_map = {c["iri"]: {"iri": c["iri"], "sali_code": c["sali_code"],
                                   "pref_label": c["pref_label"], "branch": c["branch"]}
                        for c in cons}

    def _resolve(v):
        out = []
        for iri in _as_list(v):
            if not iri:
                continue
            out.append(sali_map.get(iri, {"iri": iri, "sali_code": None,
                                          "pref_label": None, "branch": None}))
        return out

    allegations = [{
        "id": str(r["id"]), "allegation_type": r["allegation_type"],
        "status": r["status"], "confidence": float(r["confidence"] or 0),
        "allegation_text": r["allegation_text"],
        "source_doc_id": str(r["source_doc_id"]) if r["source_doc_id"] else None,
        "source_filename": r["source_filename"],
        "section_id": str(r["section_id"]) if r["section_id"] else None,
        "source_char_start": r["source_char_start"],
        "source_char_end": r["source_char_end"],
        "attribution": r["attribution"], "topic_label": r["topic_label"],
        "sali": _resolve(r["sali_iris"]),
    } for r in rows]

    pleadings: Dict[str, Any] = {}
    for a in allegations:
        key = a["source_doc_id"] or "_none"
        p = pleadings.get(key)
        if not p:
            p = {"source_doc_id": a["source_doc_id"],
                 "source_filename": a["source_filename"] or "(unknown document)",
                 "total": 0, "accepted": 0, "by_type": {}, "_iris": {}}
            pleadings[key] = p
        p["total"] += 1
        if a["status"] == "accepted":
            p["accepted"] += 1
        p["by_type"][a["allegation_type"]] = p["by_type"].get(a["allegation_type"], 0) + 1
        for s in a["sali"]:
            p["_iris"][s["iri"]] = s
    pleading_list = []
    for p in pleadings.values():
        p["sali"] = sorted(p.pop("_iris").values(),
                           key=lambda s: (s.get("pref_label") or s.get("iri") or ""))
        pleading_list.append(p)
    pleading_list.sort(key=lambda p: (-p["total"], p["source_filename"]))

    return JSONResponse({
        "matter_id": matter_id,
        "type_summary": [{**s, "n": int(s["n"]), "avg_conf": float(s["avg_conf"] or 0)}
                         for s in summary],
        "pleadings": pleading_list,
        "allegations": allegations})


@router.post("/spine/{allegation_id}/status")
async def spine_set_status(request: Request, allegation_id: str,
                           payload: Dict[str, Any] = Body(...)):
    tid = _require(request)
    new_status = (payload.get("status") or "").strip()
    if new_status not in ("accepted", "rejected", "proposed"):
        raise HTTPException(400, "status must be accepted|rejected|proposed")
    async with AsyncSessionLocal() as db:
        res = await db.execute(text("""
            UPDATE allegations SET status = :s, updated_at = NOW()
            WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid
              AND superseded_by_run_id IS NULL
        """), {"s": new_status, "id": allegation_id, "tid": tid})
        await db.commit()
    return JSONResponse({"ok": True, "id": allegation_id, "status": new_status,
                         "updated": res.rowcount})


# ===========================================================================
# SALI  (typeahead picker + attach/detach IRIs on an allegation)
# ===========================================================================
@router.get("/sali/search")
async def sali_search(request: Request, q: str = "", limit: int = 20):
    _require(request)
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse({"results": []})
    like = f"%{q}%"
    starts = q + "%"
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT iri, sali_code, pref_label, branch, definition
            FROM sali_concepts
            WHERE is_active
              AND (pref_label ILIKE :like
                   OR sali_code ILIKE :like
                   OR EXISTS (SELECT 1 FROM unnest(alt_labels) al WHERE al ILIKE :like))
            ORDER BY (CASE WHEN pref_label ILIKE :starts THEN 0
                           WHEN sali_code ILIKE :starts THEN 1 ELSE 2 END),
                     (branch = 'area_of_law') DESC, pref_label
            LIMIT :lim
        """), {"like": like, "starts": starts, "lim": max(1, min(limit, 50))})).mappings().fetchall()
    return JSONResponse({"results": [
        {"iri": r["iri"], "sali_code": r["sali_code"], "pref_label": r["pref_label"],
         "branch": r["branch"], "definition": r["definition"]} for r in rows]})


@router.post("/spine/{allegation_id}/sali")
async def spine_sali_edit(request: Request, allegation_id: str,
                          payload: Dict[str, Any] = Body(...)):
    import json
    tid = _require(request)
    action = (payload.get("action") or "add").strip()
    iri = (payload.get("iri") or "").strip()
    if action not in ("add", "remove"):
        raise HTTPException(400, "action must be add|remove")
    if not iri:
        raise HTTPException(400, "iri required")
    async with AsyncSessionLocal() as db:
        con = (await db.execute(text("""
            SELECT iri, sali_code, pref_label, branch
            FROM sali_concepts WHERE iri = :iri AND is_active
        """), {"iri": iri})).mappings().first()
        if action == "add" and not con:
            raise HTTPException(400, "unknown SALI iri")
        row = (await db.execute(text("""
            SELECT attributes FROM allegations
            WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid
              AND superseded_by_run_id IS NULL
        """), {"id": allegation_id, "tid": tid})).mappings().first()
        if not row:
            raise HTTPException(404, "allegation not found")
        attrs = row["attributes"]
        if isinstance(attrs, str):
            attrs = json.loads(attrs) if attrs else {}
        attrs = dict(attrs or {})
        cur = list(attrs.get("sali_iris") or [])
        added = list(attrs.get("sali_human_added") or [])
        if action == "add":
            if iri not in cur:
                cur.append(iri)
            if iri not in added:
                added.append(iri)
        else:
            cur = [x for x in cur if x != iri]
            added = [x for x in added if x != iri]
        attrs["sali_iris"] = cur
        attrs["sali_human_added"] = added
        await db.execute(text("""
            UPDATE allegations
            SET attributes = CAST(:a AS jsonb), updated_at = NOW()
            WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid
        """), {"a": json.dumps(attrs), "id": allegation_id, "tid": tid})
        await db.commit()
    return JSONResponse({"ok": True, "action": action, "iri": iri,
                         "sali": ({"iri": con["iri"], "sali_code": con["sali_code"],
                                   "pref_label": con["pref_label"], "branch": con["branch"]}
                                  if con else None)})


# ===========================================================================
# SPINE bulk status + new fact ; matter ISSUES (case_topics) ; pleading detach
# ===========================================================================
@router.post("/spine/bulk-status")
async def spine_bulk_status(request: Request, payload: Dict[str, Any] = Body(...)):
    tid = _require(request)
    status = (payload.get("status") or "").strip()
    if status not in ("accepted", "rejected", "proposed"):
        raise HTTPException(400, "status must be accepted|rejected|proposed")
    ids = [str(x) for x in (payload.get("ids") or []) if x]
    if not ids:
        return JSONResponse({"ok": True, "updated": 0})
    async with AsyncSessionLocal() as db:
        res = await db.execute(text("""
            UPDATE allegations SET status = :s, updated_at = NOW()
            WHERE TRIM(tenant_id) = :tid AND superseded_by_run_id IS NULL
              AND id::text = ANY(:ids)
        """), {"s": status, "tid": tid, "ids": ids})
        await db.commit()
    return JSONResponse({"ok": True, "updated": res.rowcount, "status": status})


@router.post("/spine/fact")
async def spine_new_fact(request: Request, payload: Dict[str, Any] = Body(...)):
    import json
    tid = _require(request)
    matter_id = (payload.get("matter_id") or "").strip()
    text_val = (payload.get("allegation_text") or "").strip()
    atype = (payload.get("allegation_type") or "factual").strip()
    source_doc_id = (payload.get("source_doc_id") or "").strip() or None
    iris_in = [str(x) for x in (payload.get("sali_iris") or []) if x]
    if not matter_id:
        raise HTTPException(400, "matter_id required")
    if not text_val:
        raise HTTPException(400, "allegation_text required")
    if atype not in ("factual", "legal", "defense", "counterclaim"):
        atype = "factual"
    async with AsyncSessionLocal() as db:
        sali = []
        if iris_in:
            cons = (await db.execute(text("""
                SELECT iri, sali_code, pref_label, branch FROM sali_concepts
                WHERE iri = ANY(:iris) AND is_active
            """), {"iris": iris_in})).mappings().fetchall()
            sali = [{"iri": c["iri"], "sali_code": c["sali_code"],
                     "pref_label": c["pref_label"], "branch": c["branch"]} for c in cons]
        valid_iris = [s["iri"] for s in sali]
        filename = None
        if source_doc_id:
            fr = (await db.execute(text("""
                SELECT regexp_replace(file_path,'^.*/','') AS fn FROM dms_documents
                WHERE id = CAST(:d AS uuid) AND TRIM(tenant_id) = :tid
            """), {"d": source_doc_id, "tid": tid})).mappings().first()
            filename = fr["fn"] if fr else None
        attrs = {"sali_iris": valid_iris, "sali_human_added": valid_iris,
                 "source_filename": filename, "party_side_as_pled": "manual",
                 "grounding": "manual", "human_authored": True}
        row = (await db.execute(text("""
            INSERT INTO allegations (tenant_id, matter_id, allegation_text, allegation_type,
                                     source_doc_id, status, confidence, attribution, attributes)
            VALUES (:tid, CAST(:m AS uuid), :txt, :atype,
                    CASE WHEN :sd = '' THEN NULL ELSE CAST(:sd AS uuid) END,
                    'accepted', 1.0, 'human', CAST(:a AS jsonb))
            RETURNING id
        """), {"tid": tid, "m": matter_id, "txt": text_val, "atype": atype,
               "sd": source_doc_id or "", "a": json.dumps(attrs)})).mappings().first()
        await db.commit()
    return JSONResponse({"ok": True, "allegation": {
        "id": str(row["id"]), "allegation_type": atype, "status": "accepted",
        "confidence": 1.0, "allegation_text": text_val,
        "source_doc_id": source_doc_id, "source_filename": filename,
        "section_id": None, "source_char_start": None, "source_char_end": None,
        "attribution": "human", "topic_label": None, "sali": sali}})


@router.get("/issues")
async def issues_list(request: Request, matter_id: str = MARCUS):
    tid = _require(request)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT id, topic_label, attribution, status,
                   attributes->>'sali_iri' AS sali_iri,
                   attributes->>'sali_code' AS sali_code
            FROM case_topics
            WHERE TRIM(tenant_id) = :tid AND matter_id = CAST(:m AS uuid)
              AND superseded_by_run_id IS NULL AND status <> 'rejected'
            ORDER BY topic_label
        """), {"tid": tid, "m": matter_id})).mappings().fetchall()
    return JSONResponse({"matter_id": matter_id, "issues": [
        {"id": str(r["id"]), "topic_label": r["topic_label"], "sali_iri": r["sali_iri"],
         "sali_code": r["sali_code"], "attribution": r["attribution"], "status": r["status"]}
        for r in rows]})


@router.post("/issues")
async def issues_add(request: Request, payload: Dict[str, Any] = Body(...)):
    import json
    tid = _require(request)
    matter_id = (payload.get("matter_id") or "").strip()
    iri = (payload.get("iri") or "").strip()
    if not matter_id or not iri:
        raise HTTPException(400, "matter_id and iri required")
    async with AsyncSessionLocal() as db:
        con = (await db.execute(text("""
            SELECT iri, sali_code, pref_label FROM sali_concepts WHERE iri = :iri AND is_active
        """), {"iri": iri})).mappings().first()
        if not con:
            raise HTTPException(400, "unknown SALI iri")
        ex = (await db.execute(text("""
            SELECT id, topic_label FROM case_topics
            WHERE TRIM(tenant_id) = :tid AND matter_id = CAST(:m AS uuid)
              AND superseded_by_run_id IS NULL AND status <> 'rejected'
              AND attributes->>'sali_iri' = :iri
            LIMIT 1
        """), {"tid": tid, "m": matter_id, "iri": iri})).mappings().first()
        if ex:
            return JSONResponse({"ok": True, "existed": True, "issue": {
                "id": str(ex["id"]), "topic_label": ex["topic_label"], "sali_iri": iri,
                "sali_code": con["sali_code"], "attribution": None, "status": "accepted"}})
        attrs = {"sali_iri": con["iri"], "sali_code": con["sali_code"], "human_authored": True}
        row = (await db.execute(text("""
            INSERT INTO case_topics (tenant_id, matter_id, topic_label, status, confidence, attribution, attributes)
            VALUES (:tid, CAST(:m AS uuid), :label, 'accepted', 1.0, 'human', CAST(:a AS jsonb))
            RETURNING id
        """), {"tid": tid, "m": matter_id, "label": con["pref_label"], "a": json.dumps(attrs)})).mappings().first()
        await db.commit()
    return JSONResponse({"ok": True, "existed": False, "issue": {
        "id": str(row["id"]), "topic_label": con["pref_label"], "sali_iri": con["iri"],
        "sali_code": con["sali_code"], "attribution": "human", "status": "accepted"}})


@router.post("/issues/{topic_id}/remove")
async def issues_remove(request: Request, topic_id: str):
    tid = _require(request)
    async with AsyncSessionLocal() as db:
        res = await db.execute(text("""
            UPDATE case_topics SET status = 'rejected', updated_at = NOW()
            WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid AND superseded_by_run_id IS NULL
        """), {"id": topic_id, "tid": tid})
        await db.commit()
    return JSONResponse({"ok": True, "removed": res.rowcount})


@router.post("/spine/pleading-sali")
async def pleading_sali_edit(request: Request, payload: Dict[str, Any] = Body(...)):
    import json
    tid = _require(request)
    matter_id = (payload.get("matter_id") or "").strip()
    source_doc_id = (payload.get("source_doc_id") or "").strip()
    iri = (payload.get("iri") or "").strip()
    action = (payload.get("action") or "remove").strip()
    if action != "remove":
        raise HTTPException(400, "only remove supported at pleading scope")
    if not (matter_id and source_doc_id and iri):
        raise HTTPException(400, "matter_id, source_doc_id, iri required")
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT id, attributes FROM allegations
            WHERE TRIM(tenant_id) = :tid AND matter_id = CAST(:m AS uuid)
              AND source_doc_id = CAST(:d AS uuid) AND superseded_by_run_id IS NULL
        """), {"tid": tid, "m": matter_id, "d": source_doc_id})).mappings().fetchall()
        changed = 0
        for r in rows:
            attrs = r["attributes"]
            if isinstance(attrs, str):
                attrs = json.loads(attrs) if attrs else {}
            attrs = dict(attrs or {})
            cur = list(attrs.get("sali_iris") or [])
            if iri not in cur:
                continue
            attrs["sali_iris"] = [x for x in cur if x != iri]
            attrs["sali_human_added"] = [x for x in (attrs.get("sali_human_added") or []) if x != iri]
            await db.execute(text("""
                UPDATE allegations SET attributes = CAST(:a AS jsonb), updated_at = NOW()
                WHERE id = CAST(:id AS uuid)
            """), {"a": json.dumps(attrs), "id": str(r["id"])})
            changed += 1
        await db.commit()
    return JSONResponse({"ok": True, "detached_from": changed, "iri": iri})


# === spine PDF viewer (geometry-backed page+box locator) ===
# Highlight boxes come from the geometry substrate (doc_layout_tokens) — the
# same source redaction / translation consume. The allegation's verbatim source
# slice is rebased into geometry space (resolve-don't-transcribe), because
# allegation offsets index content_text, not the geometry canonical text (until
# canonical-at-ingest converges them). Boxes are percent-of-page (0-100) for the
# shared PdfAnnotationViewer overlay. Docs whose geometry can't build (scanned /
# OCR-only) fall back to a PyMuPDF text search, converted to the same convention.

def _norm_ws(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def _pdf_search_percent(fp, target, fallback=""):
    out = {"matched": False, "boxes": [], "page": None, "num_pages": None}
    try:
        import fitz
    except Exception:
        return out
    attempts = [t for t in (_norm_ws(target), _norm_ws(fallback)) if t]
    if not attempts:
        return out
    try:
        doc = fitz.open(fp)
    except Exception:
        return out
    try:
        out["num_pages"] = doc.page_count
        for src in attempts:
            words = src.split(" ")
            needles = [" ".join(words[:n]) for n in (10, 7, 5, 4) if len(words) >= 3]
            for pno in range(doc.page_count):
                page = doc[pno]
                pw = page.rect.width or 1.0
                ph = page.rect.height or 1.0
                rects = []
                for nd in needles:
                    if len(nd) >= 8:
                        found = page.search_for(nd, quads=False)
                        if found:
                            rects = list(found)
                            break
                if not rects:
                    continue
                for i in range(0, min(len(words), 80), 6):
                    ph2 = " ".join(words[i:i + 6])
                    if len(ph2) >= 8:
                        rects.extend(page.search_for(ph2, quads=False))
                    if len(rects) > 80:
                        break
                x0 = min(r.x0 for r in rects)
                y0 = min(r.y0 for r in rects)
                x1 = max(r.x1 for r in rects)
                y1 = max(r.y1 for r in rects)
                out["boxes"] = [{"page_number": pno + 1,
                                 "x": round(x0 / pw * 100, 3),
                                 "y": round(y0 / ph * 100, 3),
                                 "width": round((x1 - x0) / pw * 100, 3),
                                 "height": round((y1 - y0) / ph * 100, 3)}]
                out["page"] = pno + 1
                out["matched"] = True
                return out
    finally:
        doc.close()
    return out


@router.get("/source-pdf/{doc_id}")
async def source_pdf(request: Request, doc_id: str):
    """Stream a source pleading PDF (dms_documents) inline, tenant-scoped."""
    tid = _require(request)
    async with AsyncSessionLocal() as db:
        row = (await db.execute(text("""
            SELECT file_path FROM dms_documents
            WHERE id = CAST(:d AS uuid) AND TRIM(tenant_id) = :tid
        """), {"d": doc_id, "tid": tid})).mappings().fetchone()
    if not row or not row["file_path"]:
        raise HTTPException(404, "Document not found")
    fp = row["file_path"]
    if not os.path.isfile(fp):
        raise HTTPException(404, "Source file not accessible")
    import mimetypes
    mime = mimetypes.guess_type(fp)[0] or "application/pdf"
    with open(fp, "rb") as f:
        data = f.read()
    fname = os.path.basename(fp).replace('"', "")
    return Response(content=data, media_type=mime, headers={
        "Content-Disposition": 'inline; filename="%s"' % fname,
        "Cache-Control": "private, max-age=300",
    })


@router.get("/spine/{allegation_id}/locator")
async def spine_locator(request: Request, allegation_id: str):
    """Resolve an allegation to {pdf_url, page, boxes(percent), matched, source}."""
    tid = _require(request)
    async with AsyncSessionLocal() as db:
        row = (await db.execute(text("""
            SELECT a.source_doc_id, a.source_char_start, a.source_char_end,
                   a.allegation_text, d.file_path, d.content_text
            FROM allegations a
            JOIN dms_documents d ON d.id = a.source_doc_id
            WHERE a.id = CAST(:id AS uuid) AND TRIM(a.tenant_id) = :tid
        """), {"id": allegation_id, "tid": tid})).mappings().fetchone()
    if not row or not row["source_doc_id"]:
        raise HTTPException(404, "Allegation or source document not found")

    cs, ce = row["source_char_start"], row["source_char_end"]
    content = row["content_text"] or ""
    verbatim = ""
    if cs is not None and ce is not None and 0 <= cs < ce <= len(content):
        verbatim = content[cs:ce]
    needle = verbatim or row["allegation_text"] or ""
    doc_id = str(row["source_doc_id"])

    resp = {
        "source_doc_id": doc_id,
        "pdf_url": "/api/review/source-pdf/%s" % doc_id,
        "snippet": _norm_ws(needle)[:600],
        "page": None, "boxes": [], "matched": False,
        "source": None, "geometry_status": None,
    }

    from starlette.concurrency import run_in_threadpool
    from modules.ediscovery.services.geometry_service import (
        get_or_build_geometry, resolve_boxes_by_text,
    )
    try:
        g = await run_in_threadpool(get_or_build_geometry, tid, "dms", doc_id)
        resp["geometry_status"] = g.get("status")
        if g.get("status") in ("built", "cached"):
            loc = await run_in_threadpool(
                resolve_boxes_by_text, tid, "dms", doc_id, needle)
            if loc.get("matched"):
                resp["boxes"] = loc["boxes"]
                resp["matched"] = True
                resp["page"] = loc["boxes"][0]["page_number"] if loc["boxes"] else None
                resp["source"] = "geometry"
    except Exception as e:  # geometry is best-effort; fall back below
        log.warning("spine_locator geometry failed for %s: %s", doc_id, e)

    if not resp["matched"] and row["file_path"] and os.path.isfile(row["file_path"]):
        fb = await run_in_threadpool(
            _pdf_search_percent, row["file_path"], verbatim, row["allegation_text"] or "")
        if fb.get("matched"):
            resp["boxes"] = fb["boxes"]
            resp["matched"] = True
            resp["page"] = fb["page"]
            resp["source"] = "pdf_search"
    return resp
