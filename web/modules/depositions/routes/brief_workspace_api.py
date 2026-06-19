"""modules/depositions/routes/brief_workspace_api.py
JSON API for the React appellate brief workspace (Module B / U6).

Async AsyncSessionLocal + sa_text, tenant from request.state (mirrors depo_api).
The TRAP 38.1 section model lives in brief_sections; live word count, TOC, and the
U4 record-cite resolver + U5 auto-TOA / compliance engines are surfaced here (the
sync engines run in a threadpool so the event loop never blocks on a docx render).
"""
import logging
from typing import Optional

import os

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/appellate", tags=["appellate-api"])

# TRAP 38.1 section catalog: (key, title, word_excluded by TRAP 9.4(i)(1)).
# word_excluded sections do not count toward the 9.4(i)(2) word limit.
SECTION_CATALOG = [
    ("identity_of_parties", "Identity of Parties and Counsel", True),
    ("table_of_contents", "Table of Contents", True),
    ("index_of_authorities", "Index of Authorities", True),
    ("statement_of_the_case", "Statement of the Case", True),
    ("statement_on_oral_argument", "Statement Regarding Oral Argument", True),
    ("issues_presented", "Issues Presented", True),
    ("statement_of_facts", "Statement of Facts", False),
    ("summary_of_the_argument", "Summary of the Argument", False),
    ("argument", "Argument", False),
    ("prayer", "Prayer", False),
    ("certifications", "Certifications", True),
    ("appendix", "Appendix", True),
]


def _tenant(r: Request) -> str:
    return (getattr(r.state, "tenant_id", "") or "").strip()


def _wc(text: str) -> int:
    import re
    return len(re.findall(r"\S+", text or ""))


async def _ensure_sections(session, tenant, cid):
    """Seed the 38.1 skeleton for a case if absent (idempotent)."""
    for i, (key, title, excl) in enumerate(SECTION_CATALOG):
        await session.execute(sa_text(
            "INSERT INTO brief_sections (tenant_id, appellate_case_id, section_key, title, "
            "  sort_order, word_excluded) VALUES (:t, CAST(:c AS uuid), :k, :ti, :o, :e) "
            "ON CONFLICT (appellate_case_id, section_key) DO NOTHING"),
            {"t": tenant, "c": cid, "k": key, "ti": title, "o": (i + 1) * 10, "e": excl})
    await session.commit()


# --- list appeals for a matter ----------------------------------------------

@router.get("/cases")
async def list_cases(request: Request, matter_id: str = Query("")):
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT id::text, style, appellate_cause_number, court_of_appeals, "
            "       trial_cause_number, brief_deadline, status FROM appellate_cases "
            "WHERE TRIM(tenant_id)=TRIM(:t) AND (:m='' OR matter_id=CAST(:m AS uuid)) "
            "ORDER BY created_at DESC"), {"t": tenant, "m": matter_id})
        rows = [dict(x) for x in r.mappings().all()]
    return JSONResponse({"cases": rows})


# --- workspace (case + ordered sections + live totals + TOC) ----------------

@router.get("/cases/{cid}/workspace")
async def workspace(request: Request, cid: str):
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT id::text, matter_id::text, style, appellate_cause_number, "
            "       court_of_appeals, trial_court, trial_cause_number, appellant, appellee, "
            "       jurisdiction, brief_deadline, status FROM appellate_cases "
            "WHERE id=CAST(:c AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
            {"c": cid, "t": tenant})
        case = r.mappings().fetchone()
        if not case:
            return JSONResponse({"error": "case not found"}, status_code=404)
        case = dict(case)
        await _ensure_sections(session, tenant, cid)
        r = await session.execute(sa_text(
            "SELECT id::text, section_key, title, sort_order, body, included, "
            "       word_excluded, word_count FROM brief_sections "
            "WHERE appellate_case_id=CAST(:c AS uuid) ORDER BY sort_order"), {"c": cid})
        sections = [dict(x) for x in r.mappings().all()]
        # word limit from the rules (data)
        r = await session.execute(sa_text(
            "SELECT rule_value FROM appellate_rules WHERE jurisdiction=:j "
            "  AND doc_type='brief_appellant' AND rule_key='word_limit'"),
            {"j": case.get("jurisdiction") or "TX-TRAP"})
        lim_row = r.fetchone()
        limit = int(str(lim_row[0]).strip('"')) if lim_row else 15000

    counted = sum(s["word_count"] for s in sections if s["included"] and not s["word_excluded"])
    total = sum(s["word_count"] for s in sections if s["included"])
    toc = [{"title": s["title"], "section_key": s["section_key"]}
           for s in sections if s["included"]]
    return JSONResponse({
        "case": {k: (str(v) if v is not None and k == "brief_deadline" else v)
                 for k, v in case.items()},
        "sections": sections,
        "totals": {"counted_words": counted, "total_words": total, "word_limit": limit,
                   "over": max(0, counted - limit)},
        "toc": toc,
    })


# --- section CRUD -----------------------------------------------------------

class SectionPatch(BaseModel):
    body: Optional[str] = None
    title: Optional[str] = None
    included: Optional[bool] = None


@router.patch("/sections/{sid}")
async def patch_section(request: Request, sid: str, patch: SectionPatch):
    tenant = _tenant(request)
    sets, params = [], {"s": sid, "t": tenant}
    if patch.body is not None:
        sets.append("body=:b"); params["b"] = patch.body
        sets.append("word_count=:wc"); params["wc"] = _wc(patch.body)
    if patch.title is not None:
        sets.append("title=:ti"); params["ti"] = patch.title
    if patch.included is not None:
        sets.append("included=:inc"); params["inc"] = patch.included
    if not sets:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    sets.append("updated_at=now()")
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text(
            "UPDATE brief_sections SET " + ", ".join(sets) +
            " WHERE id=CAST(:s AS uuid) AND TRIM(tenant_id)=TRIM(:t)"), params)
        await session.commit()
        r = await session.execute(sa_text(
            "SELECT id::text, section_key, title, body, included, word_excluded, word_count "
            "FROM brief_sections WHERE id=CAST(:s AS uuid)"), {"s": sid})
        row = r.mappings().fetchone()
    return JSONResponse({"section": dict(row) if row else None})


class NewSection(BaseModel):
    section_key: str
    title: str
    sort_order: Optional[int] = None
    word_excluded: bool = False


@router.post("/cases/{cid}/sections")
async def add_section(request: Request, cid: str, s: NewSection):
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        order = s.sort_order
        if order is None:
            r = await session.execute(sa_text(
                "SELECT COALESCE(max(sort_order),0)+10 FROM brief_sections "
                "WHERE appellate_case_id=CAST(:c AS uuid)"), {"c": cid})
            order = r.scalar() or 10
        await session.execute(sa_text(
            "INSERT INTO brief_sections (tenant_id, appellate_case_id, section_key, title, "
            "  sort_order, word_excluded) VALUES (:t, CAST(:c AS uuid), :k, :ti, :o, :e) "
            "ON CONFLICT (appellate_case_id, section_key) DO UPDATE SET title=EXCLUDED.title"),
            {"t": tenant, "c": cid, "k": s.section_key, "ti": s.title, "o": order,
             "e": s.word_excluded})
        await session.commit()
    return JSONResponse({"ok": True})


@router.delete("/sections/{sid}")
async def delete_section(request: Request, sid: str):
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text(
            "DELETE FROM brief_sections WHERE id=CAST(:s AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
            {"s": sid, "t": tenant})
        await session.commit()
    return JSONResponse({"ok": True})


# --- U4/U5 engine proxies (sync -> threadpool) ------------------------------

@router.get("/cases/{cid}/compliance")
async def compliance(request: Request, cid: str, doctype: str = Query("brief_appellant")):
    from modules.depositions.jobs.appellate_brief import check_compliance
    out = await run_in_threadpool(check_compliance, _tenant(request), cid, doctype)
    return JSONResponse(out)


@router.post("/cases/{cid}/toa")
async def toa(request: Request, cid: str):
    from modules.depositions.jobs.appellate_brief import build_toa
    out = await run_in_threadpool(build_toa, _tenant(request), cid, None)
    return JSONResponse(out)


@router.post("/cases/{cid}/verify-cites")
async def verify_cites(request: Request, cid: str):
    from modules.depositions.jobs.appellate_verify import verify_brief
    out = await run_in_threadpool(verify_brief, _tenant(request), cid, None)
    return JSONResponse(out)


@router.post("/cases/{cid}/preservation")
async def preservation(request: Request, cid: str, source: str = Query("section")):
    """U8: trace each Issue Presented to its RR objection/ruling, grade TRAP 33.1."""
    from modules.depositions.jobs.appellate_preservation import check_preservation
    out = await run_in_threadpool(check_preservation, _tenant(request), cid, source)
    return JSONResponse(out)


@router.get("/cases/{cid}/preservation")
async def preservation_list(request: Request, cid: str):
    from modules.depositions.jobs.appellate_preservation import list_preservation
    out = await run_in_threadpool(list_preservation, _tenant(request), cid)
    return JSONResponse({"preservation": out})


# --- U9 Record Fact-Element Classifier (appellate intelligence layer) --------

@router.post("/cases/{cid}/framelock")
async def framelock(request: Request, cid: str):
    """U9.1: seed causes_of_action + coa_elements from CR pleadings + judgment."""
    from modules.depositions.jobs.appellate_framelock import frame_lock
    out = await run_in_threadpool(frame_lock, _tenant(request), cid, False)
    return JSONResponse(out)


@router.post("/cases/{cid}/classify")
async def classify_record(request: Request, cid: str, assign_at: float = Query(0.42)):
    """U9.2: classify the closed record's fact units against the element spine."""
    from modules.depositions.jobs.appellate_classify import classify
    out = await run_in_threadpool(classify, _tenant(request), cid, assign_at, False)
    return JSONResponse(out)


@router.get("/cases/{cid}/sufficiency")
async def sufficiency(request: Request, cid: str, rollup: bool = Query(False)):
    """U9.4: element-coverage / legal-sufficiency matrix over the frozen frame."""
    from modules.depositions.jobs.appellate_matrix import build_matrix
    out = await run_in_threadpool(build_matrix, _tenant(request), cid, rollup)
    return JSONResponse(out)


class AssembleReq(BaseModel):
    promote: bool = False
    subfolder: str = "03-Briefs and Motions"


@router.post("/cases/{cid}/assemble")
async def assemble(request: Request, cid: str, req: AssembleReq):
    from modules.depositions.jobs.appellate_assemble import assemble_brief
    out = await run_in_threadpool(assemble_brief, _tenant(request), cid, req.promote, req.subfolder)
    return JSONResponse(out)


class CiteReq(BaseModel):
    cite: str


@router.post("/cases/{cid}/resolve")
async def resolve(request: Request, cid: str, req: CiteReq):
    from modules.depositions.jobs.appellate_pipeline import resolve_cite
    out = await run_in_threadpool(resolve_cite, _tenant(request), cid, req.cite)
    return JSONResponse(out)


@router.get("/cases/{cid}/record")
async def record_docs(request: Request, cid: str):
    """The record register (CR/RR/supplements) + appendix/briefs, for the workspace."""
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT id::text, record_kind, is_record, label, page_first, page_last, page_count, "
            "       geometry_status, status, rr_transcript_id::text FROM record_documents "
            "WHERE appellate_case_id=CAST(:c AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
            "ORDER BY is_record DESC, record_kind, label"), {"c": cid, "t": tenant})
        rows = [dict(x) for x in r.mappings().all()]
    return JSONResponse({"record": rows})


# --- Module C: appellate matter surface (matter-keyed, for AppellateHomepage) ---
# These route on matters.id (the dashboard knows the matter, not the brief case).

# record_kind -> which side of the RR/CR switch it belongs on.
_RR_KINDS = ("RR", "SUPP_RR")
_CR_KINDS = ("CR", "SUPP_CR")


@router.get("/matters/{matter_id}/record")
async def matter_record(request: Request, matter_id: str):
    """Record Viewer data contract for the appellate dashboard: resolves the matter
    to its appellate case, returns matter/case meta, the trial-matter projection
    link (folder 06), and the closed record set split into Reporter's Record vs
    Clerk's Record for the top-level switch. Only is_record=true items are citable."""
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT m.matter_number, m.matter_name, m.matter_type, m.cause_number, m.court, "
            "       ac.id::text AS appellate_case_id, ac.style, ac.appellant, ac.appellee, "
            "       ac.court_of_appeals, ac.appellate_cause_number, ac.trial_court, "
            "       ac.trial_cause_number, ac.brief_deadline, ac.status "
            "FROM matters m LEFT JOIN appellate_cases ac "
            "  ON ac.matter_id=m.id AND TRIM(ac.tenant_id)=TRIM(m.tenant_id) "
            "WHERE m.id=CAST(:m AS uuid) AND TRIM(m.tenant_id)=TRIM(:t)"),
            {"m": matter_id, "t": tenant})
        meta = r.mappings().fetchone()
        if not meta:
            return JSONResponse({"error": "matter not found"}, status_code=404)
        meta = dict(meta)
        cid = meta.get("appellate_case_id")

        # trial-matter projection (folder 06): the appeal_of link, no copy.
        r = await session.execute(sa_text(
            "SELECT ml.to_matter_id::text AS trial_matter_id, tm.matter_number, tm.matter_name "
            "FROM matter_links ml JOIN matters tm ON tm.id=ml.to_matter_id "
            "WHERE ml.from_matter_id=CAST(:m AS uuid) AND ml.relation='appeal_of' "
            "  AND TRIM(ml.tenant_id)=TRIM(:t) LIMIT 1"), {"m": matter_id, "t": tenant})
        tl = r.mappings().fetchone()
        trial_link = dict(tl) if tl else None

        rr, cr = [], []
        if cid:
            r = await session.execute(sa_text(
                "SELECT rd.id::text, rd.record_kind, rd.label, rd.volume, rd.page_first, rd.page_last, "
                "       rd.page_count, rd.geometry_status, rd.status, rd.rr_transcript_id::text, "
                "       dt.trial_day, dt.title AS transcript_title, dt.has_video "
                "FROM record_documents rd "
                "LEFT JOIN deposition_transcripts dt "
                "  ON dt.id=rd.rr_transcript_id AND TRIM(dt.tenant_id)=TRIM(rd.tenant_id) "
                "WHERE rd.appellate_case_id=CAST(:c AS uuid) AND TRIM(rd.tenant_id)=TRIM(:t) "
                "  AND rd.is_record=true "
                "ORDER BY rd.record_kind, dt.trial_day NULLS FIRST, rd.volume NULLS FIRST, rd.label"),
                {"c": cid, "t": tenant})
            for x in r.mappings().all():
                d = dict(x)
                d["has_pageline"] = bool(d.get("rr_transcript_id"))
                d["view_url"] = "/api/v1/appellate/matters/%s/record-file/%s" % (matter_id, d["id"])
                (rr if d["record_kind"] in _RR_KINDS else cr).append(d)

    return JSONResponse({
        "matter": {k: (str(v) if k == "brief_deadline" and v is not None else v)
                   for k, v in meta.items()},
        "trial_link": trial_link,
        "reporters_record": rr,
        "clerks_record": cr,
        "record_complete": bool(rr) and bool(cr),
    })


@router.get("/matters/{matter_id}/dashboard")
async def matter_dashboard(request: Request, matter_id: str):
    """Composed payload for the appellate matter homepage 6-card grid:
    Alerts, Matter Summary, Briefing Deadline, Court & Counsel, Briefs, Open Tasks.
    Every card is backed by a real primitive; empty cards render as empty-state."""
    tenant = _tenant(request)

    def _s(v):
        return str(v) if v is not None else None

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT m.matter_number, m.matter_name, m.matter_type, m.cause_number, m.court, "
            "       ac.id::text AS appellate_case_id, ac.style, ac.appellant, ac.appellee, "
            "       ac.court_of_appeals, ac.appellate_cause_number, ac.trial_court, "
            "       ac.trial_cause_number, ac.brief_deadline, ac.status "
            "FROM matters m LEFT JOIN appellate_cases ac "
            "  ON ac.matter_id=m.id AND TRIM(ac.tenant_id)=TRIM(m.tenant_id) "
            "WHERE m.id=CAST(:m AS uuid) AND TRIM(m.tenant_id)=TRIM(:t)"),
            {"m": matter_id, "t": tenant})
        meta = r.mappings().fetchone()
        if not meta:
            return JSONResponse({"error": "matter not found"}, status_code=404)
        meta = dict(meta)
        cid = meta.get("appellate_case_id")

        r = await session.execute(sa_text(
            "SELECT ml.to_matter_id::text AS trial_matter_id, tm.matter_number, tm.matter_name "
            "FROM matter_links ml JOIN matters tm ON tm.id=ml.to_matter_id "
            "WHERE ml.from_matter_id=CAST(:m AS uuid) AND ml.relation='appeal_of' "
            "  AND TRIM(ml.tenant_id)=TRIM(:t) LIMIT 1"), {"m": matter_id, "t": tenant})
        tl = r.mappings().fetchone()
        trial_link = dict(tl) if tl else None

        r = await session.execute(sa_text(
            "SELECT alert_type, count, message, COALESCE(status,'open') AS status "
            "FROM onboarding_alerts WHERE matter_id=CAST(:m AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
            "  AND COALESCE(status,'open') NOT IN ('dismissed','resolved') "
            "ORDER BY created_at DESC LIMIT 12"), {"m": matter_id, "t": tenant})
        alerts = [dict(x) for x in r.mappings().all()]

        r = await session.execute(sa_text(
            "SELECT overview_paragraph, critical_issues_paragraph, generated_at, status "
            "FROM matter_summaries WHERE matter_id=CAST(:m AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
            "ORDER BY generated_at DESC NULLS LAST, created_at DESC LIMIT 1"),
            {"m": matter_id, "t": tenant})
        srow = r.mappings().fetchone()
        summary = None
        if srow:
            summary = dict(srow)
            summary["generated_at"] = _s(summary.get("generated_at"))

        r = await session.execute(sa_text(
            "SELECT title, deadline_date FROM deadlines "
            "WHERE matter_id=CAST(:m AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
            "  AND COALESCE(superseded,false)=false AND deadline_date IS NOT NULL "
            "ORDER BY deadline_date ASC LIMIT 6"), {"m": matter_id, "t": tenant})
        deadlines = [{"title": d["title"], "deadline_date": _s(d["deadline_date"])}
                     for d in r.mappings().all()]

        r = await session.execute(sa_text(
            "SELECT c.full_name AS name, COALESCE(c.firm_name, c.company) AS org, "
            "       c.bar_number, mc.role, mc.is_client_side "
            "FROM matter_contacts mc JOIN contacts c ON c.id=mc.contact_id "
            "WHERE mc.matter_id=CAST(:m AS uuid) AND TRIM(mc.tenant_id)=TRIM(:t) "
            "ORDER BY mc.is_primary DESC NULLS LAST, mc.role LIMIT 20"), {"m": matter_id, "t": tenant})
        contacts = [dict(x) for x in r.mappings().all()]

        briefs = {"appellate_case_id": cid, "total_sections": 0, "drafted_sections": 0, "word_count": 0}
        if cid:
            r = await session.execute(sa_text(
                "SELECT COUNT(*) AS total, "
                "  COUNT(*) FILTER (WHERE drafting_output_id IS NOT NULL OR COALESCE(word_count,0)>0) AS drafted, "
                "  COALESCE(SUM(word_count),0) AS words "
                "FROM brief_sections WHERE appellate_case_id=CAST(:c AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
                {"c": cid, "t": tenant})
            b = r.mappings().fetchone()
            if b:
                briefs = {"appellate_case_id": cid, "total_sections": int(b["total"] or 0),
                          "drafted_sections": int(b["drafted"] or 0), "word_count": int(b["words"] or 0)}

        r = await session.execute(sa_text(
            "SELECT title, due_date, status, priority FROM tasks "
            "WHERE matter_id=CAST(:m AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
            "  AND status NOT IN ('done','completed','cancelled') "
            "ORDER BY due_date ASC NULLS LAST LIMIT 10"), {"m": matter_id, "t": tenant})
        tasks = [{"title": t["title"], "due_date": _s(t["due_date"]), "status": t["status"]}
                 for t in r.mappings().all()]

        # Primary Reporter's Record transcript — drives the homepage "Record" nav tab
        # deep-link into the page:line transcript viewer (/depositions/transcript/{id}).
        record = {"rr_transcript_id": None, "rec_id": None}
        if cid:
            r = await session.execute(sa_text(
                "SELECT id::text AS rec_id, rr_transcript_id::text AS rr_transcript_id "
                "FROM record_documents "
                "WHERE appellate_case_id=CAST(:c AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
                "  AND record_kind='RR' AND rr_transcript_id IS NOT NULL "
                "ORDER BY volume ASC NULLS LAST LIMIT 1"), {"c": cid, "t": tenant})
            rr = r.mappings().fetchone()
            if rr:
                record = {"rr_transcript_id": rr["rr_transcript_id"], "rec_id": rr["rec_id"]}

    return JSONResponse({
        "matter": {k: (_s(v) if k == "brief_deadline" else v) for k, v in meta.items()},
        "appellate_case_id": cid,
        "record": record,
        "trial_link": trial_link,
        "alerts": alerts,
        "summary": summary,
        "briefing": {"brief_deadline": _s(meta.get("brief_deadline")), "deadlines": deadlines},
        "court_counsel": {"court_of_appeals": meta.get("court_of_appeals") or meta.get("court"),
                          "trial_court": meta.get("trial_court"), "contacts": contacts},
        "briefs": briefs,
        "tasks": tasks,
    })


@router.get("/cases/{cid}/exhibits")
async def case_exhibits(request: Request, cid: str):
    """Exhibits physically bound in the record (with RR page ranges) plus every place
    each is used in the testimony (page:line loci), for the exhibit viewer."""
    tenant = _tenant(request)
    SUB = ("SELECT rr_transcript_id FROM record_documents "
           "WHERE appellate_case_id=CAST(:c AS uuid) AND record_kind IN ('RR','SUPP_RR') "
           "  AND rr_transcript_id IS NOT NULL")
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT te.id::text AS id, te.party, te.exhibit_number, te.exhibit_label, te.admitted, "
            "       te.rr_record_id::text AS rr_record_id, te.rr_page_first, te.rr_page_last, te.rr_page_label "
            "FROM trial_exhibits te "
            "WHERE te.transcript_id IN (" + SUB + ") "
            "  AND te.rr_page_first IS NOT NULL AND TRIM(te.tenant_id)=TRIM(:t) "
            "ORDER BY te.rr_page_first"), {"c": cid, "t": tenant})
        exs = [dict(x) for x in r.mappings().all()]
        r = await session.execute(sa_text(
            "SELECT exhibit_id::text AS eid, page, line, locus, usage_kind, snippet "
            "FROM trial_exhibit_usages "
            "WHERE transcript_id IN (" + SUB + ") ORDER BY page, line"), {"c": cid})
        usages = {}
        for u in r.mappings().all():
            usages.setdefault(u["eid"], []).append(
                {"locus": u["locus"], "page": u["page"], "line": u["line"],
                 "kind": u["usage_kind"], "snippet": u["snippet"]})
        for e in exs:
            e["usages"] = usages.get(e["id"], [])
    return JSONResponse({"exhibits": exs})


@router.get("/cases/{cid}/cr-filings")
async def case_cr_filings(request: Request, cid: str):
    """Clerk's Record filing index for the brief-workspace Record viewer.

    cr_parser segments each CR volume into its constituent filings (cr_filing sections).
    This returns them grouped by CR volume, each filing carrying its CR page range and a
    deep link into that volume's PDF -- the navigable counterpart to the Exhibits tab."""
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT ds.section_label AS title, ds.attributes->>'cr_type' AS cr_type, "
            "       ds.page_start, ds.page_end, ds.section_index, "
            "       ds.attributes->>'record_document_id' AS rec_id, "
            "       rd.record_kind, rd.label AS volume_label, rd.volume, rd.page_count, "
            "       (SELECT count(*) FROM document_sections c WHERE c.parent_section_id=ds.id) AS fact_units "
            "FROM document_sections ds "
            "LEFT JOIN record_documents rd ON rd.id::text = ds.attributes->>'record_document_id' "
            "  AND TRIM(rd.tenant_id)=TRIM(ds.tenant_id) "
            "WHERE ds.section_type='cr_filing' "
            "  AND ds.attributes->>'appellate_case_id'=:c AND TRIM(ds.tenant_id)=TRIM(:t) "
            "ORDER BY rd.record_kind, rd.volume NULLS FIRST, ds.page_start NULLS LAST, ds.section_index"),
            {"c": cid, "t": tenant})
        rows = r.mappings().all()

    vols, order = {}, []
    for x in rows:
        rec_id = x["rec_id"]
        if rec_id not in vols:
            vols[rec_id] = {
                "record_document_id": rec_id,
                "record_kind": x["record_kind"],
                "label": x["volume_label"],
                "volume": x["volume"],
                "page_count": x["page_count"],
                "view_url": ("/api/v1/appellate/cases/%s/record-file/%s" % (cid, rec_id)) if rec_id else None,
                "filings": [],
            }
            order.append(rec_id)
        vols[rec_id]["filings"].append({
            "title": x["title"],
            "type": x["cr_type"],
            "page_first": x["page_start"],
            "page_last": x["page_end"],
            "fact_units": int(x["fact_units"] or 0),
        })
    return JSONResponse({"volumes": [vols[k] for k in order]})


@router.get("/cases/{cid}/record-file/{rec_id}")
async def case_record_file(request: Request, cid: str, rec_id: str):
    """Stream a record PDF for inline viewing in the brief workspace (cid-keyed).
    Path is read from record_documents.storage_path, scoped to the case + tenant."""
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT rd.storage_path, rd.label FROM record_documents rd "
            "WHERE rd.id=CAST(:r AS uuid) AND rd.appellate_case_id=CAST(:c AS uuid) "
            "  AND TRIM(rd.tenant_id)=TRIM(:t)"),
            {"r": rec_id, "c": cid, "t": tenant})
        row = r.mappings().fetchone()
    if not row:
        return JSONResponse({"error": "record document not found"}, status_code=404)
    path = row["storage_path"]
    if not path or not os.path.isfile(path):
        return JSONResponse({"error": "file missing on disk"}, status_code=404)
    return FileResponse(path, media_type="application/pdf", filename=row["label"], content_disposition_type="inline")


@router.get("/matters/{matter_id}/record-file/{rec_id}")
async def matter_record_file(request: Request, matter_id: str, rec_id: str):
    """Stream a record PDF for inline viewing. The on-disk path is taken from the
    DB (record_documents.storage_path), never from the request, and is validated
    against the matter's appellate case + tenant -- no path traversal surface."""
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT rd.storage_path, rd.label FROM record_documents rd "
            "JOIN appellate_cases ac ON ac.id=rd.appellate_case_id "
            "WHERE rd.id=CAST(:r AS uuid) AND ac.matter_id=CAST(:m AS uuid) "
            "  AND TRIM(rd.tenant_id)=TRIM(:t)"),
            {"r": rec_id, "m": matter_id, "t": tenant})
        row = r.mappings().fetchone()
    if not row:
        return JSONResponse({"error": "record document not found"}, status_code=404)
    path = row["storage_path"]
    if not path or not os.path.isfile(path):
        return JSONResponse({"error": "file missing on disk"}, status_code=404)
    return FileResponse(path, media_type="application/pdf", filename=row["label"], content_disposition_type="inline")


@router.post("/matters/{matter_id}/completeness")
async def run_completeness(request: Request, matter_id: str):
    """C-6: compare the parent trial file against the Clerk's Record (and brief-cited
    RR against the filed record); persist omission gaps with TRAP 34.5(c)/34.6 prompts."""
    from modules.depositions.jobs.appellate_completeness import check
    out = await run_in_threadpool(check, _tenant(request), matter_id, True)
    if out.get("error"):
        return JSONResponse(out, status_code=404)
    return JSONResponse(out)


@router.get("/matters/{matter_id}/completeness")
async def list_completeness(request: Request, matter_id: str, status: str = Query("open")):
    """C-6 gaps for the appellate dashboard, newest first, severity-ranked."""
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT id::text, gap_kind, filing_type, severity, title, detail, trial_refs, "
            "       best_cr_match, best_sim, brief_reliance, rule_cite, prompt, status, "
            "       detected_at FROM record_gaps "
            "WHERE matter_id=CAST(:m AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
            "  AND (:s='all' OR status=:s) "
            "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
            "         gap_kind, title"), {"m": matter_id, "t": tenant, "s": status})
        rows = [dict(x) for x in r.mappings().all()]
    for d in rows:
        d["detected_at"] = str(d["detected_at"]) if d.get("detected_at") else None
    sev = {"high": 0, "medium": 0, "low": 0}
    for d in rows:
        sev[d["severity"]] = sev.get(d["severity"], 0) + 1
    return JSONResponse({"gaps": rows, "counts": sev, "total": len(rows)})


# ============================================================================
# Module B / Unit 11 -- Working document surface for the OnlyOffice editor.
# The brief is edited as a single locked-section DOCX living in the matter DMS.
# These endpoints (a) ensure that DOCX exists and hand back the ids the editor
# needs, (b) list the matter's office docs for the working-doc picker, and
# (c) reattach the TRAP 38.1 TOC when an edited copy is brought back in.
# ============================================================================

_OFFICE_EXTS = (".docx", ".doc", ".odt", ".rtf")
_BRIEF_SUBFOLDER = "03-Briefs and Motions"


async def _case_matter(session, tenant, cid):
    r = await session.execute(sa_text(
        "SELECT ac.matter_id::text AS matter_id, ac.style, m.matter_name, c.client_name "
        "FROM appellate_cases ac JOIN matters m ON m.id=ac.matter_id "
        "  AND TRIM(m.tenant_id)=TRIM(ac.tenant_id) "
        "LEFT JOIN clients c ON c.id=m.client_id AND TRIM(c.tenant_id)=TRIM(m.tenant_id) "
        "WHERE ac.id=CAST(:c AS uuid) AND TRIM(ac.tenant_id)=TRIM(:t)"),
        {"c": cid, "t": tenant})
    return r.mappings().fetchone()


def _rel_path(storage_path, client, matter):
    """Matter-relative path in the convention oo-config expects (the segment
    after /matters/{client}/{matter}/). Mirrors dms_routes.document_meta."""
    if storage_path and client and matter:
        marker = "/matters/%s/%s/" % (client, matter)
        i = storage_path.find(marker)
        if i >= 0:
            return storage_path[i + len(marker):]
    return ""


def _ensure_brief_docx(tenant, cid):
    """Build the locked-section brief DOCX and file one copy into the matter DMS.
    Runs in a threadpool (psycopg2 + docx + disk IO)."""
    from modules.depositions.jobs.appellate_assemble import (
        build_brief_docx, _file_into_dms, _connect)
    built = build_brief_docx(tenant, cid)
    conn = _connect()
    try:
        cur = conn.cursor()
        ddoc, dpath = _file_into_dms(cur, tenant, built["matter_id"],
                                     built["docx_path"], _BRIEF_SUBFOLDER)
        conn.commit()
        return ddoc, dpath, built["filename"]
    finally:
        conn.close()


@router.get("/cases/{cid}/working-doc")
async def working_doc(request: Request, cid: str):
    """The default working document for the editor: the locked brief DOCX in DMS.
    Returns the ids the frontend needs to open it in OnlyOffice (edit mode)."""
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        meta = await _case_matter(session, tenant, cid)
        if not meta:
            return JSONResponse({"error": "case not found"}, status_code=404)
        meta = dict(meta)
        mid, client, matter = meta["matter_id"], meta["client_name"], meta["matter_name"]
        r = await session.execute(sa_text(
            "SELECT id::text, filename, storage_path FROM documents "
            "WHERE matter_id=CAST(:m AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
            "  AND document_type='brief' AND status='active' "
            "  AND lower(filename) LIKE '%.docx' "
            "ORDER BY updated_at DESC NULLS LAST, created_at DESC LIMIT 1"),
            {"m": mid, "t": tenant})
        row = r.mappings().fetchone()
    if row and row["storage_path"] and os.path.isfile(row["storage_path"]):
        return JSONResponse({"document_id": row["id"], "matter_id": mid,
                             "relative_path": _rel_path(row["storage_path"], client, matter),
                             "filename": row["filename"], "is_brief": True})
    ddoc, dpath, fname = await run_in_threadpool(_ensure_brief_docx, tenant, cid)
    return JSONResponse({"document_id": ddoc, "matter_id": mid,
                         "relative_path": _rel_path(dpath, client, matter),
                         "filename": fname, "is_brief": True})


@router.get("/cases/{cid}/office-docs")
async def office_docs(request: Request, cid: str):
    """Editable office documents in the matter, for the working-doc picker.
    The assembled brief sorts first; everything else is plain collaborative edit."""
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        meta = await _case_matter(session, tenant, cid)
        if not meta:
            return JSONResponse({"error": "case not found"}, status_code=404)
        meta = dict(meta)
        mid, client, matter = meta["matter_id"], meta["client_name"], meta["matter_name"]
        r = await session.execute(sa_text(
            "SELECT id::text, filename, storage_path, document_type, updated_at "
            "FROM documents WHERE matter_id=CAST(:m AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
            "  AND status='active' AND ("
            "    lower(filename) LIKE '%.docx' OR lower(filename) LIKE '%.doc' "
            "    OR lower(filename) LIKE '%.odt' OR lower(filename) LIKE '%.rtf') "
            "ORDER BY (document_type='brief') DESC, updated_at DESC NULLS LAST LIMIT 200"),
            {"m": mid, "t": tenant})
        rows = [dict(x) for x in r.mappings().all()]
    docs = []
    for d in rows:
        rel = _rel_path(d["storage_path"], client, matter)
        if not rel:
            continue
        docs.append({"document_id": d["id"], "filename": d["filename"],
                     "relative_path": rel,
                     "folder": rel.rsplit("/", 1)[0] if "/" in rel else "",
                     "matter_id": mid, "is_brief": d["document_type"] == "brief"})
    return JSONResponse({"matter_id": mid, "docs": docs})


class ReattachReq(BaseModel):
    document_id: Optional[str] = None


@router.post("/cases/{cid}/working-doc/reattach")
async def working_doc_reattach(request: Request, cid: str, req: ReattachReq):
    """Reattach the TRAP 38.1 TOC to an edited copy that was brought back in:
    recover the section bodies, sync brief_sections, and re-render a clean locked
    DOCX. Pass document_id to reattach a specific DMS doc; defaults to the brief."""
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        meta = await _case_matter(session, tenant, cid)
        if not meta:
            return JSONResponse({"error": "case not found"}, status_code=404)
        if req.document_id:
            r = await session.execute(sa_text(
                "SELECT storage_path FROM documents WHERE id=CAST(:d AS uuid) "
                "  AND TRIM(tenant_id)=TRIM(:t)"), {"d": req.document_id, "t": tenant})
        else:
            r = await session.execute(sa_text(
                "SELECT storage_path FROM documents "
                "WHERE matter_id=CAST(:m AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
                "  AND document_type='brief' AND status='active' AND lower(filename) LIKE '%.docx' "
                "ORDER BY updated_at DESC NULLS LAST, created_at DESC LIMIT 1"),
                {"m": dict(meta)["matter_id"], "t": tenant})
        row = r.mappings().fetchone()
    if not row or not row["storage_path"] or not os.path.isfile(row["storage_path"]):
        return JSONResponse({"error": "working document not found on disk"}, status_code=404)

    def _do(src):
        from modules.depositions.jobs.appellate_toc_bind import reattach_brief_toc
        import shutil as _sh
        out = reattach_brief_toc(tenant, cid, src)
        # overwrite the working doc in place with the rebound, re-locked DOCX
        _sh.copy2(out["docx_path"], src)
        try:
            from modules.dms.services.onlyoffice_route import bump_oo_gen as _bog2
            _bog2(src)
        except Exception:
            pass
        return out

    out = await run_in_threadpool(_do, row["storage_path"])
    return JSONResponse({"ok": True, "repaired": out["repaired"],
                         "matched": out["matched"], "unmatched": out["unmatched"],
                         "counted_words": out["counted_words"]})


# ============================================================================
# Module B / Unit 11 -- Record excerpts + brief version ledger.
# Excerpts are collected from the record viewer (RR transcript text / CR locus)
# and feed the left list + insert-at-cursor. Versions snapshot the working brief
# DOCX for timeline / diff / restore.
# ============================================================================

import hashlib as _hashlib
import shutil as _shutil
import os as _os


async def _brief_doc_row(session, tenant, cid):
    """The current working brief DOCX (documents row) for a case, or None."""
    meta = await _case_matter(session, tenant, cid)
    if not meta:
        return None, None
    r = await session.execute(sa_text(
        "SELECT id::text, filename, storage_path FROM documents "
        "WHERE matter_id=CAST(:m AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
        "  AND document_type='brief' AND status='active' AND lower(filename) LIKE '%.docx' "
        "ORDER BY updated_at DESC NULLS LAST, created_at DESC LIMIT 1"),
        {"m": dict(meta)["matter_id"], "t": tenant})
    return r.mappings().fetchone(), dict(meta)


# --- record excerpts --------------------------------------------------------

class ExcerptIn(BaseModel):
    record_document_id: Optional[str] = None
    record_kind: Optional[str] = None
    locus: Optional[str] = None
    page_first: Optional[int] = None
    page_last: Optional[int] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    text: Optional[str] = None
    color: Optional[str] = None
    note: Optional[str] = None


@router.get("/cases/{cid}/excerpts")
async def list_excerpts(request: Request, cid: str):
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT id::text, record_document_id::text AS record_document_id, record_kind, "
            "       locus, page_first, page_last, start_line, end_line, text, color, note, "
            "       created_by, created_at FROM record_excerpts "
            "WHERE appellate_case_id=CAST(:c AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
            "ORDER BY created_at DESC"), {"c": cid, "t": tenant})
        rows = [dict(x) for x in r.mappings().all()]
    for d in rows:
        d["created_at"] = str(d["created_at"]) if d.get("created_at") else None
    return JSONResponse({"excerpts": rows})


@router.post("/cases/{cid}/excerpts")
async def add_excerpt(request: Request, cid: str, body: ExcerptIn):
    tenant = _tenant(request)
    uid = getattr(getattr(request.state, "current_user", None), "id", None)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "INSERT INTO record_excerpts (tenant_id, appellate_case_id, record_document_id, "
            "  record_kind, locus, page_first, page_last, start_line, end_line, text, color, "
            "  note, created_by) VALUES (TRIM(:t), CAST(:c AS uuid), "
            "  CASE WHEN :rd='' OR :rd IS NULL THEN NULL ELSE CAST(:rd AS uuid) END, "
            "  :rk, :loc, :pf, :pl, :sl, :el, :txt, :col, :note, :uid) RETURNING id::text"),
            {"t": tenant, "c": cid, "rd": body.record_document_id, "rk": body.record_kind,
             "loc": body.locus, "pf": body.page_first, "pl": body.page_last,
             "sl": body.start_line, "el": body.end_line, "txt": body.text,
             "col": body.color, "note": body.note, "uid": uid})
        eid = r.scalar()
        await session.commit()
    return JSONResponse({"ok": True, "id": eid})


@router.delete("/excerpts/{eid}")
async def delete_excerpt(request: Request, eid: str):
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text(
            "DELETE FROM record_excerpts WHERE id=CAST(:e AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
            {"e": eid, "t": tenant})
        await session.commit()
    return JSONResponse({"ok": True})


# --- brief version ledger ---------------------------------------------------

def _snapshot_brief(storage_path, tenant, cid, document_id, label, kind, counted, uid, author, changes_path=None):
    """Copy the working DOCX into a per-case version store and record a row.
    Runs in a threadpool (disk + psycopg2)."""
    from modules.depositions.jobs.appellate_assemble import _connect
    vdir = _os.path.join(_os.path.dirname(storage_path), ".briefversions")
    _os.makedirs(vdir, exist_ok=True)
    content = open(storage_path, "rb").read()
    checksum = _hashlib.sha256(content).hexdigest()
    base = _os.path.basename(storage_path)
    # filename carries the checksum prefix so snapshots never collide
    snap = _os.path.join(vdir, checksum[:12] + "__" + base)
    if not _os.path.isfile(snap):
        _shutil.copy2(storage_path, snap)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO brief_doc_versions (tenant_id, appellate_case_id, document_id, "
            "  storage_path, author_user_id, author_name, label, kind, word_count, file_size, "
            "  checksum, changes_path) VALUES (%s, CAST(%s AS uuid), "
            "  CASE WHEN %s IS NULL THEN NULL ELSE CAST(%s AS uuid) END, "
            "  %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text",
            (tenant.strip(), str(cid), document_id, document_id, snap, uid, author,
             label, kind, counted, len(content), checksum, changes_path))
        vid = cur.fetchone()[0]
        conn.commit()
        return vid, snap, checksum
    finally:
        conn.close()


@router.get("/cases/{cid}/versions")
async def list_versions(request: Request, cid: str):
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT id::text, document_id::text AS document_id, label, kind, author_user_id, "
            "       author_name, word_count, file_size, checksum, created_at "
            "FROM brief_doc_versions WHERE appellate_case_id=CAST(:c AS uuid) "
            "  AND TRIM(tenant_id)=TRIM(:t) ORDER BY created_at DESC"), {"c": cid, "t": tenant})
        rows = [dict(x) for x in r.mappings().all()]
    for d in rows:
        d["created_at"] = str(d["created_at"]) if d.get("created_at") else None
    return JSONResponse({"versions": rows})


class SaveVersionReq(BaseModel):
    label: Optional[str] = None


@router.post("/cases/{cid}/versions")
async def save_version(request: Request, cid: str, req: SaveVersionReq):
    """Save Version: snapshot the current working brief DOCX with a label."""
    tenant = _tenant(request)
    uid = getattr(getattr(request.state, "current_user", None), "id", None)
    author = getattr(getattr(request.state, "current_user", None), "full_name", None)
    async with AsyncSessionLocal() as session:
        row, meta = await _brief_doc_row(session, tenant, cid)
        if not row or not row["storage_path"] or not _os.path.isfile(row["storage_path"]):
            return JSONResponse({"error": "working brief not found on disk"}, status_code=404)
        wr = await session.execute(sa_text(
            "SELECT COALESCE(SUM(word_count),0) FROM brief_sections "
            "WHERE appellate_case_id=CAST(:c AS uuid) AND included AND NOT word_excluded "
            "  AND TRIM(tenant_id)=TRIM(:t)"), {"c": cid, "t": tenant})
        counted = int(wr.scalar() or 0)
        doc_id = row["id"]; sp = row["storage_path"]
    label = (req.label or "").strip() or "Manual version"
    vid, snap, checksum = await run_in_threadpool(
        _snapshot_brief, sp, tenant, cid, doc_id, label, "version", counted, uid, author)
    return JSONResponse({"ok": True, "version_id": vid, "checksum": checksum, "label": label})


@router.post("/cases/{cid}/versions/{vid}/restore")
async def restore_version(request: Request, cid: str, vid: str):
    """Restore a prior version: snapshot the current base (safety), then copy the
    chosen version over the working brief DOCX. Whole-version restore."""
    tenant = _tenant(request)
    uid = getattr(getattr(request.state, "current_user", None), "id", None)
    author = getattr(getattr(request.state, "current_user", None), "full_name", None)
    async with AsyncSessionLocal() as session:
        vr = await session.execute(sa_text(
            "SELECT storage_path FROM brief_doc_versions WHERE id=CAST(:v AS uuid) "
            "  AND appellate_case_id=CAST(:c AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
            {"v": vid, "c": cid, "t": tenant})
        vrow = vr.mappings().fetchone()
        row, meta = await _brief_doc_row(session, tenant, cid)
    if not vrow or not vrow["storage_path"] or not _os.path.isfile(vrow["storage_path"]):
        return JSONResponse({"error": "version snapshot missing"}, status_code=404)
    if not row or not row["storage_path"]:
        return JSONResponse({"error": "working brief not found"}, status_code=404)
    snap_src = vrow["storage_path"]; base = row["storage_path"]; doc_id = row["id"]

    def _do():
        # safety snapshot of current base before clobbering
        if _os.path.isfile(base):
            _snapshot_brief(base, tenant, cid, doc_id, "Before restore", "pre-restore", None, uid, author)
        _shutil.copy2(snap_src, base)
        try:
            from modules.dms.services.onlyoffice_route import bump_oo_gen as _bog
            _bog(base)
        except Exception:
            pass
        content = open(base, "rb").read()
        from modules.depositions.jobs.appellate_assemble import _connect
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE documents SET checksum=%s, file_size=%s, updated_at=NOW() "
                        "WHERE id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s",
                        (_hashlib.sha256(content).hexdigest(), len(content), doc_id, tenant.strip()))
            conn.commit()
        finally:
            conn.close()
        # rebind the TOC/word counts from the restored doc
        from modules.depositions.jobs.appellate_toc_bind import reattach_brief_toc
        return reattach_brief_toc(tenant, cid, base)

    out = await run_in_threadpool(_do)
    return JSONResponse({"ok": True, "restored_from": vid, "repaired": out.get("repaired"),
                         "matched": out.get("matched"), "counted_words": out.get("counted_words")})


def _record_oo_version(tenant, cid, document_id, base_path, changes_path, actor_id, kind):
    """Write a brief_doc_versions row for a committed brief DOCX (called from the
    OnlyOffice save callback via a threadpool). Snapshots the DOCX; the changes
    zip is left for a later diff enhancement."""
    import os as _o
    if not base_path or not _o.path.isfile(base_path):
        return None
    from modules.depositions.jobs.appellate_assemble import _connect
    counted, author = None, None
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(SUM(word_count),0) FROM brief_sections "
                    "WHERE appellate_case_id=CAST(%s AS uuid) AND included AND NOT word_excluded "
                    "  AND TRIM(tenant_id)=%s", (str(cid), (tenant or "").strip()))
        counted = int(cur.fetchone()[0] or 0)
        if actor_id is not None and str(actor_id).isdigit():
            cur.execute("SELECT full_name FROM users WHERE id=%s AND TRIM(tenant_id)=%s",
                        (int(actor_id), (tenant or "").strip()))
            rr = cur.fetchone()
            author = rr[0] if rr else None
    finally:
        conn.close()
    uid = int(actor_id) if (actor_id is not None and str(actor_id).isdigit()) else None
    return _snapshot_brief(base_path, tenant, cid, document_id, None, kind, counted, uid, author, changes_path)


def _docx_text(path):
    """Full body text of a DOCX (paragraph order, including content-control bodies)."""
    from docx import Document
    from docx.oxml.ns import qn
    doc = Document(path)
    out = []
    for p in doc.element.body.iter(qn("w:p")):
        out.append("".join((n.text or "") for n in p.iter(qn("w:t"))))
    return "\n".join(out)


@router.get("/cases/{cid}/versions/{vid}/diff")
async def version_diff(request: Request, cid: str, vid: str, against: str = Query("current")):
    """Word-level visual diff between a saved version and another version (or the
    current draft). Returns HTML with inserted/deleted runs highlighted."""
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        vr = await session.execute(sa_text(
            "SELECT label, kind, storage_path FROM brief_doc_versions "
            "WHERE id=CAST(:v AS uuid) AND appellate_case_id=CAST(:c AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
            {"v": vid, "c": cid, "t": tenant})
        vrow = vr.mappings().fetchone()
        if against == "current":
            row, _meta = await _brief_doc_row(session, tenant, cid)
            other_path = row["storage_path"] if row else None
            other_label = "Current draft"
        else:
            ar = await session.execute(sa_text(
                "SELECT label, kind, storage_path FROM brief_doc_versions "
                "WHERE id=CAST(:v AS uuid) AND appellate_case_id=CAST(:c AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
                {"v": against, "c": cid, "t": tenant})
            arow = ar.mappings().fetchone()
            other_path = arow["storage_path"] if arow else None
            other_label = (arow["label"] or arow["kind"]) if arow else "?"
    if not vrow or not vrow["storage_path"]:
        return JSONResponse({"error": "version not found"}, status_code=404)
    if not other_path:
        return JSONResponse({"error": "comparison target not found"}, status_code=404)

    def _diff():
        import difflib, html, os as _o
        a = (_docx_text(vrow["storage_path"]) if _o.path.isfile(vrow["storage_path"]) else "").split()
        b = (_docx_text(other_path) if _o.path.isfile(other_path) else "").split()
        DEL = "<del style='background:#fee2e2;color:#991b1b'>"
        INS = "<ins style='background:#dcfce7;color:#166534;text-decoration:none'>"
        parts = []
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b).get_opcodes():
            aw = html.escape(" ".join(a[i1:i2])); bw = html.escape(" ".join(b[j1:j2]))
            if op == "equal":
                parts.append("<span>" + bw + "</span>")
            elif op == "delete":
                parts.append(DEL + aw + "</del>")
            elif op == "insert":
                parts.append(INS + bw + "</ins>")
            else:
                parts.append(DEL + aw + "</del> " + INS + bw + "</ins>")
        return " ".join(parts)

    out_html = await run_in_threadpool(_diff)
    return JSONResponse({"from_label": (vrow["label"] or vrow["kind"]),
                         "to_label": other_label, "html": out_html})


# --- §5 unified record search (testimony + CR/RR-page + exhibit) ----------
class RecordSearchBody(BaseModel):
    query: str
    limit: int = 20
    min_sim: float = 0.18
    scopes: list[str] | None = None   # entire_record|clerk_record|trial_transcript|trial_exhibits|research


def _embed_query_vec(q):
    """Embed a single query string -> pgvector literal (ModernBERT-768).
    Reuses the depo embed helper; runs inside the web container which can
    reach praesidium-embed."""
    from modules.depositions.routes.ai_api import _embed_many_sync
    return _embed_many_sync([q])[0]


@router.post("/cases/{cid}/record-search")
async def record_search(request: Request, cid: str, body: RecordSearchBody):
    """Semantic search across the whole appellate record for one case:
      (a) RR testimony via the Q&A index (transcript_qa_embeddings), and
      (b) CR + RR record-document chunks (dms_chunks source_type=record_document),
          with RR-page hits tagged as the exhibit whose page-span contains them.
    All sources are ranked together by cosine similarity. The record-document
    half lights up once the embed lane (§4) has run; until then only testimony
    is searched (note flagged in the response)."""
    tenant = _tenant(request)
    q = (body.query or "").strip()
    if not q:
        return JSONResponse({"results": [], "note": "empty query"})
    k = max(1, min(int(body.limit), 50))
    minsim = float(body.min_sim)
    _sc = set(body.scopes or ["entire_record"])
    want_all = ("entire_record" in _sc) or not _sc
    want_transcript = want_all or ("trial_transcript" in _sc)
    want_clerk = want_all or ("clerk_record" in _sc)
    want_exhibits = want_all or ("trial_exhibits" in _sc)
    want_research = "research" in _sc
    try:
        qv = await run_in_threadpool(_embed_query_vec, q)
    except Exception as e:                                  # embed service down
        logger.exception("record-search embed failed")
        return JSONResponse({"error": "embed unavailable: %s" % e}, status_code=503)

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT id::text AS rec_id, record_kind, label, document_id::text AS document_id, "
            "       geometry_doc_id::text AS geometry_doc_id, rr_transcript_id::text AS rr_transcript_id "
            "FROM record_documents "
            "WHERE appellate_case_id=CAST(:c AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
            "  AND record_kind IN ('RR','SUPP_RR','CR','SUPP_CR')"),
            {"c": cid, "t": tenant})
        recs = [dict(x) for x in r.mappings().all()]
        by_docid, doc_ids, rr_transcripts, rr_rec_ids = {}, [], [], []
        for d in recs:
            for key in (d["document_id"], d["geometry_doc_id"]):
                if key and key not in by_docid:
                    by_docid[key] = d
                    doc_ids.append(key)
            if d["rr_transcript_id"]:
                rr_transcripts.append(d["rr_transcript_id"])
            if d["record_kind"] in ("RR", "SUPP_RR"):
                rr_rec_ids.append(d["rec_id"])

        results = []

        # (a) RR testimony -- best chunk per Q&A unit over the case RR transcripts
        if rr_transcripts and want_transcript:
            qa = await session.execute(sa_text(
                "WITH ranked AS ("
                "  SELECT emb.qa_unit_id AS qa_id, "
                "         min(emb.embedding <=> CAST(:qv AS vector)) AS dist "
                "  FROM transcript_qa_embeddings emb "
                "  WHERE TRIM(emb.tenant_id)=TRIM(:t) "
                "    AND emb.transcript_id::text = ANY(:trs) "
                "  GROUP BY emb.qa_unit_id) "
                "SELECT q.id::text AS id, q.transcript_id::text AS transcript_id, "
                "       q.q_start_page, q.q_start_line, q.is_colloquy, "
                "       q.question_text, q.answer_text, (1 - r.dist) AS sim "
                "FROM ranked r JOIN transcript_qa_units q ON q.id=r.qa_id "
                "WHERE (1 - r.dist) >= :minsim ORDER BY r.dist LIMIT :k"),
                {"qv": qv, "t": tenant, "trs": rr_transcripts, "minsim": minsim, "k": k})
            for x in qa.mappings().all():
                snip = ((("Q: " + x["question_text"]) if x["question_text"] else "")
                        + (("  A: " + x["answer_text"]) if x["answer_text"] else "")).strip()
                results.append({
                    "id": "qa:" + x["id"], "source": "testimony", "kind": "RR",
                    "transcript_id": x["transcript_id"],
                    "page": x["q_start_page"], "line": x["q_start_line"],
                    "locus": "%d:%02d" % (x["q_start_page"], x["q_start_line"]),
                    "label": "Testimony — RR %d:%02d%s" % (
                        x["q_start_page"], x["q_start_line"],
                        " (colloquy)" if x["is_colloquy"] else ""),
                    "snippet": snip[:320], "sim": float(x["sim"])})

        # (b) record-document chunks -- CR + RR pages (post-§4)
        if doc_ids:
            rc = await session.execute(sa_text(
                "SELECT c.id::text AS id, c.source_id::text AS doc_id, c.page_number, "
                "       LEFT(c.content, 320) AS snippet, "
                "       (e.embedding_768 <=> CAST(:qv AS vector)) AS dist "
                "FROM dms_chunks c JOIN dms_chunk_embeddings e ON e.chunk_id=c.id "
                "WHERE TRIM(c.tenant_id)=TRIM(:t) AND c.source_type='record_document' "
                "  AND c.source_id::text = ANY(:docids) AND e.embedding_768 IS NOT NULL "
                "ORDER BY dist ASC LIMIT :k"),
                {"qv": qv, "t": tenant, "docids": doc_ids, "k": k})
            rec_hits = [dict(x) for x in rc.mappings().all()]

            ex_by_rec = {}
            if rec_hits and rr_rec_ids:
                ex = await session.execute(sa_text(
                    "SELECT rr_record_id::text AS rec_id, exhibit_number, party, exhibit_label, "
                    "       rr_page_first, rr_page_last FROM trial_exhibits "
                    "WHERE rr_record_id::text = ANY(:recids) AND rr_page_first IS NOT NULL"),
                    {"recids": rr_rec_ids})
                for x in ex.mappings().all():
                    ex_by_rec.setdefault(x["rec_id"], []).append(dict(x))

            for h in rec_hits:
                rd = by_docid.get(h["doc_id"])
                if not rd:
                    continue
                sim = 1 - float(h["dist"])
                if sim < minsim:
                    continue
                page, kind, rec_id = h["page_number"], rd["record_kind"], rd["rec_id"]
                if kind in ("CR", "SUPP_CR"):
                    if not want_clerk:
                        continue
                    pref = "Clerk's Record" if kind == "CR" else "Supp. Clerk's Record"
                    results.append({
                        "id": "rec:" + h["id"], "source": "record", "kind": kind,
                        "rec_id": rec_id, "page": page, "line": None,
                        "locus": ("p.%s" % page) if page else "",
                        "label": pref + ((" p.%s" % page) if page else ""),
                        "snippet": (h["snippet"] or "").strip(), "sim": sim})
                    continue
                # RR / SUPP_RR -- exhibit containment?
                hit_ex = None
                if page is not None:
                    for e in ex_by_rec.get(rec_id, []):
                        last = e["rr_page_last"] or e["rr_page_first"]
                        if e["rr_page_first"] <= page <= last:
                            hit_ex = e
                            break
                if hit_ex:
                    if not want_exhibits:
                        continue
                    lbl = (hit_ex["exhibit_label"]
                           or ((hit_ex["party"] or "") + " Ex. " + (hit_ex["exhibit_number"] or "")).strip())
                    results.append({
                        "id": "rec:" + h["id"], "source": "record", "kind": "EX",
                        "rec_id": rec_id, "page": page, "line": None,
                        "locus": "Ex. %s" % (hit_ex["exhibit_number"] or "?"),
                        "label": "Exhibit %s — %s (RR p.%s)" % (
                            hit_ex["exhibit_number"] or "?", lbl, page),
                        "snippet": (h["snippet"] or "").strip(), "sim": sim})
                else:
                    if not want_transcript:
                        continue
                    results.append({
                        "id": "rec:" + h["id"], "source": "record", "kind": "RR",
                        "rec_id": rec_id, "page": page, "line": None,
                        "locus": ("RR p.%s" % page) if page else "",
                        "label": ("Reporter's Record p.%s" % page) if page else "Reporter's Record",
                        "snippet": (h["snippet"] or "").strip(), "sim": sim})

    # research -- routed to the tenant's configured provider (research router)
    research_note, research_provider = None, None
    if want_research:
        try:
            from modules.drafting.research_router import search_research
            _rr = await search_research(tenant, q, qv, limit=k, minsim=minsim)
            results.extend(_rr.get("results", []))
            research_note = _rr.get("note"); research_provider = _rr.get("provider")
        except Exception:
            logger.exception("record-search research branch failed")
            research_note = "research unavailable"

    results.sort(key=lambda r: r["sim"], reverse=True)
    has_record_hits = any(r["source"] == "record" for r in results)
    has_research_hits = any(r["source"] == "research" for r in results)
    note = None
    if want_transcript and not has_record_hits and not has_research_hits:
        note = ("record-document chunks not embedded yet (§4 embed lane) -- "
                "searching RR testimony only")
    return JSONResponse({
        "results": results[:k],
        "sources": {"testimony": bool(rr_transcripts) and want_transcript,
                    "record": has_record_hits, "research": has_research_hits},
        "scopes": sorted(_sc),
        "research_provider": research_provider,
        "note": note or research_note})


# --- single CR filing as a small extracted PDF (page range of parent volume) --
def _extract_pdf_pages(path, pf, pl):
    """Bytes of a new PDF holding pages pf..pl (1-based, inclusive) of path,
    clamped to the file. fitz is fast even on a large source."""
    import fitz
    src = fitz.open(path)
    try:
        n = src.page_count
        a = max(1, int(pf or 1))
        b = int(pl or pf or a)
        b = min(n, b if b >= a else a)
        a = min(a, n)
        out = fitz.open()
        try:
            out.insert_pdf(src, from_page=a - 1, to_page=b - 1)
            return out.tobytes()
        finally:
            out.close()
    finally:
        src.close()


@router.get("/cases/{cid}/filing-file/{rec_id}")
async def case_filing_file(request: Request, cid: str, rec_id: str,
                           pf: int = Query(1), pl: int = Query(0)):
    """Stream one filing as a small extracted PDF (pages pf..pl of the parent
    record volume). Path from DB, scoped to case+tenant; range clamped to file.
    Non-PDF (docx) volumes are served whole."""
    from fastapi import Response
    tenant = _tenant(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT rd.storage_path, rd.label FROM record_documents rd "
            "WHERE rd.id=CAST(:r AS uuid) AND rd.appellate_case_id=CAST(:c AS uuid) "
            "  AND TRIM(rd.tenant_id)=TRIM(:t)"),
            {"r": rec_id, "c": cid, "t": tenant})
        row = r.mappings().fetchone()
    if not row:
        return JSONResponse({"error": "record document not found"}, status_code=404)
    path = row["storage_path"]
    if not path or not os.path.isfile(path):
        return JSONResponse({"error": "file missing on disk"}, status_code=404)
    if not path.lower().endswith(".pdf"):
        return FileResponse(path, media_type="application/pdf", filename=row["label"],
                            content_disposition_type="inline")
    try:
        data = await run_in_threadpool(_extract_pdf_pages, path, pf, pl)
    except Exception as e:
        logger.exception("filing extract failed")
        return JSONResponse({"error": str(e)}, status_code=500)
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": "inline; filename=filing.pdf"})


@router.post("/cases/{cid}/research-search")
async def research_search(request: Request, cid: str, body: RecordSearchBody):
    """Semantic search over the matter's general documents (research / work product)
    for the 'research' scope of the workspace search bar. Reuses the record-search
    embedding lane; searches dms_chunks(source_type='document') for the case's matter.
    Returns the same result shape as record-search (source='research')."""
    tenant = _tenant(request)
    q = (body.query or "").strip()
    if not q:
        return JSONResponse({"results": []})
    k = max(1, min(int(body.limit), 50))
    minsim = float(body.min_sim)
    async with AsyncSessionLocal() as session:
        meta = await _case_matter(session, tenant, cid)
        if not meta:
            return JSONResponse({"error": "case not found"}, status_code=404)
        mid = dict(meta)["matter_id"]
    try:
        qv = await run_in_threadpool(_embed_query_vec, q)
    except Exception as e:
        logger.exception("research-search embed failed")
        return JSONResponse({"error": "embed unavailable: %s" % e}, status_code=503)
    async with AsyncSessionLocal() as session:
        rc = await session.execute(sa_text(
            "SELECT c.id::text AS id, c.source_id::text AS doc_id, c.page_number, "
            "       LEFT(c.content, 320) AS snippet, d.filename, "
            "       (e.embedding_768 <=> CAST(:qv AS vector)) AS dist "
            "FROM dms_chunks c JOIN dms_chunk_embeddings e ON e.chunk_id=c.id "
            "JOIN documents d ON d.id=c.source_id AND TRIM(d.tenant_id)=TRIM(c.tenant_id) "
            "WHERE TRIM(c.tenant_id)=TRIM(:t) AND c.source_type='document' "
            "  AND d.matter_id=CAST(:m AS uuid) AND e.embedding_768 IS NOT NULL "
            "ORDER BY dist ASC LIMIT :k"),
            {"qv": qv, "t": tenant, "m": mid, "k": k})
        out = []
        for x in rc.mappings().all():
            sim = 1 - float(x["dist"])
            if sim < minsim:
                continue
            out.append({
                "id": "res:" + x["id"], "source": "research", "kind": "DOC",
                "doc_id": x["doc_id"], "page": x["page_number"],
                "locus": (x["filename"] or "Document") + ((" p.%s" % x["page_number"]) if x["page_number"] else ""),
                "label": x["filename"] or "Research document",
                "snippet": (x["snippet"] or "").strip(), "sim": sim})
    return JSONResponse({"results": out})
