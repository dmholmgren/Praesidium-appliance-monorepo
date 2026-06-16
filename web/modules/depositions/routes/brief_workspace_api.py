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
            "SELECT record_kind, is_record, label, page_first, page_last, page_count, "
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
                "SELECT id::text, record_kind, label, volume, page_first, page_last, "
                "       page_count, geometry_status, status, rr_transcript_id::text "
                "FROM record_documents "
                "WHERE appellate_case_id=CAST(:c AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
                "  AND is_record=true ORDER BY record_kind, volume NULLS FIRST, label"),
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
    return FileResponse(path, media_type="application/pdf", filename=row["label"])


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
