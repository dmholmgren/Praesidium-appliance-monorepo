from sqlalchemy import text as sa_text
from modules.dms.brand_helper import get_brand
"""
COMP 8 — Document Comparison & Redlining
POST /api/v1/dms/documents/compare
- Downloads both documents from StorageService
- Uses python-docx to extract paragraphs
- Runs difflib to identify changes
- Generates .docx with tracked changes (w:del, w:ins)
- Returns download URL for redlined document
- Saves redline to DMS under same matter
"""

import os
import io
import uuid
import difflib
import logging
import hashlib
from datetime import datetime, timezone
from typing import Optional
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dms/documents", tags=["dms-compare"])


class CompareRequest(BaseModel):
    doc_a_id: str
    doc_b_id: str


@router.post("/compare")
async def compare_documents(body: CompareRequest, request: Request):
    """Compare two documents and generate a redlined output."""
    from core.db.base import TenantSession, get_session_factory
    from core.audit import write_audit
    import httpx

    tenant_id = request.state.tenant_id
    user_id = getattr(request.state.current_user, "id", request.state.current_user) if request.state.current_user else "anonymous"
    session = TenantSession(get_session_factory()(), tenant_id)
    cifs_url = os.environ["CIFS_URL"]

    # Fetch both document records
    doc_a = session.execute(
        sa_text("SELECT * FROM documents WHERE id = :id AND tenant_id = :tid"),
        {"id": body.doc_a_id, "tid": tenant_id},
    ).fetchone()
    doc_b = session.execute(
        sa_text("SELECT * FROM documents WHERE id = :id AND tenant_id = :tid"),
        {"id": body.doc_b_id, "tid": tenant_id},
    ).fetchone()

    if not doc_a or not doc_b:
        raise HTTPException(status_code=404, detail="Document(s) not found")

    # Download both files
    try:
        resp_a = httpx.get(
            f"{cifs_url}/api/v1/files/download",
            params={"tenant_id": tenant_id, "path": doc_a["storage_path"]},
            timeout=120,
        )
        resp_b = httpx.get(
            f"{cifs_url}/api/v1/files/download",
            params={"tenant_id": tenant_id, "path": doc_b["storage_path"]},
            timeout=120,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {e}")

    # Extract text from both documents
    text_a = _extract_paragraphs(resp_a.content, doc_a["file_type"])
    text_b = _extract_paragraphs(resp_b.content, doc_b["file_type"])

    # Generate redline
    redline_content = _generate_redline(
        text_a, text_b, doc_a["name"], doc_b["name"]
    )

    # Save redline to DMS
    redline_name = f"REDLINE_{doc_a['name']}_vs_{doc_b['name']}.docx"
    redline_path = f"{os.path.dirname(doc_a['storage_path'])}/{redline_name}"
    checksum = hashlib.sha256(redline_content).hexdigest()

    # Upload to storage
    httpx.post(
        f"{cifs_url}/api/v1/files/upload",
        files={"file": (redline_name, redline_content,
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        data={"tenant_id": tenant_id, "path": redline_path},
        timeout=60,
    )

    # Create document record
    redline_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    session.execute(
        sa_text("""INSERT INTO documents
        (id, tenant_id, matter_id, name, storage_path, file_type,
         file_size, checksum, created_at, modified_at, is_deleted, uploaded_by)
        VALUES (:id, :tid, :mid, :name, :path, 'docx',
                :size, :cs, :now, :now, 0, :by)"""),
        {
            "id": redline_id, "tid": tenant_id,
            "mid": doc_a.get("matter_id"),
            "name": redline_name, "path": redline_path,
            "size": len(redline_content), "cs": checksum,
            "now": now, "by": user_id,
        },
    )

    write_audit(
        tenant_id=tenant_id, user_id=user_id,
        action="compare", module="dms", table_name="documents",
        record_id=redline_id,
        new_value={
            "doc_a": body.doc_a_id, "doc_b": body.doc_b_id,
            "redline": redline_name,
        },
        source="dms_compare",
    )
    session.commit()

    return {
        "redline_id": redline_id,
        "redline_name": redline_name,
        "changes": _count_changes(text_a, text_b),
    }


def _extract_paragraphs(content: bytes, file_type: str) -> list[str]:
    """Extract paragraphs from a document for comparison."""
    if file_type in ("docx",):
        from docx import Document
        doc = Document(io.BytesIO(content))
        return [p.text for p in doc.paragraphs]
    elif file_type in ("txt", "rtf"):
        return content.decode("utf-8", errors="replace").split("\n")
    elif file_type in ("pdf",):
        import pdfplumber
        parts = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                parts.extend(text.split("\n"))
        return parts
    return [content.decode("utf-8", errors="replace")]


def _generate_redline(
    text_a: list[str],
    text_b: list[str],
    name_a: str,
    name_b: str,
) -> bytes:
    """
    Generate a .docx with tracked changes showing differences.
    Deletions marked as w:del, insertions as w:ins.
    """
    from docx import Document
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    doc = Document()

    # Add header
    doc.add_paragraph(
        f"Redline Comparison", style="Heading 1"
    )
    doc.add_paragraph(f"Original: {name_a}")
    doc.add_paragraph(f"Modified: {name_b}")
    doc.add_paragraph(
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    doc.add_paragraph("")  # Spacer

    # Compute diff
    matcher = difflib.SequenceMatcher(None, text_a, text_b)
    now_iso = datetime.now(timezone.utc).isoformat() + "Z"
    change_id = 1

    for op, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if op == "equal":
            for line in text_a[a_start:a_end]:
                if line.strip():
                    doc.add_paragraph(line)

        elif op == "delete":
            for line in text_a[a_start:a_end]:
                if line.strip():
                    p = doc.add_paragraph()
                    _add_deletion(p, line, change_id, now_iso)
                    change_id += 1

        elif op == "insert":
            for line in text_b[b_start:b_end]:
                if line.strip():
                    p = doc.add_paragraph()
                    _add_insertion(p, line, change_id, now_iso)
                    change_id += 1

        elif op == "replace":
            for line in text_a[a_start:a_end]:
                if line.strip():
                    p = doc.add_paragraph()
                    _add_deletion(p, line, change_id, now_iso)
                    change_id += 1
            for line in text_b[b_start:b_end]:
                if line.strip():
                    p = doc.add_paragraph()
                    _add_insertion(p, line, change_id, now_iso)
                    change_id += 1

    # Save to bytes
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def _add_deletion(paragraph, text, change_id, date_str):
    """Add a deletion tracked change to a paragraph."""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    del_elem = OxmlElement("w:del")
    del_elem.set(qn("w:id"), str(change_id))
    del_elem.set(qn("w:author"), "Comparison Engine")
    del_elem.set(qn("w:date"), date_str)

    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    strike = OxmlElement("w:strike")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "FF0000")
    rpr.append(strike)
    rpr.append(color)
    run.append(rpr)

    del_text = OxmlElement("w:delText")
    del_text.set(qn("xml:space"), "preserve")
    del_text.text = text
    run.append(del_text)
    del_elem.append(run)
    paragraph._p.append(del_elem)


def _add_insertion(paragraph, text, change_id, date_str):
    """Add an insertion tracked change to a paragraph."""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    ins_elem = OxmlElement("w:ins")
    ins_elem.set(qn("w:id"), str(change_id))
    ins_elem.set(qn("w:author"), "Comparison Engine")
    ins_elem.set(qn("w:date"), date_str)

    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0000FF")
    rpr.append(underline)
    rpr.append(color)
    run.append(rpr)

    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    run.append(t)
    ins_elem.append(run)
    paragraph._p.append(ins_elem)


def _count_changes(text_a: list[str], text_b: list[str]) -> dict:
    """Count insertions, deletions, and modifications."""
    matcher = difflib.SequenceMatcher(None, text_a, text_b)
    counts = {"insertions": 0, "deletions": 0, "modifications": 0, "unchanged": 0}
    for op, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if op == "equal":
            counts["unchanged"] += a_end - a_start
        elif op == "delete":
            counts["deletions"] += a_end - a_start
        elif op == "insert":
            counts["insertions"] += b_end - b_start
        elif op == "replace":
            counts["modifications"] += max(a_end - a_start, b_end - b_start)
    return counts
