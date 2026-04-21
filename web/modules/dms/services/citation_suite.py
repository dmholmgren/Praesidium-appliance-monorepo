from sqlalchemy import text as sa_text
from modules.dms.brand_helper import get_brand
"""
COMP 12 — Citation Suite
- Citation extraction from documents
- Shepards/KeyCite signal inline via LegalResearchService
- Table of Authorities (TOA) auto-generation
- TOA format: cases alphabetical, statutes by title, rules by number
"""

import os
import re
import io
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dms/citations", tags=["dms-citations"])


class ExtractRequest(BaseModel):
    document_id: str


class TOARequest(BaseModel):
    document_id: str
    matter_id: str
    check_signals: bool = True  # Run Shepards/KeyCite on each citation


# ── Citation Patterns ──────────────────────────────────────

CASE_PATTERN = re.compile(
    r'(?P<volume>\d+)\s+'
    r'(?P<reporter>[A-Z][a-z]*\.?\s*(?:2d|3d|4th|5th|Supp\.?\s*(?:2d|3d)?|App\.?)?)\s+'
    r'(?P<page>\d+)'
    r'(?:\s*,\s*(?P<pinpoint>\d+))?'
    r'(?:\s*\((?P<court>[^)]+)\s+(?P<year>\d{4})\))?',
)

USC_PATTERN = re.compile(
    r'(?P<title>\d+)\s+U\.S\.C\.\s*§\s*(?P<section>\d+[\w.-]*)'
)

CFR_PATTERN = re.compile(
    r'(?P<title>\d+)\s+C\.F\.R\.\s*§\s*(?P<section>\d+[\w.-]*)'
)

STATE_STATUTE_PATTERN = re.compile(
    r'(?P<state>[A-Z][a-z]+\.?)\s+'
    r'(?P<code>[A-Z][a-z]*\.?\s*(?:Code|Stat|Ann)\.?)\s*'
    r'§\s*(?P<section>\d+[\w.-]*)'
)

RULE_PATTERN = re.compile(
    r'(?:Fed\.\s*R\.\s*(?P<type>Civ|Crim|App|Evid|Bankr)\.?\s*P\.\s*(?P<number>\d+(?:\.\d+)?))'
    r'|(?:(?:FRCP|FRCrP|FRAP|FRE)\s*(?:Rule\s*)?(?P<number2>\d+(?:\.\d+)?))'
)

CONSTITUTION_PATTERN = re.compile(
    r'U\.S\.\s*Const\.\s*(?:art|amend)\.\s*[IVXLCDM]+(?:\s*,\s*§\s*\d+)?'
)


@router.post("/extract")
async def extract_citations(body: ExtractRequest, request: Request):
    """Extract all citations from a document."""
    from core.db.base import TenantSession, get_session_factory

    tenant_id = request.state.tenant_id
    session = TenantSession(get_session_factory()(), tenant_id)

    doc = session.execute(
        sa_text("SELECT ocr_text, name FROM documents WHERE id = :id AND tenant_id = :tid"),
        {"id": body.document_id, "tid": tenant_id},
    ).fetchone()

    if not doc or not doc["ocr_text"]:
        raise HTTPException(status_code=404, detail="No text available")

    citations = _extract_all_citations(doc["ocr_text"])

    return {
        "document_id": body.document_id,
        "document_name": doc["title"],
        "total_citations": sum(len(v) for v in citations.values()),
        "cases": citations["cases"],
        "statutes": citations["statutes"],
        "rules": citations["rules"],
        "regulations": citations["regulations"],
        "constitutional": citations["constitutional"],
    }


@router.post("/toa")
async def generate_toa(body: TOARequest, request: Request):
    """
    Generate a Table of Authorities from a document.
    Cases alphabetical, statutes by title, rules by number.
    Optionally checks Shepards/KeyCite for each citation.
    """
    from core.audit import write_audit

    tenant_id = request.state.tenant_id
    user_id = getattr(request.state.current_user, "id", request.state.current_user) if request.state.current_user else "anonymous"
    session = TenantSession(get_session_factory()(), tenant_id)

    doc = session.execute(
        sa_text("SELECT ocr_text, name, matter_id FROM documents WHERE id = :id AND tenant_id = :tid"),
        {"id": body.document_id, "tid": tenant_id},
    ).fetchone()

    if not doc or not doc["ocr_text"]:
        raise HTTPException(status_code=404, detail="No text available")

    citations = _extract_all_citations(doc["ocr_text"])

    # Optionally check signals
    signals = {}
    if body.check_signals:
        from modules.dms.adapters.legal_research import LexisAdapter
        adapter = LexisAdapter()
        for case in citations["cases"][:30]:
            try:
                result = await adapter.cite_check(case["full_cite"])
                signals[case["full_cite"]] = result.signal.value
            except Exception:
                signals[case["full_cite"]] = "unknown"
        await adapter.close()

    # Find page references for each citation
    page_refs = _find_page_references(doc["ocr_text"], citations)

    # Build TOA document
    toa_content = _build_toa_docx(citations, signals, page_refs, doc["title"])

    # Save to DMS
    import hashlib
    toa_name = f"TOA_{doc['title']}"
    toa_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    import httpx
    cifs_url = os.environ["CIFS_URL"]
    matter = session.execute(
        sa_text("SELECT number, name FROM matters WHERE id = :id AND tenant_id = :tid"),
        {"id": body.matter_id, "tid": tenant_id},
    ).fetchone()
    storage_path = f"{matter['matter_number'] or matter['matter_name']}/Generated/{toa_name}"

    httpx.post(
        f"{cifs_url}/api/v1/files/upload",
        files={"file": (toa_name, toa_content,
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        data={"tenant_id": tenant_id, "path": storage_path},
        timeout=60,
    )

    session.execute(
        sa_text("""INSERT INTO documents
        (id, tenant_id, matter_id, name, storage_path, file_type,
         file_size, checksum, created_at, modified_at, is_deleted,
         uploaded_by, doc_category)
        VALUES (:id, :tid, :mid, :name, :path, 'docx',
                :size, :cs, :now, :now, 0, :by, 'toa')"""),
        {
            "id": toa_id, "tid": tenant_id, "mid": body.matter_id,
            "name": toa_name, "path": storage_path,
            "size": len(toa_content),
            "cs": hashlib.sha256(toa_content).hexdigest(),
            "now": now, "by": user_id,
        },
    )

    write_audit(
        tenant_id=tenant_id, user_id=user_id,
        action="generate_toa", module="dms", table_name="documents",
        record_id=toa_id,
        new_value={"source_doc": body.document_id, "citations_count": sum(len(v) for v in citations.values())},
        source="citation_suite",
    )
    session.commit()

    return {
        "toa_document_id": toa_id,
        "toa_name": toa_name,
        "citation_counts": {
            "cases": len(citations["cases"]),
            "statutes": len(citations["statutes"]),
            "rules": len(citations["rules"]),
            "regulations": len(citations["regulations"]),
            "constitutional": len(citations["constitutional"]),
        },
        "signals": signals,
    }


def _extract_all_citations(text: str) -> dict:
    """Extract all citation types from text."""
    result = {
        "cases": [],
        "statutes": [],
        "rules": [],
        "regulations": [],
        "constitutional": [],
    }

    seen = set()

    for m in CASE_PATTERN.finditer(text):
        cite = m.group().strip()
        if cite not in seen:
            seen.add(cite)
            result["cases"].append({
                "full_cite": cite,
                "volume": m.group("volume"),
                "reporter": m.group("reporter").strip(),
                "page": m.group("page"),
                "court": m.group("court") or "",
                "year": m.group("year") or "",
            })

    for m in USC_PATTERN.finditer(text):
        cite = m.group().strip()
        if cite not in seen:
            seen.add(cite)
            result["statutes"].append({
                "full_cite": cite,
                "title": m.group("title"),
                "section": m.group("section"),
                "code": "U.S.C.",
            })

    for m in CFR_PATTERN.finditer(text):
        cite = m.group().strip()
        if cite not in seen:
            seen.add(cite)
            result["regulations"].append({
                "full_cite": cite,
                "title": m.group("title"),
                "section": m.group("section"),
            })

    for m in RULE_PATTERN.finditer(text):
        cite = m.group().strip()
        if cite not in seen:
            seen.add(cite)
            result["rules"].append({
                "full_cite": cite,
                "type": m.group("type") or "",
                "number": m.group("number") or m.group("number2") or "",
            })

    for m in CONSTITUTION_PATTERN.finditer(text):
        cite = m.group().strip()
        if cite not in seen:
            seen.add(cite)
            result["constitutional"].append({"full_cite": cite})

    # Sort: cases alphabetical, statutes by title, rules by number
    result["cases"].sort(key=lambda x: x["full_cite"])
    result["statutes"].sort(key=lambda x: (int(x.get("title", "0")), x.get("section", "")))
    result["rules"].sort(key=lambda x: float(x.get("number", "0") or "0"))

    return result


def _find_page_references(text: str, citations: dict) -> dict:
    """Find which pages each citation appears on (for passim detection)."""
    pages = text.split("\f")  # Form feeds indicate page breaks
    refs = defaultdict(list)

    for page_num, page_text in enumerate(pages, 1):
        for category in citations.values():
            for cite in category:
                if cite["full_cite"] in page_text:
                    refs[cite["full_cite"]].append(page_num)

    return dict(refs)


def _build_toa_docx(citations: dict, signals: dict, page_refs: dict,
                     source_name: str) -> bytes:
    """Build a Table of Authorities .docx file."""
    from docx import Document

    doc = Document()
    doc.add_heading("TABLE OF AUTHORITIES", level=0)
    doc.add_paragraph(f"Source: {source_name}")
    doc.add_paragraph("")

    # Cases
    if citations["cases"]:
        doc.add_heading("Cases", level=1)
        for case in citations["cases"]:
            cite = case["full_cite"]
            signal = signals.get(cite, "")
            pages = page_refs.get(cite, [])
            page_str = "passim" if len(pages) > 4 else ", ".join(str(p) for p in pages)
            signal_str = f" [{signal}]" if signal and signal != "unknown" else ""
            doc.add_paragraph(f"{cite}{signal_str} {'.' * 40} {page_str}")

    # Statutes
    if citations["statutes"]:
        doc.add_heading("Statutes", level=1)
        for statute in citations["statutes"]:
            cite = statute["full_cite"]
            pages = page_refs.get(cite, [])
            page_str = "passim" if len(pages) > 4 else ", ".join(str(p) for p in pages)
            doc.add_paragraph(f"{cite} {'.' * 40} {page_str}")

    # Rules
    if citations["rules"]:
        doc.add_heading("Rules", level=1)
        for rule in citations["rules"]:
            cite = rule["full_cite"]
            pages = page_refs.get(cite, [])
            page_str = "passim" if len(pages) > 4 else ", ".join(str(p) for p in pages)
            doc.add_paragraph(f"{cite} {'.' * 40} {page_str}")

    # Regulations
    if citations["regulations"]:
        doc.add_heading("Regulations", level=1)
        for reg in citations["regulations"]:
            cite = reg["full_cite"]
            pages = page_refs.get(cite, [])
            page_str = "passim" if len(pages) > 4 else ", ".join(str(p) for p in pages)
            doc.add_paragraph(f"{cite} {'.' * 40} {page_str}")

    # Constitutional Provisions
    if citations["constitutional"]:
        doc.add_heading("Constitutional Provisions", level=1)
        for const in citations["constitutional"]:
            cite = const["full_cite"]
            pages = page_refs.get(cite, [])
            page_str = "passim" if len(pages) > 4 else ", ".join(str(p) for p in pages)
            doc.add_paragraph(f"{cite} {'.' * 40} {page_str}")

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
