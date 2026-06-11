"""
Billing Matter Intake API
POST /api/v1/billing/intake/analyze

Single endpoint: upload document(s) → AI extracts matter fields → return JSON.
No intake session table needed — stateless. The widget holds state in JS
until attorney confirms, then POSTs to existing matter creation routes.

Uses the same patterns as ediscovery/routes/chat.py:
  - credentials_vault for Anthropic key
  - httpx for API call
  - PyMuPDF / python-docx for text extraction
"""

import json
import logging
import tempfile
from pathlib import Path
from typing import List

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse

from modules.intelligence import (
    call as ai_call,
    resolve_prompt,
    AICallContext,
    AICapBreach,
    AILayerError,
    strip_markdown_fences,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/billing", tags=["billing-intake"])


# ---------------------------------------------------------------------------
# API key, routing, prompt rendering, cost caps, and call attribution are now
# handled by modules.intelligence.anthropic_adapter.
# ---------------------------------------------------------------------------

async def _unused_api_key_stub(tenant_id: str) -> str:
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT encrypted_key FROM credentials_vault
                    WHERE TRIM(tenant_id) = :tid
                      AND provider = 'anthropic'
                      AND key_type = 'api_key'
                    LIMIT 1
                """),
                {"tid": tenant_id}
            )
            row = result.first()
            if row and row[0]:
                return row[0]
    except Exception:
        pass
    import os
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError("No Anthropic API key configured.")
    return key


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

def _extract_text(path: Path) -> str:
    """Extract text from PDF, DOCX, or plain text file."""
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(path))
            parts = []
            for page in doc:
                t = page.get_text()
                if t.strip():
                    parts.append(t)
            doc.close()
            if parts:
                return "\n".join(parts)
        except ImportError:
            pass
        # Fallback: return filename context
        return f"[PDF document: {path.name}]"

    if suffix in (".docx", ".doc"):
        try:
            import docx
            doc = docx.Document(str(path))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except ImportError:
            pass
        return f"[Word document: {path.name}]"

    # Plain text / image fallback
    try:
        return path.read_text(errors="replace")
    except Exception:
        return f"[Document: {path.name}]"


# ---------------------------------------------------------------------------
# Main intake analyze endpoint
# ---------------------------------------------------------------------------

@router.post("/intake/analyze")
async def analyze_intake_document(
    request: Request,
    files: List[UploadFile] = File(...),
):
    """
    Upload one or more documents. AI extracts matter fields.
    Returns proposed fields for attorney review — no DB writes.
    Attorney confirms via existing matter creation routes.
    """
    tenant_id = (getattr(request.state, "tenant_id", "") or "").strip()

    # Save uploads to temp files and extract text
    texts = []
    for f in files[:3]:
        try:
            content = await f.read()
            suffix  = Path(f.filename or "upload").suffix or ".pdf"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(content)
                tmp_path = Path(tmp.name)
            text_content = _extract_text(tmp_path)
            texts.append(text_content[:8000])
            tmp_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("File extraction error %s: %s", f.filename, exc)

    if not texts:
        return JSONResponse(
            {"error": "Could not extract text from uploaded files."},
            status_code=400,
        )

    combined = "\n\n---\n\n".join(texts)

    prompt = f"""Analyze this legal document and extract the following fields.
Return ONLY a valid JSON object with these exact keys:

{{
  "client_name": "Full name of the client/plaintiff/party we represent",
  "client_type": "individual or entity",
  "matter_type": "litigation, transactional, real_estate, family, probate, or other",
  "court_name": "Full court name or null",
  "case_number": "Case/cause number or null",
  "judge_name": "Judge name or null",
  "adverse_parties": ["List", "of", "opposing", "party", "names"],
  "cause_of_action": ["List", "of", "causes", "of", "action"],
  "key_dates": [
    {{"name": "Answer Due", "date": "YYYY-MM-DD", "description": "brief description"}},
    {{"name": "Trial", "date": "YYYY-MM-DD", "description": "brief description"}}
  ],
  "filing_date": "YYYY-MM-DD or null",
  "summary": "2-3 sentence description of what this matter is about"
}}

If a field cannot be determined, use null or an empty array.
Do not include any text outside the JSON object.

DOCUMENT:
{combined[:10000]}"""

    try:
        # Render prompt from prompt_templates (versioned)
        rendered = await resolve_prompt(
            tenant_id=tenant_id,
            slug="billing.intake_analyze",
            variables={"document_text": combined[:10000]},
        )
        # Get user_id if present in request state for attribution
        user_id = getattr(getattr(request.state, "user", None), "id", None)
        ctx = AICallContext(
            tenant_id=tenant_id,
            module="billing",
            purpose="intake_analyze",
            user_id=user_id,
            matter_id=None,  # Intake is pre-matter by definition
        )
        result = await ai_call(ctx, prompt=rendered)

        # Parse JSON — strip any markdown fences (adapter utility)
        raw_text = strip_markdown_fences(result.text)
        fields = json.loads(raw_text)
        response = JSONResponse({"extracted_fields": fields, "status": "ok"})
        # Forward budget warnings as headers for ambient UX
        for k, v in result.to_headers().items():
            response.headers[k] = v
        return response

    except AICapBreach as exc:
        return JSONResponse(
            {"error": "AI budget cap reached for this workflow.",
             "breach": exc.context},
            status_code=429,
        )
    except AILayerError as exc:
        logger.error("AI layer error in intake: %s", exc)
        return JSONResponse(
            {"error": "AI analysis failed. Check API key configuration."},
            status_code=502,
        )

    except json.JSONDecodeError as exc:
        logger.error("JSON parse error in intake: %s", exc)
        return JSONResponse(
            {"error": "AI returned unexpected format. Please try again."},
            status_code=422,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("Intake analyze error: %s", exc)
        return JSONResponse(
            {"error": "Document analysis failed. Please try again."},
            status_code=500,
        )
from core.db.base import AsyncSessionLocal as _AsyncSL
from sqlalchemy import text as _text
from fastapi.responses import JSONResponse as _JSONResponse


@router.get("/timekeepers")
async def list_timekeepers_v1(request: Request):
    """
    Alias for /api/billing/timekeepers.
    The billing_matter_intake widget fetches from /api/v1/billing/timekeepers.
    """
    tenant_id = (getattr(request.state, "tenant_id", "") or "").strip()
    async with _AsyncSL() as session:
        result = await session.execute(
            _text("""
                SELECT ts_tk_id, ts_name, ts_initials
                FROM ts_timekeepers
                WHERE trim(tenant_id) = trim(:tid)
                ORDER BY ts_name
            """),
            {"tid": tenant_id},
        )
        tks = [dict(r._mapping) for r in result.fetchall()]
    return _JSONResponse({"timekeepers": tks})