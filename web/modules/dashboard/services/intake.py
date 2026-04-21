"""COMP 1 — Matter Intake: Drag-and-Drop OCR.

Handles document upload, OCR (Tesseract for image PDFs, native for text),
AI field extraction, multi-doc merge, QC review, and auto-provisioning.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules.dashboard.services._audit_safe import safe_audit
from core.db.session import TenantSession
from core.services import get_ai_service, get_storage_service, get_calendar_service
from modules.dashboard.models import IntakeSession

logger = logging.getLogger(__name__)


async def create_intake_session(ts: TenantSession, user_id: int) -> IntakeSession:
    """Start a new intake session."""
    session = IntakeSession(
        tenant_id=ts.tenant_id,
        initiated_by=user_id,
        status="uploading",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    ts.session.add(session)
    await ts.session.flush()
    await safe_audit(ts, "CREATE", "intake_sessions", session.id, user_id=user_id)
    return session


async def process_uploaded_documents(
    ts: TenantSession,
    intake_id: int,
    file_paths: list[str],
) -> dict[str, Any]:
    """Process uploaded documents: OCR if needed, extract text.

    Returns combined extracted text and metadata.
    """
    intake = await ts.get("intake_sessions", intake_id)
    intake.status = "extracting"
    intake.updated_at = datetime.now(timezone.utc)

    all_text: list[str] = []
    ocr_results: list[dict] = []

    for fp in file_paths:
        path = Path(fp)
        ext = path.suffix.lower()

        if ext == ".pdf":
            text, was_ocr = await _extract_pdf(path)
        elif ext in (".doc", ".docx"):
            text = await _extract_word(path)
            was_ocr = False
        else:
            text = path.read_text(errors="replace")
            was_ocr = False

        all_text.append(text)
        ocr_results.append({
            "filename": path.name,
            "ocr_used": was_ocr,
            "char_count": len(text),
        })

    combined_text = "\n\n---\n\n".join(all_text)
    intake.ocr_results = ocr_results
    await ts.session.flush()

    return {"combined_text": combined_text, "ocr_results": ocr_results}


async def extract_fields_with_ai(
    ts: TenantSession,
    intake_id: int,
    combined_text: str,
) -> dict[str, Any]:
    """Use AIService to extract structured fields from combined document text."""
    ai = get_ai_service(ts.tenant_id)

    prompt = (
        "Extract the following fields from this legal document. "
        "Return JSON with keys: client_name, client_type (individual|entity), "
        "adverse_parties (list of names), matter_type (litigation|transactional|"
        "real_estate|family|probate|other), court_name, case_number, judge_name, "
        "filing_date, cause_of_action_list (list of strings), "
        "key_dates (list of {name, date, description}). "
        "If a field is not found, set it to null.\n\n"
        f"DOCUMENT TEXT:\n{combined_text[:15000]}"
    )

    result = await ai.complete(
        prompt=prompt,
        system="You are a legal document analyzer. Return valid JSON only.",
        max_tokens=2000,
        temperature=0.1,
    )

    import json
    try:
        fields = json.loads(result.content)
    except json.JSONDecodeError:
        fields = {"raw_response": result.content, "parse_error": True}

    intake = await ts.get("intake_sessions", intake_id)
    intake.extracted_fields = fields
    intake.status = "qc_review"
    intake.updated_at = datetime.now(timezone.utc)
    await ts.session.flush()

    await safe_audit(
        ts, "UPDATE", "intake_sessions", intake_id,
        new_values={"status": "qc_review", "fields_extracted": True},
    )
    return fields


async def confirm_intake(
    ts: TenantSession,
    intake_id: int,
    confirmed_fields: dict[str, Any],
    user_id: int,
) -> dict[str, Any]:
    """Confirm intake: create matter, provision folders, calendar, contacts.

    This is called after QC review and conflict check pass.
    """
    from modules.dashboard.services.conflict_checker import run_conflict_check

    intake = await ts.get("intake_sessions", intake_id)

    # Run conflict check on adverse parties
    adverse = confirmed_fields.get("adverse_parties", [])
    parties = [{"name": n} for n in adverse if n]
    conflict = await run_conflict_check(ts, parties, checked_by=user_id)

    if conflict.overall_result == "hard_conflict":
        intake.status = "conflict_check"
        await ts.session.flush()
        return {
            "status": "blocked",
            "conflict_check_id": conflict.id,
            "message": "Hard conflict detected — super_admin override required.",
        }

    # Create the matter
    matter_data = {
        "tenant_id": ts.tenant_id,
        "client_name": confirmed_fields.get("client_name"),
        "matter_type": confirmed_fields.get("matter_type", "litigation"),
        "court_name": confirmed_fields.get("court_name"),
        "case_number": confirmed_fields.get("case_number"),
        "judge_name": confirmed_fields.get("judge_name"),
        "status": "active",
    }
    matter_id = await ts.insert("matters", matter_data)

    # Provision DMS folders
    storage = get_storage_service(ts.tenant_id)
    await storage.create_matter_folder(ts.tenant_id, matter_id)

    # Calendar key dates
    calendar = get_calendar_service(ts.tenant_id)
    for kd in confirmed_fields.get("key_dates", []):
        if kd.get("date"):
            await calendar.create_event(
                ts.tenant_id,
                title=f"{confirmed_fields.get('client_name', 'Matter')} — {kd['name']}",
                date=kd["date"],
                matter_id=matter_id,
            )

    # Update intake
    intake.matter_id = matter_id
    intake.status = "confirmed"
    intake.extracted_fields = confirmed_fields
    intake.updated_at = datetime.now(timezone.utc)
    await ts.session.flush()

    await safe_audit(
        ts, "UPDATE", "intake_sessions", intake_id,
        new_values={"matter_id": matter_id},
        user_id=user_id,
    )

    return {
        "status": "confirmed",
        "matter_id": matter_id,
        "conflict_result": conflict.overall_result,
    }


# ── Internal helpers ──────────────────────────────────────────

async def _extract_pdf(path: Path) -> tuple[str, bool]:
    """Extract text from PDF.  Falls back to Tesseract OCR for image PDFs."""
    import fitz  # PyMuPDF

    doc = fitz.open(str(path))
    text_parts: list[str] = []
    has_text = False

    for page in doc:
        t = page.get_text()
        if t.strip():
            text_parts.append(t)
            has_text = True

    doc.close()

    if has_text:
        return "\n".join(text_parts), False

    # Image PDF — use Tesseract
    return await _ocr_pdf(path), True


async def _ocr_pdf(path: Path) -> str:
    """OCR an image-based PDF using Tesseract."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Convert PDF pages to images
        subprocess.run(
            ["pdftoppm", "-jpeg", "-r", "300", str(path), f"{tmpdir}/page"],
            check=True, capture_output=True,
        )
        # OCR each image
        text_parts: list[str] = []
        for img in sorted(Path(tmpdir).glob("page-*.jpg")):
            result = subprocess.run(
                ["tesseract", str(img), "stdout", "-l", "eng"],
                capture_output=True, text=True,
            )
            text_parts.append(result.stdout)

    return "\n".join(text_parts)


async def _extract_word(path: Path) -> str:
    """Extract text from Word document."""
    import docx

    doc = docx.Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
