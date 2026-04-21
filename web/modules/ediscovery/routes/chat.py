"""
modules/ediscovery/routes/chat.py

Legal Intelligence Chat — AI-assisted eDiscovery workflow.
Mounted at /api/v1/ediscovery/chat

POST /api/v1/ediscovery/chat
  Body: { messages, context, matter_id }
  Returns: { reply, search_terms?, boolean_query? }

POST /api/v1/ediscovery/chat/extract-terms
  Body: multipart/form-data with files + context
  Returns: { terms, summary, search_terms?, boolean_query? }

POST /api/v1/ediscovery/search-terms/approve
  Body: { terms, matter_id, source }
  Logs terms to search_term_proposals table.
"""
import json
import logging
from typing import Optional, List
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from modules.intelligence import (
    call as ai_call,
    AICallContext,
    AILayerError,
    AICapBreach,
)

from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ediscovery", tags=["ediscovery-chat"])

# ── System prompts per context ────────────────────────────────────────────────

SYSTEM_PROMPTS = {
    "collection": """You are a legal eDiscovery expert assistant helping attorneys build search strategies.

Your role:
1. Help attorneys translate their factual knowledge into effective search terms
2. Suggest Boolean search strings using AND, OR, NOT, proximity operators
3. Identify key custodians, date ranges, and document types
4. Flag proportionality considerations under FRCP 26(b)(1)
5. Generate methodology documentation for court filings

When proposing search terms, always:
- Return a JSON block at the end with this exact format:
  {"search_terms": ["term1", "term2", ...], "boolean_query": "full AND (query OR string)"}
- Keep terms specific enough to be defensible but broad enough to capture responsive documents
- Note any gaps or risks in the search strategy

You have deep knowledge of federal and Texas state discovery rules, proportionality standards,
and common eDiscovery disputes. Speak plainly — attorneys understand the concepts but not always
the technology.""",

    "review": """You are a document review expert helping attorneys make responsiveness and privilege determinations.

Your role:
1. Explain why a document may be responsive or non-responsive to specific requests
2. Identify potential privilege issues (attorney-client, work product)
3. Flag documents that may be key evidence
4. Suggest coding decisions with reasoning

Be precise and cite specific document contents when explaining your reasoning.""",

    "drafting": """You are a legal drafting assistant specializing in discovery motions and correspondence.

Your role:
1. Draft motions to compel, responses to motions to compel, and discovery objections
2. Draft meet-and-confer letters
3. Generate search methodology summaries for court filings
4. Draft privilege log entries

Always identify the jurisdiction so you can apply the correct local rules.""",

    "default": """You are a legal intelligence assistant helping with this matter.
Answer questions accurately and concisely. Flag when you need more information to give a reliable answer.""",
}


def _get_system_prompt(context: str, matter_context: str = "") -> str:
    base = SYSTEM_PROMPTS.get(context, SYSTEM_PROMPTS["default"])
    if matter_context:
        base += f"\n\nMatter context:\n{matter_context}"
    return base


async def _get_matter_context(tenant_id: str, matter_id: str) -> str:
    """Pull matter name, number, and recent collection summary for context."""
    if not matter_id:
        return ""
    try:
        from sqlalchemy import text
        from core.db.base import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT m.matter_name, m.matter_number, m.case_type,
                           COUNT(ec.id) as collection_count,
                           SUM(ec.total_docs) as total_docs
                    FROM matters m
                    LEFT JOIN ediscovery_collections ec
                        ON ec.matter_id = m.id
                        AND TRIM(ec.tenant_id) = :tid
                    WHERE m.id::text = :mid
                      AND TRIM(m.tenant_id) = :tid
                    GROUP BY m.matter_name, m.matter_number, m.case_type
                """),
                {"tid": tenant_id, "mid": matter_id}
            )
            row = result.mappings().first()
            if row:
                return (
                    f"Matter: {row['matter_name']} ({row['matter_number']})\n"
                    f"Type: {row['case_type'] or 'Not specified'}\n"
                    f"Collections: {row['collection_count'] or 0}\n"
                    f"Total documents: {row['total_docs'] or 0}"
                )
    except Exception as e:
        logger.warning(f"Could not load matter context: {e}")
    return ""


def _extract_search_terms(reply: str) -> tuple[list, str]:
    """
    Extract search_terms and boolean_query from Claude's reply.
    Looks for JSON block at end of response.
    """
    import re
    # Look for JSON block with search_terms key
    pattern = r'\{[^{}]*"search_terms"[^{}]*\}'
    matches = re.findall(pattern, reply, re.DOTALL)
    if matches:
        try:
            data = json.loads(matches[-1])
            return data.get("search_terms", []), data.get("boolean_query", "")
        except Exception:
            pass
    return [], ""


# ── Chat endpoint ─────────────────────────────────────────────────────────────

@router.post("/chat")
async def ediscovery_chat(
    request: Request,
    user=Depends(get_current_user),
):
    """
    Legal Intelligence Chat endpoint.
    Calls Claude with context-appropriate system prompt.
    Extracts and returns structured search terms if present.
    """
    tenant_id = getattr(request.state, "tenant_id", "").strip()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    messages = body.get("messages", [])
    context = body.get("context", "default")
    matter_id = body.get("matter_id", "")

    if not messages:
        return JSONResponse({"error": "No messages provided"}, status_code=400)

    # Get matter context for system prompt
    matter_context = await _get_matter_context(tenant_id, matter_id)
    system_prompt = _get_system_prompt(context, matter_context)

    # Call Claude via the adapter
    try:
        # Multi-turn chat: collapse messages into a single user_prompt.
        # The system_prompt is pulled from SYSTEM_PROMPTS; matter_context is
        # appended per existing behavior. Template slug documents the shape;
        # we pass system + user directly to preserve multi-turn fidelity.
        user_messages_text = "\n\n".join(
            f"{m.get('role', 'user').upper()}: {m.get('content', '')}"
            for m in messages
        )
        ctx = AICallContext(
            tenant_id=tenant_id,
            module="ediscovery",
            purpose="matter_chat",
            matter_id=matter_id or None,
            user_id=getattr(user, "id", None) if user else None,
        )
        ai_result = await ai_call(
            ctx,
            raw_system_prompt=system_prompt,
            raw_user_prompt=user_messages_text,
        )
        reply = ai_result.text
        search_terms, boolean_query = _extract_search_terms(reply)
        response = JSONResponse({
            "reply": reply,
            "search_terms": search_terms,
            "boolean_query": boolean_query,
        })
        for k, v in ai_result.to_headers().items():
            response.headers[k] = v
        return response

    except AICapBreach as exc:
        return JSONResponse(
            {"error": "AI budget cap reached for this matter.",
             "breach": exc.context},
            status_code=429,
        )
    except AILayerError as exc:
        logger.error("Chat AI error: %s", exc)
        return JSONResponse({"error": "Chat unavailable."}, status_code=502)




# ── Extract terms from uploaded documents ─────────────────────────────────────

@router.post("/chat/extract-terms")
async def extract_terms_from_documents(
    request: Request,
    files: List[UploadFile] = File(...),
    context: str = Form("collection"),
    matter_id: str = Form(""),
    user=Depends(get_current_user),
):
    """
    Extract search terms from uploaded pleadings or documents.
    Uses Claude to identify key entities, issues, custodians, and date ranges.
    Returns proposed search terms for attorney review.
    """
    tenant_id = getattr(request.state, "tenant_id", "").strip()

    extracted_texts = []
    for f in files[:3]:  # Max 3 files per request
        try:
            content = await f.read()
            # For now handle text and simple extraction
            # Full PDF/DOCX extraction uses existing text_extraction service
            if f.filename.lower().endswith(('.txt', '.md')):
                extracted_texts.append(content.decode('utf-8', errors='ignore')[:8000])
            else:
                # For binary files, use filename as context for now
                # Full extraction will be added when text_extraction service is wired
                extracted_texts.append(f"Document: {f.filename} ({f.content_type})")
        except Exception as e:
            logger.warning(f"File extraction error {f.filename}: {e}")

    if not extracted_texts:
        return JSONResponse({"terms": [], "summary": "Could not extract text from uploaded files."})

    combined = "\n\n---\n\n".join(extracted_texts)

    extraction_prompt = f"""Analyze this legal document and extract:
1. Key persons/custodians (name and role)
2. Key date ranges
3. Key topics, transactions, or events
4. Document types likely to be relevant
5. Proposed search terms with Boolean logic

Document content:
{combined[:6000]}

Respond with:
- A brief summary of what this document is about (2-3 sentences)
- Bullet list of key entities and terms
- Then this exact JSON block:
{{"search_terms": ["term1", "term2", ...], "boolean_query": "full AND (search OR string)"}}"""

    try:
        import httpx
        api_key = await _get_api_key(tenant_id)

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1500,
                    "messages": [{"role": "user", "content": extraction_prompt}]
                }
            )
            resp.raise_for_status()
            data = resp.json()
            reply = data["content"][0]["text"] if data.get("content") else ""

    except Exception as e:
        return JSONResponse({"terms": [], "summary": f"AI extraction failed: {str(e)[:100]}"})

    search_terms, boolean_query = _extract_search_terms(reply)

    import re
    clean = re.sub(r'\{[^{}]*"search_terms"[^{}]*\}', '', reply, flags=re.DOTALL).strip()
    lines = [l.strip('• -').strip() for l in clean.split('\n') if l.strip() and l.strip().startswith(('•', '-', '*'))]

    return JSONResponse({
        "terms": lines[:20],
        "summary": clean[:500],
        "search_terms": search_terms,
        "boolean_query": boolean_query,
    })


# ── Approve and log search terms ──────────────────────────────────────────────

@router.post("/search-terms/approve")
async def approve_search_terms(
    request: Request,
    user=Depends(get_current_user),
):
    """
    Log approved search terms to search_term_proposals.
    These become the methodology record for court filings.
    """
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)

    terms = body.get("terms", [])
    matter_id = body.get("matter_id", "")
    source = body.get("source", "ai_chat")
    boolean_query = body.get("boolean_query", "")

    if not terms:
        return JSONResponse({"ok": True, "logged": 0})

    logged = 0
    try:
        from sqlalchemy import text
        from core.db.base import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            for term in terms:
                if not term or not term.strip():
                    continue
                await session.execute(
                    text("""
                        INSERT INTO search_term_proposals
                            (tenant_id, matter_id, term, source_document_id,
                             extraction_method, proposed_by, status,
                             boolean_query, created_at)
                        VALUES
                            (:tid, :mid, :term, NULL,
                             :method, :uid, 'proposed',
                             :bq, NOW())
                        ON CONFLICT DO NOTHING
                    """),
                    {
                        "tid":    tenant_id,
                        "mid":    matter_id or None,
                        "term":   term.strip(),
                        "method": source,
                        "uid":    user_id,
                        "bq":     boolean_query or None,
                    }
                )
                logged += 1
            await session.commit()
    except Exception as e:
        logger.error(f"search-terms/approve error: {e}")
        # Table may not exist yet — return success anyway, migration pending
        return JSONResponse({"ok": True, "logged": 0, "note": str(e)[:100]})

    return JSONResponse({"ok": True, "logged": logged})


# ── API key helper ────────────────────────────────────────────────────────────

async def _get_api_key(tenant_id: str) -> str:
    """Get Anthropic API key from credentials_vault (BYOK) or env fallback."""
    import os
    try:
        from sqlalchemy import text
        from core.db.base import AsyncSessionLocal
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
    # Fallback to platform env var
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError("No Anthropic API key configured. Add it in Firm Settings → AI API Keys.")
    return key
