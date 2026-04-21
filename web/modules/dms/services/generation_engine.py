from sqlalchemy import text as sa_text
from modules.dms.brand_helper import get_brand
"""
COMP 11 — Document Generation Engine + Sanity Check Engine (8 Layers)
- Template library with version control and approval workflow
- Exemplar library for firm voice learning
- PPM assembly, loan package assembly
- 8-layer sanity check pipeline
- All AI calls via AIService only — never direct SDK
"""

import os
import io
import uuid
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from enum import Enum

from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dms/generate", tags=["dms-generation"])


# ═══════════════════════════════════════════════════════════════
# DOCUMENT GENERATION ENGINE
# ═══════════════════════════════════════════════════════════════

class GenerateRequest(BaseModel):
    template_id: str
    matter_id: str
    variables: dict = {}
    exemplar_ids: list[str] = []
    assembly_type: str = "standard"  # standard | ppm | loan_package


class TemplateUploadRequest(BaseModel):
    name: str
    description: str = ""
    doc_type: str = ""
    category: str = ""


@router.post("/document")
async def generate_document(body: GenerateRequest, request: Request):
    """
    Generate a document from a template with matter context.
    AI extracts data from source materials, maps to variables, generates draft.
    All AI calls via AIService.
    """
    from core.db.base import TenantSession, get_session_factory
    from core.audit import write_audit
    from core.services.ai import AIService

    tenant_id = request.state.tenant_id
    user_id = getattr(request.state.current_user, "id", request.state.current_user) if request.state.current_user else "anonymous"
    session = TenantSession(get_session_factory()(), tenant_id)
    ai = AIService()

    # Load template
    template = session.execute(
        sa_text("SELECT * FROM templates WHERE id = :id AND tenant_id = :tid"),
        {"id": body.template_id, "tid": tenant_id},
    ).fetchone()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    # Load matter context
    matter = session.execute(
        sa_text("""SELECT m.*, c.client_name as client_name
           FROM matters m
           LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
           WHERE m.id = :mid AND m.tenant_id = :tid"""),
        {"mid": body.matter_id, "tid": tenant_id},
    ).fetchone()
    if not matter:
        raise HTTPException(status_code=404, detail="Matter not found")

    # Load source documents for context
    source_docs = session.execute(
        sa_text("""SELECT name, ocr_text FROM documents
           WHERE matter_id = :mid AND tenant_id = :tid
            AND ocr_text IS NOT NULL
           ORDER BY modified_at DESC LIMIT 20"""),
        {"mid": body.matter_id, "tid": tenant_id},
    ).fetchall()

    # Load exemplars for style reference
    exemplar_texts = []
    for eid in body.exemplar_ids:
        ex = session.execute(
            sa_text("SELECT content, style_notes FROM exemplars WHERE id = :id AND tenant_id = :tid"),
            {"id": eid, "tid": tenant_id},
        ).fetchone()
        if ex:
            exemplar_texts.append(ex["content"])

    # Build generation prompt
    context = {
        "template_content": template["content"],
        "template_variables": body.variables,
        "matter_name": matter["matter_name"],
        "matter_number": matter.get("number", ""),
        "client_name": matter.get("client_name", ""),
        "source_documents": [
            {"name": d["name"], "text": d["ocr_text"][:5000]}
            for d in source_docs
        ],
        "exemplars": [t[:3000] for t in exemplar_texts],
        "assembly_type": body.assembly_type,
    }

    # Generate via AIService
    generated_content = await ai.generate_document(
        tenant_id=tenant_id,
        context=context,
        template_type=template.get("category", "general"),
    )

    # Save generated document to DMS
    doc_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    doc_name = f"DRAFT_{template['name']}_{matter['matter_name'][:30]}.docx"

    # Build the docx from generated content
    docx_bytes = _build_docx(generated_content, doc_name)

    # Upload to storage
    import httpx
    cifs_url = os.environ["CIFS_URL"]
    storage_path = f"{matter.get('matter_number', matter['matter_name'])}/Generated/{doc_name}"

    httpx.post(
        f"{cifs_url}/api/v1/files/upload",
        files={"file": (doc_name, docx_bytes,
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        data={"tenant_id": tenant_id, "path": storage_path},
        timeout=60,
    )

    import hashlib
    session.execute(
        sa_text("""INSERT INTO documents
        (id, tenant_id, matter_id, name, storage_path, file_type,
         file_size, checksum, created_at, modified_at, is_deleted,
         uploaded_by, doc_category)
        VALUES (:id, :tid, :mid, :name, :path, 'docx',
                :size, :cs, :now, :now, 0, :by, 'generated')"""),
        {
            "id": doc_id, "tid": tenant_id, "mid": body.matter_id,
            "name": doc_name, "path": storage_path,
            "size": len(docx_bytes),
            "cs": hashlib.sha256(docx_bytes).hexdigest(),
            "now": now, "by": user_id,
        },
    )

    write_audit(
        tenant_id=tenant_id, user_id=user_id,
        action="generate_document", module="dms", table_name="documents",
        record_id=doc_id,
        new_value={"template": template["name"], "matter": matter["matter_name"]},
        source="doc_generation",
    )
    session.commit()

    return {
        "document_id": doc_id,
        "name": doc_name,
        "gaps": generated_content.get("gaps", []),
        "ai_inferences": generated_content.get("inferences", []),
    }


def _build_docx(content: dict, filename: str) -> bytes:
    """Build a .docx file from generated content structure."""
    from docx import Document

    doc = Document()
    for section in content.get("sections", []):
        if section.get("heading"):
            doc.add_heading(section["heading"], level=section.get("level", 1))
        for para in section.get("paragraphs", []):
            doc.add_paragraph(para)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


# ═══════════════════════════════════════════════════════════════
# SANITY CHECK ENGINE — 8 LAYERS
# ═══════════════════════════════════════════════════════════════

class SanityCheckRequest(BaseModel):
    document_id: str
    matter_id: str
    layers: list[int] = [1, 2, 3, 4, 5, 6, 7, 8]  # Which layers to run


class SanityCheckResult(BaseModel):
    layer: int
    layer_name: str
    status: str  # pass | warning | fail
    issues: list[dict] = []
    score: float = 1.0


LAYER_NAMES = {
    1: "Firm Learning Layer",
    2: "Matter Context",
    3: "Shepards/Citation Check",
    4: "Legal Research",
    5: "Internet & Current Sources",
    6: "Structure & Completeness",
    7: "Style & Professionalism",
    8: "Transactional Compliance",
}


@router.post("/sanity-check")
async def run_sanity_check(body: SanityCheckRequest, request: Request):
    """
    Run the 8-layer sanity check pipeline on a document.
    Each layer is an independent check; all AI calls via AIService.
    """
    from core.services.ai import AIService

    tenant_id = request.state.tenant_id
    session = TenantSession(get_session_factory()(), tenant_id)
    ai = AIService()

    # Load document
    doc = session.execute(
        sa_text("SELECT * FROM documents WHERE id = :id AND tenant_id = :tid"),
        {"id": body.document_id, "tid": tenant_id},
    ).fetchone()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    doc_text = doc.get("ocr_text", "")
    if not doc_text:
        # Try downloading and extracting
        import httpx
        cifs_url = os.environ["CIFS_URL"]
        resp = httpx.get(
            f"{cifs_url}/api/v1/files/download",
            params={"tenant_id": tenant_id, "path": doc["storage_path"]},
            timeout=120,
        )
        from modules.dms.jobs.ocr_pipeline import _process_docx, _process_pdf
        if doc["doc_type"] == "docx":
            doc_text = _process_docx(resp.content)
        elif doc["doc_type"] == "pdf":
            doc_text = _process_pdf(resp.content)

    # Load matter context
    matter = session.execute(
        sa_text("""SELECT m.*, c.client_name as client_name FROM matters m
           LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
           WHERE m.id = :mid AND m.tenant_id = :tid"""),
        {"mid": body.matter_id, "tid": tenant_id},
    ).fetchone()

    results = []

    for layer_num in sorted(body.layers):
        if layer_num not in LAYER_NAMES:
            continue

        try:
            layer_result = await _run_layer(
                layer_num, tenant_id, doc_text, doc, matter, session, ai,
            )
            results.append(layer_result)
        except Exception as e:
            logger.error(f"Layer {layer_num} failed: {e}")
            results.append(SanityCheckResult(
                layer=layer_num,
                layer_name=LAYER_NAMES[layer_num],
                status="error",
                issues=[{"type": "error", "message": str(e)}],
                score=0.0,
            ))

    overall_score = sum(r.score for r in results) / len(results) if results else 0
    return {
        "document_id": body.document_id,
        "overall_score": round(overall_score, 2),
        "layers": [r.dict() for r in results],
    }


async def _run_layer(
    layer_num, tenant_id, doc_text, doc, matter, session, ai,
) -> SanityCheckResult:
    """Run a single sanity check layer."""
    layer_name = LAYER_NAMES[layer_num]

    if layer_num == 1:
        # Layer 1 — Firm Learning Layer
        prior_docs = session.execute(
            sa_text("""SELECT name, ocr_text FROM documents
               WHERE tenant_id = :tid AND doc_category = 'generated'
                AND ocr_text IS NOT NULL
               ORDER BY created_at DESC LIMIT 5"""),
            {"tid": tenant_id},
        ).fetchall()
        result = await ai.sanity_check_layer(
            tenant_id=tenant_id, layer="firm_learning",
            document_text=doc_text[:10000],
            context={"prior_documents": [d["ocr_text"][:3000] for d in prior_docs]},
        )

    elif layer_num == 2:
        # Layer 2 — Matter Context
        result = await ai.sanity_check_layer(
            tenant_id=tenant_id, layer="matter_context",
            document_text=doc_text[:10000],
            context={
                "matter_name": matter["matter_name"] if matter else "",
                "client_name": matter.get("client_name", "") if matter else "",
                "matter_number": matter.get("number", "") if matter else "",
            },
        )

    elif layer_num == 3:
        # Layer 3 — Shepards/Citation Check
        citations = _extract_citations(doc_text)
        cite_results = []
        if citations:
            from modules.dms.adapters.legal_research import LexisAdapter
            adapter = LexisAdapter()
            for cite in citations[:20]:  # Cap at 20 citations
                try:
                    check = await adapter.cite_check(cite)
                    cite_results.append({
                        "citation": cite,
                        "signal": check.signal.value,
                        "treatment": check.treatment,
                    })
                except Exception:
                    cite_results.append({"citation": cite, "signal": "error"})
            await adapter.close()
        result = {"issues": cite_results, "score": _score_citations(cite_results)}

    elif layer_num == 4:
        # Layer 4 — Legal Research
        result = await ai.sanity_check_layer(
            tenant_id=tenant_id, layer="legal_research",
            document_text=doc_text[:10000],
            context={},
        )

    elif layer_num == 5:
        # Layer 5 — Internet & Current Sources
        result = await ai.sanity_check_layer(
            tenant_id=tenant_id, layer="current_sources",
            document_text=doc_text[:10000],
            context={},
        )

    elif layer_num == 6:
        # Layer 6 — Structure & Completeness
        result = await ai.sanity_check_layer(
            tenant_id=tenant_id, layer="structure",
            document_text=doc_text[:10000],
            context={"doc_type": doc.get("file_type", ""), "name": doc["title"]},
        )

    elif layer_num == 7:
        # Layer 7 — Style & Professionalism
        result = await ai.sanity_check_layer(
            tenant_id=tenant_id, layer="style",
            document_text=doc_text[:10000],
            context={},
        )

    elif layer_num == 8:
        # Layer 8 — Transactional Compliance
        result = await ai.sanity_check_layer(
            tenant_id=tenant_id, layer="transactional",
            document_text=doc_text[:10000],
            context={"matter_type": matter.get("matter_type", "") if matter else ""},
        )
    else:
        result = {"issues": [], "score": 1.0}

    issues = result.get("issues", [])
    score = result.get("score", 1.0)
    status = "pass" if score >= 0.8 else ("warning" if score >= 0.5 else "fail")

    return SanityCheckResult(
        layer=layer_num, layer_name=layer_name,
        status=status, issues=issues if isinstance(issues, list) else [],
        score=score,
    )


def _extract_citations(text: str) -> list[str]:
    """Extract legal citations from document text using regex patterns."""
    import re
    patterns = [
        r'\d+\s+[A-Z][a-z]*\.?\s*(?:2d|3d|4th|Supp\.?|App\.?)?\s+\d+',  # Reporter citations
        r'\d+\s+U\.S\.C\.\s*§\s*\d+',  # USC citations
        r'\d+\s+C\.F\.R\.\s*§\s*\d+',  # CFR citations
        r'Fed\.\s*R\.\s*(?:Civ|Crim|App|Evid)\.\s*P\.\s*\d+',  # Federal Rules
    ]
    citations = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            citations.add(match.group().strip())
    return list(citations)[:50]


def _score_citations(cite_results: list[dict]) -> float:
    """Score citation check results."""
    if not cite_results:
        return 1.0
    bad = sum(1 for c in cite_results if c.get("signal") in ("negative", "overruled", "error"))
    return max(0, 1.0 - (bad / len(cite_results)))


# ── Template Management ────────────────────────────────────

@router.get("/templates")
async def list_templates(request: Request):
    tenant_id = request.state.tenant_id
    session = TenantSession(get_session_factory()(), tenant_id)
    templates_list = session.execute(
        sa_text("SELECT id, name, description, category, version, status, created_at "
        "FROM templates WHERE tenant_id = :tid ORDER BY name"),
        {"tid": tenant_id},
    ).fetchall()
    return {"templates": [dict(t) for t in templates_list]}


@router.post("/templates")
async def create_template(request: Request, name: str = Form(...),
                           description: str = Form(""), category: str = Form(""),
                           file: UploadFile = File(...)):
    from core.audit import write_audit

    tenant_id = request.state.tenant_id
    user_id = getattr(request.state.current_user, "id", request.state.current_user) if request.state.current_user else "anonymous"
    session = TenantSession(get_session_factory()(), tenant_id)

    content = (await file.read()).decode("utf-8", errors="replace")
    tpl_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    session.execute(
        sa_text("""INSERT INTO templates
        (id, tenant_id, name, description, category, content,
         version, status, created_at, created_by)
        VALUES (:id, :tid, :name, :desc, :cat, :content,
                1, 'active', :now, :by)"""),
        {
            "id": tpl_id, "tid": tenant_id, "name": name,
            "desc": description, "cat": category,
            "content": content, "now": now, "by": user_id,
        },
    )
    write_audit(
        tenant_id=tenant_id, user_id=user_id,
        action="create", module="dms", table_name="templates",
        record_id=tpl_id, new_value={"name": name},
        source="doc_generation",
    )
    session.commit()
    return {"template_id": tpl_id, "name": name}
