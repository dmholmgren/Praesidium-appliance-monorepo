"""
Module 5i — Intelligence Layer
Router + CRUD for issue map, drift events, knowledge graph, WIAM sessions/findings.
Jobs are in jobs/issue_map_engine.py, jobs/kg_extraction.py, jobs/wiam_engine.py.

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import uuid
import json
import logging

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from core.db.session import get_session_factory
from modules.dashboard.services.auth_helper import get_current_user
from modules.dashboard.services.nav_context import get_nav_context

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ediscovery", tags=["ediscovery-intelligence"])
templates = Jinja2Templates(directory="templates")

# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _get_tenant_id(request: Request) -> str:
    tid = getattr(request.state, 'tenant_id', None)
    if not tid:
        raise HTTPException(status_code=401, detail="No tenant context")
    return tid.strip()


def _get_user_id(request: Request) -> int:
    uid = getattr(request.state, 'user_id', None)
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return int(uid)


async def _matter_access(matter_id: str, tenant_id: str, session_factory) -> dict:
    """Verify matter belongs to tenant; return matter row."""
    async with session_factory() as session:
        row = await session.execute(
            text("SELECT id, name FROM matters WHERE id = :mid AND tenant_id = :tid"),
            {"mid": matter_id, "tid": tenant_id}
        )
        matter = row.mappings().first()
    if not matter:
        raise HTTPException(status_code=404, detail="Matter not found")
    return dict(matter)


def _validate_citations(citations: list) -> bool:
    """
    S2-002 / Claim 11: findings without citations are errors.
    Each citation must have doc_id, page, line.
    """
    if not citations:
        return False
    for c in citations:
        if not all(k in c for k in ('doc_id', 'page', 'line')):
            return False
    return True


# ================================================================== #
# ISSUE MAP                                                           #
# ================================================================== #

@router.get("/matters/{matter_id}/issue-map", response_class=HTMLResponse)
async def issue_map_view(request: Request, matter_id: str):
    tenant_id = _get_tenant_id(request)
    factory = get_session_factory()
    matter = await _matter_access(matter_id, tenant_id, factory)

    async with factory() as session:
        row = await session.execute(
            text("""
                SELECT id, version_num, trigger_type, magnitude,
                       created_at, issue_map
                FROM issue_map_versions
                WHERE matter_id = :mid AND tenant_id = :tid
                ORDER BY version_num DESC
                LIMIT 1
            """),
            {"mid": matter_id, "tid": tenant_id}
        )
        latest = row.mappings().first()

        rows = await session.execute(
            text("""
                SELECT id, version_num, trigger_type, magnitude, created_at
                FROM issue_map_versions
                WHERE matter_id = :mid AND tenant_id = :tid
                ORDER BY version_num DESC
            """),
            {"mid": matter_id, "tid": tenant_id}
        )
        versions = [dict(r) for r in rows.mappings()]

    nav = await get_nav_context(request, page='ediscovery')
    return templates.TemplateResponse(request, "ediscovery/issue_map.html", {
        "matter": matter,
        "matter_id": matter_id,
        "latest": dict(latest) if latest else None,
        "versions": versions,
        "nav": nav,
    })


@router.post("/matters/{matter_id}/issue-map/versions")
async def create_issue_map_version(request: Request, matter_id: str):
    """
    Create a new issue map version (manual trigger).
    Body: { issue_map: {...}, source_doc_ids: [...] }
    """
    tenant_id = _get_tenant_id(request)
    user_id = _get_user_id(request)
    factory = get_session_factory()
    await _matter_access(matter_id, tenant_id, factory)

    body = await request.json()
    issue_map = body.get("issue_map", {})
    source_doc_ids = body.get("source_doc_ids", [])

    async with factory() as session:
        row = await session.execute(
            text("""
                SELECT COALESCE(MAX(version_num), 0) AS max_v
                FROM issue_map_versions
                WHERE matter_id = :mid AND tenant_id = :tid
            """),
            {"mid": matter_id, "tid": tenant_id}
        )
        max_v = row.scalar()
        next_v = max_v + 1

        prior_row = await session.execute(
            text("""
                SELECT issue_map FROM issue_map_versions
                WHERE matter_id = :mid AND tenant_id = :tid
                AND version_num = :v
            """),
            {"mid": matter_id, "tid": tenant_id, "v": max_v}
        )
        prior = prior_row.scalar()

        magnitude = 0.0
        differential = {}
        if prior and issue_map:
            prior_str = json.dumps(prior, sort_keys=True)
            new_str = json.dumps(issue_map, sort_keys=True)
            if prior_str != new_str:
                magnitude = 0.5  # placeholder; full engine in issue_map_engine.py
                differential = {"changed": True, "prior_version": max_v}

        version_id = str(uuid.uuid4())
        await session.execute(
            text("""
                INSERT INTO issue_map_versions
                    (id, matter_id, tenant_id, version_num, trigger_type,
                     source_doc_ids, issue_map, differential, magnitude, created_by)
                VALUES
                    (:id, :mid, :tid, :vnum, 'manual',
                     :src_ids, :issue_map, :diff, :mag, :uid)
            """),
            {
                "id": version_id,
                "mid": matter_id,
                "tid": tenant_id,
                "vnum": next_v,
                "src_ids": source_doc_ids or [],
                "issue_map": json.dumps(issue_map),
                "diff": json.dumps(differential),
                "mag": magnitude,
                "uid": user_id,
            }
        )
        await session.commit()

    return JSONResponse({"version_id": version_id, "version_num": next_v})


@router.get("/matters/{matter_id}/issue-map/versions/{version_num}",
            response_class=HTMLResponse)
async def issue_map_version_view(request: Request, matter_id: str, version_num: int):
    tenant_id = _get_tenant_id(request)
    factory = get_session_factory()
    matter = await _matter_access(matter_id, tenant_id, factory)

    async with factory() as session:
        row = await session.execute(
            text("""
                SELECT id, version_num, trigger_type, magnitude,
                       created_at, issue_map, differential
                FROM issue_map_versions
                WHERE matter_id = :mid AND tenant_id = :tid
                AND version_num = :vnum
            """),
            {"mid": matter_id, "tid": tenant_id, "vnum": version_num}
        )
        version = row.mappings().first()

    if not version:
        raise HTTPException(status_code=404, detail="Version not found")

    nav = await get_nav_context(request, page='ediscovery')
    return templates.TemplateResponse(request, "ediscovery/issue_map_version.html", {
        "matter": matter,
        "matter_id": matter_id,
        "version": dict(version),
        "nav": nav,
    })


@router.get("/matters/{matter_id}/drift-map", response_class=HTMLResponse)
async def drift_map_view(request: Request, matter_id: str):
    tenant_id = _get_tenant_id(request)
    factory = get_session_factory()
    matter = await _matter_access(matter_id, tenant_id, factory)

    async with factory() as session:
        rows = await session.execute(
            text("""
                SELECT id, dimension, drift_type, description,
                       magnitude, version_before, version_after, created_at
                FROM drift_events
                WHERE matter_id = :mid AND tenant_id = :tid
                ORDER BY created_at DESC
            """),
            {"mid": matter_id, "tid": tenant_id}
        )
        events = [dict(r) for r in rows.mappings()]

    nav = await get_nav_context(request, page='ediscovery')
    return templates.TemplateResponse(request, "ediscovery/drift_map.html", {
        "matter": matter,
        "matter_id": matter_id,
        "drift_events": events,
        "nav": nav,
    })


# ================================================================== #
# KNOWLEDGE GRAPH                                                     #
# ================================================================== #

@router.get("/matters/{matter_id}/knowledge-graph", response_class=HTMLResponse)
async def knowledge_graph_view(request: Request, matter_id: str):
    tenant_id = _get_tenant_id(request)
    factory = get_session_factory()
    matter = await _matter_access(matter_id, tenant_id, factory)

    async with factory() as session:
        ent_rows = await session.execute(
            text("""
                SELECT id, entity_type, canonical_name, confidence,
                       attribution, created_at
                FROM kg_entities
                WHERE matter_id = :mid AND tenant_id = :tid
                ORDER BY entity_type, canonical_name
            """),
            {"mid": matter_id, "tid": tenant_id}
        )
        entities = [dict(r) for r in ent_rows.mappings()]

        rel_rows = await session.execute(
            text("""
                SELECT r.id, r.relationship_type, r.confidence,
                       a.canonical_name AS entity_a,
                       b.canonical_name AS entity_b
                FROM kg_relationships r
                JOIN kg_entities a ON r.entity_a_id = a.id
                JOIN kg_entities b ON r.entity_b_id = b.id
                WHERE r.matter_id = :mid AND r.tenant_id = :tid
                ORDER BY r.relationship_type
            """),
            {"mid": matter_id, "tid": tenant_id}
        )
        relationships = [dict(r) for r in rel_rows.mappings()]

    nav = await get_nav_context(request, page='ediscovery')
    return templates.TemplateResponse(request, "ediscovery/knowledge_graph.html", {
        "matter": matter,
        "matter_id": matter_id,
        "entities": entities,
        "relationships": relationships,
        "nav": nav,
    })


@router.post("/matters/{matter_id}/knowledge-graph/entities")
async def create_entity(request: Request, matter_id: str):
    """Human-created entity. attribution='human'."""
    tenant_id = _get_tenant_id(request)
    factory = get_session_factory()
    await _matter_access(matter_id, tenant_id, factory)

    body = await request.json()
    entity_type = body.get("entity_type", "person")
    canonical_name = body.get("canonical_name", "").strip()
    if not canonical_name:
        raise HTTPException(status_code=422, detail="canonical_name required")

    valid_types = {'person', 'organization', 'contract', 'event',
                   'fact', 'admission', 'contradiction'}
    if entity_type not in valid_types:
        raise HTTPException(status_code=422, detail=f"Invalid entity_type: {entity_type}")

    entity_id = str(uuid.uuid4())
    async with factory() as session:
        await session.execute(
            text("""
                INSERT INTO kg_entities
                    (id, matter_id, tenant_id, entity_type, canonical_name,
                     properties, source_doc_id, confidence, attribution)
                VALUES
                    (:id, :mid, :tid, :etype, :name,
                     :props, :src, :conf, 'human')
            """),
            {
                "id": entity_id,
                "mid": matter_id,
                "tid": tenant_id,
                "etype": entity_type,
                "name": canonical_name,
                "props": json.dumps(body.get("properties", {})),
                "src": body.get("source_doc_id"),
                "conf": float(body.get("confidence", 1.0)),
            }
        )
        await session.commit()

    return JSONResponse({"entity_id": entity_id})


# ================================================================== #
# WIAM                                                                #
# ================================================================== #

@router.get("/matters/{matter_id}/wiam", response_class=HTMLResponse)
async def wiam_view(request: Request, matter_id: str):
    tenant_id = _get_tenant_id(request)
    factory = get_session_factory()
    matter = await _matter_access(matter_id, tenant_id, factory)

    async with factory() as session:
        sess_rows = await session.execute(
            text("""
                SELECT id, triggered_by, status, created_at, completed_at
                FROM wiam_sessions
                WHERE matter_id = :mid AND tenant_id = :tid
                ORDER BY created_at DESC
                LIMIT 20
            """),
            {"mid": matter_id, "tid": tenant_id}
        )
        sessions = [dict(r) for r in sess_rows.mappings()]

    nav = await get_nav_context(request, page='ediscovery')
    return templates.TemplateResponse(request, "ediscovery/wiam_session.html", {
        "matter": matter,
        "matter_id": matter_id,
        "sessions": sessions,
        "nav": nav,
    })


@router.post("/matters/{matter_id}/wiam/sessions")
async def create_wiam_session(request: Request, matter_id: str):
    """Create a new WIAM session (enqueues wiam_engine job in M5i-D)."""
    tenant_id = _get_tenant_id(request)
    user_id = _get_user_id(request)
    factory = get_session_factory()
    await _matter_access(matter_id, tenant_id, factory)

    body = await request.json()
    triggered_by = body.get("triggered_by", "manual")
    if triggered_by not in {'manual', 'pre_filing', 'post_depo', 'scheduled'}:
        triggered_by = "manual"

    session_id = str(uuid.uuid4())
    async with factory() as session:
        await session.execute(
            text("""
                INSERT INTO wiam_sessions
                    (id, matter_id, tenant_id, triggered_by, status, created_by)
                VALUES
                    (:id, :mid, :tid, :trig, 'running', :uid)
            """),
            {
                "id": session_id,
                "mid": matter_id,
                "tid": tenant_id,
                "trig": triggered_by,
                "uid": user_id,
            }
        )
        await session.commit()

    return JSONResponse({"session_id": session_id, "status": "running"})


@router.get("/matters/{matter_id}/wiam/sessions/{session_id}")
async def get_wiam_session(request: Request, matter_id: str, session_id: str):
    tenant_id = _get_tenant_id(request)
    factory = get_session_factory()

    async with factory() as session:
        sess_row = await session.execute(
            text("""
                SELECT id, triggered_by, status, created_at, completed_at
                FROM wiam_sessions
                WHERE id = :sid AND matter_id = :mid AND tenant_id = :tid
            """),
            {"sid": session_id, "mid": matter_id, "tid": tenant_id}
        )
        sess = sess_row.mappings().first()
        if not sess:
            raise HTTPException(status_code=404, detail="Session not found")

        find_rows = await session.execute(
            text("""
                SELECT id, finding_type, claim_element, description,
                       citations, confidence, priority, suggested_action,
                       disposition, created_at
                FROM wiam_findings
                WHERE session_id = :sid
                ORDER BY
                    CASE priority WHEN 'high' THEN 1
                                  WHEN 'medium' THEN 2
                                  ELSE 3 END,
                    created_at
            """),
            {"sid": session_id}
        )
        findings = [dict(r) for r in find_rows.mappings()]

    return JSONResponse({"session": dict(sess), "findings": findings})


@router.post("/matters/{matter_id}/wiam/sessions/{session_id}/findings")
async def create_wiam_finding(request: Request, matter_id: str, session_id: str):
    """
    Create a WIAM finding.
    S2-002 / Claim 11: citations are MANDATORY.
    Rejected and logged as error if citations is empty or malformed.
    """
    tenant_id = _get_tenant_id(request)
    factory = get_session_factory()

    body = await request.json()
    citations = body.get("citations", [])

    if not _validate_citations(citations):
        log.error(
            "WIAM finding rejected — missing or malformed citations. "
            "session_id=%s matter_id=%s", session_id, matter_id
        )
        raise HTTPException(
            status_code=422,
            detail="citations required: each entry must have doc_id, page, line"
        )

    finding_id = str(uuid.uuid4())
    async with factory() as session:
        sess_row = await session.execute(
            text("""
                SELECT id FROM wiam_sessions
                WHERE id = :sid AND matter_id = :mid AND tenant_id = :tid
            """),
            {"sid": session_id, "mid": matter_id, "tid": tenant_id}
        )
        if not sess_row.first():
            raise HTTPException(status_code=404, detail="Session not found")

        await session.execute(
            text("""
                INSERT INTO wiam_findings
                    (id, session_id, matter_id, tenant_id, finding_type,
                     claim_element, description, citations, confidence,
                     priority, suggested_action)
                VALUES
                    (:id, :sid, :mid, :tid, :ftype,
                     :elem, :desc, :cit, :conf,
                     :pri, :sug)
            """),
            {
                "id": finding_id,
                "sid": session_id,
                "mid": matter_id,
                "tid": tenant_id,
                "ftype": body.get("finding_type", "own_gap"),
                "elem": body.get("claim_element"),
                "desc": body.get("description", ""),
                "cit": json.dumps(citations),
                "conf": float(body.get("confidence", 1.0)),
                "pri": body.get("priority", "medium"),
                "sug": body.get("suggested_action"),
            }
        )
        await session.commit()

    return JSONResponse({"finding_id": finding_id})


@router.post(
    "/matters/{matter_id}/wiam/sessions/{session_id}/findings/{finding_id}/dispose"
)
async def dispose_wiam_finding(
    request: Request, matter_id: str, session_id: str, finding_id: str
):
    """Accept or dismiss a WIAM finding. Immutable — cannot re-dispose."""
    tenant_id = _get_tenant_id(request)
    user_id = _get_user_id(request)
    factory = get_session_factory()

    body = await request.json()
    disposition = body.get("disposition")
    if disposition not in ("accepted", "dismissed"):
        raise HTTPException(
            status_code=422, detail="disposition must be accepted or dismissed"
        )

    async with factory() as session:
        row = await session.execute(
            text("""
                SELECT disposition FROM wiam_findings
                WHERE id = :fid AND session_id = :sid AND tenant_id = :tid
            """),
            {"fid": finding_id, "sid": session_id, "tid": tenant_id}
        )
        finding = row.mappings().first()
        if not finding:
            raise HTTPException(status_code=404, detail="Finding not found")
        if finding["disposition"] is not None:
            raise HTTPException(status_code=409, detail="Finding already disposed")

        await session.execute(
            text("""
                UPDATE wiam_findings
                SET disposition = :disp,
                    disposed_by = :uid,
                    disposed_at = NOW()
                WHERE id = :fid
            """),
            {"disp": disposition, "uid": user_id, "fid": finding_id}
        )
        await session.commit()

    return JSONResponse({"finding_id": finding_id, "disposition": disposition})
