"""
modules/drafting/assembly_service.py

Document assembly service — Module 4 core engine.

Responsibilities:
    1. Load source documents from dms_documents for the matter
    2. Load template body and variable manifest from template_library
    3. Load exemplar style summaries from exemplar_library
    4. Resolve tenant Anthropic API key (vault first, env fallback)
    5. Call Anthropic to generate first draft with gaps/inferences flagged
    6. Write drafting_document record; log AI contribution

Called by:
    drafting_service.py (session orchestrator)

Never called by:
    Routes, templates, or any HTTP layer

Architecture:
    - All DB access via AsyncSessionLocal -- never get_session_factory()
    - AI key: credentials_vault (provider='anthropic', key_type='api_key')
              falls back to ANTHROPIC_API_KEY env var if not in vault
    - credentials_vault schema: id, tenant_id CHAR(36), provider TEXT,
      key_type TEXT, encrypted_key TEXT, key_hint TEXT, created_at, updated_at
    - tenant_id always .strip() before DB queries
    - Sync Anthropic client (same pattern as tag_intelligence.py)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

MAX_SOURCE_CHARS = 80_000
MAX_EXEMPLAR_CHARS = 4_000
MAX_EXEMPLARS = 3


@dataclass
class GapFlag:
    location: str
    description: str
    suggested_source: str


@dataclass
class InferenceFlag:
    location: str
    inference: str
    confidence: float
    basis: str


@dataclass
class AssemblyResult:
    session_id: UUID
    document_id: UUID
    body_text: str
    gaps: list[GapFlag] = field(default_factory=list)
    inferences: list[InferenceFlag] = field(default_factory=list)
    ai_contribution_id: Optional[UUID] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_used: str = ''


async def _get_anthropic_key(tenant_id: str) -> str:
    """
    Resolve Anthropic API key for tenant.
    1. credentials_vault WHERE provider='anthropic' AND key_type='api_key'
    2. ANTHROPIC_API_KEY env var (HJMM transition fallback)
    Raises ValueError if neither found.
    """
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            text(
                "SELECT encrypted_key FROM credentials_vault "
                "WHERE trim(tenant_id) = :tid "
                "  AND provider = 'anthropic' "
                "  AND key_type = 'api_key' "
                "LIMIT 1"
            ),
            {'tid': tenant_id.strip()},
        )
        rec = row.fetchone()
        if rec and rec.encrypted_key:
            return rec.encrypted_key

    env_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if env_key:
        return env_key

    raise ValueError(
        f"No Anthropic API key for tenant '{tenant_id}'. "
        "Configure via Tenant Admin > API Keys or set ANTHROPIC_API_KEY."
    )


async def _load_source_documents(
    tenant_id: str,
    matter_id: Optional[UUID],
    doc_ids: list[str],
) -> list[dict]:
    async with AsyncSessionLocal() as session:
        if doc_ids:
            rows = await session.execute(
                text(
                    "SELECT file_name, content_text, file_path "
                    "FROM dms_documents "
                    "WHERE id = ANY(:ids) "
                    "  AND ocr_status IN ('text_native', 'ocr_complete') "
                    "ORDER BY file_name"
                ),
                {'ids': doc_ids},
            )
        elif matter_id:
            rows = await session.execute(
                text(
                    "SELECT d.file_name, d.content_text, d.file_path "
                    "FROM dms_documents d "
                    "JOIN matters m ON d.file_path LIKE '%' || m.folder_path || '%' "
                    "WHERE m.id = :mid "
                    "  AND trim(m.tenant_id) = :tid "
                    "  AND d.ocr_status IN ('text_native', 'ocr_complete') "
                    "  AND d.content_text IS NOT NULL "
                    "ORDER BY d.file_name "
                    "LIMIT 50"
                ),
                {'mid': str(matter_id), 'tid': tenant_id.strip()},
            )
        else:
            return []

        return [
            {
                'file_name': r.file_name,
                'content_text': (r.content_text or '')[:8000],
                'file_path': r.file_path,
            }
            for r in rows.fetchall()
            if r.content_text
        ]


async def _load_template(tenant_id: str, template_id: UUID) -> Optional[dict]:
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            text(
                "SELECT name, body_text, variables, document_type, practice_area "
                "FROM template_library "
                "WHERE id = :tid AND trim(tenant_id) = :ten AND is_active = TRUE"
            ),
            {'tid': str(template_id), 'ten': tenant_id.strip()},
        )
        r = row.fetchone()
        if not r:
            return None
        return {
            'name': r.name,
            'body_text': r.body_text,
            'variables': r.variables if r.variables else [],
            'document_type': r.document_type,
            'practice_area': r.practice_area,
        }


async def _load_exemplars(
    tenant_id: str,
    document_type: str,
    practice_area: str,
    matter_id: Optional[UUID],
) -> list[str]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            text(
                "SELECT style_summary "
                "FROM exemplar_library "
                "WHERE trim(tenant_id) = :tid "
                "  AND document_type = :dt "
                "  AND practice_area = :pa "
                "  AND is_active = TRUE "
                "  AND style_summary IS NOT NULL "
                "ORDER BY "
                "  CASE WHEN matter_id = :mid THEN 0 ELSE 1 END, "
                "  created_at DESC "
                "LIMIT :lim"
            ),
            {
                'tid': tenant_id.strip(),
                'dt': document_type,
                'pa': practice_area,
                'mid': str(matter_id) if matter_id else None,
                'lim': MAX_EXEMPLARS,
            },
        )
        return [
            r.style_summary[:MAX_EXEMPLAR_CHARS]
            for r in rows.fetchall()
            if r.style_summary
        ]


async def _load_matter_context(tenant_id: str, matter_id: UUID) -> dict:
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            text(
                "SELECT m.matter_name as name, m.matter_number, m.practice_area, "
                "       c.name as client_name "
                "FROM matters m "
                "LEFT JOIN clients c ON m.client_id = c.id "
                "WHERE m.id = :mid AND trim(m.tenant_id) = :tid"
            ),
            {'mid': str(matter_id), 'tid': tenant_id.strip()},
        )
        r = row.fetchone()
        if not r:
            return {}
        return {
            'matter_name': r.name,
            'matter_number': r.matter_number,
            'client_name': r.client_name,
            'practice_area': r.practice_area,
        }


def _build_assembly_prompt(
    document_type: str,
    practice_area: str,
    template: Optional[dict],
    source_docs: list[dict],
    exemplars: list[str],
    matter_context: dict,
    user_instructions: Optional[str],
) -> str:
    parts = [
        f"You are a legal document drafting assistant. "
        f"Generate a first draft {document_type.replace('_', ' ')} "
        f"for a {practice_area} matter."
    ]

    if matter_context:
        parts.append("\n## Matter Context")
        if matter_context.get('client_name'):
            parts.append(f"Client: {matter_context['client_name']}")
        if matter_context.get('matter_name'):
            parts.append(f"Matter: {matter_context['matter_name']}")
        if matter_context.get('matter_number'):
            parts.append(f"Matter Number: {matter_context['matter_number']}")

    if user_instructions:
        parts.append(f"\n## Drafting Instructions\n{user_instructions}")

    if template and template.get('body_text'):
        parts.append(
            "\n## Template Structure\n"
            "Use this template as your structural guide. "
            "Fill {{variable}} placeholders from source documents. "
            "Where information is unavailable insert [GAP: description].\n"
            + template['body_text'][:20_000]
        )
    else:
        parts.append(
            "\n## Instructions\n"
            "Draft a complete, professional document from the source materials. "
            "Where information is unavailable insert [GAP: description]."
        )

    if exemplars:
        parts.append("\n## Firm Style Reference\nMatch this firm's voice and style:")
        for i, ex in enumerate(exemplars, 1):
            parts.append(f"\nExemplar {i}:\n{ex}")

    if source_docs:
        parts.append("\n## Source Documents\n"
                     "Extract relevant facts, dates, parties, and terms:")
        total_chars = 0
        for doc in source_docs:
            if total_chars >= MAX_SOURCE_CHARS:
                parts.append(f"\n[Remaining docs truncated — {len(source_docs)} total]")
                break
            content = doc['content_text']
            remaining = MAX_SOURCE_CHARS - total_chars
            if len(content) > remaining:
                content = content[:remaining] + '\n[truncated]'
            parts.append(f"\n### {doc['file_name']}\n{content}")
            total_chars += len(content)
    else:
        parts.append("\n## Note\nNo source documents provided. Mark all variable fields as [GAP: field name].")

    parts.append(
        '\n## Output Format\n'
        'Return a JSON object with exactly these keys:\n'
        '{\n'
        '  "draft": "<complete draft text>",\n'
        '  "gaps": [{"location": "...", "description": "...", "suggested_source": "..."}],\n'
        '  "inferences": [{"location": "...", "inference": "...", "confidence": 0.0-1.0, "basis": "..."}]\n'
        '}\n'
        'Return ONLY the JSON object. No preamble, no markdown fences.'
    )

    return '\n'.join(parts)


def _parse_assembly_response(raw: str) -> tuple[str, list[GapFlag], list[InferenceFlag]]:
    cleaned = raw.strip()
    if cleaned.startswith('```'):
        lines = cleaned.split('\n')
        end = -1 if lines[-1].strip() == '```' else len(lines)
        cleaned = '\n'.join(lines[1:end])
        if cleaned.startswith('json'):
            cleaned = cleaned[4:].strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("AI assembly returned non-JSON -- using raw text as draft")
        return cleaned, [], []

    draft = data.get('draft', cleaned)
    gaps = []
    for g in data.get('gaps', []):
        try:
            gaps.append(GapFlag(
                location=g.get('location', ''),
                description=g.get('description', ''),
                suggested_source=g.get('suggested_source', ''),
            ))
        except Exception:
            pass

    inferences = []
    for inf in data.get('inferences', []):
        try:
            inferences.append(InferenceFlag(
                location=inf.get('location', ''),
                inference=inf.get('inference', ''),
                confidence=float(inf.get('confidence', 0.5)),
                basis=inf.get('basis', ''),
            ))
        except Exception:
            pass

    return draft, gaps, inferences


async def assemble_document(
    session_id: UUID,
    tenant_id: str,
    matter_id: Optional[UUID],
    document_type: str,
    practice_area: str,
    template_id: Optional[UUID],
    source_doc_ids: list[str],
    user_instructions: Optional[str],
    created_by: Optional[int],
) -> AssemblyResult:
    """
    Assemble a first-draft document. Writes to drafting_documents and
    ai_contribution_log. Returns AssemblyResult with document_id and content.
    """
    tenant_id = tenant_id.strip()

    api_key = await _get_anthropic_key(tenant_id)

    source_docs = await _load_source_documents(tenant_id, matter_id, source_doc_ids)
    template = await _load_template(tenant_id, template_id) if template_id else None
    matter_context = await _load_matter_context(tenant_id, matter_id) if matter_id else {}
    exemplars = await _load_exemplars(tenant_id, document_type, practice_area, matter_id)

    prompt = _build_assembly_prompt(
        document_type=document_type,
        practice_area=practice_area,
        template=template,
        source_docs=source_docs,
        exemplars=exemplars,
        matter_context=matter_context,
        user_instructions=user_instructions,
    )

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=(
                "You are a precise legal drafting assistant. "
                "Return only valid JSON as instructed. "
                "Never invent case citations, party names, or dates. "
                "When information is unavailable, insert a [GAP] marker."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        logger.error("Anthropic call failed for tenant %s: %s", tenant_id, exc)
        raise

    raw_text = response.content[0].text if response.content else ''
    model_used = response.model or MODEL
    prompt_tokens = response.usage.input_tokens if response.usage else 0
    completion_tokens = response.usage.output_tokens if response.usage else 0

    draft_text, gaps, inferences = _parse_assembly_response(raw_text)

    doc_id = uuid4()
    contrib_id = uuid4()

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO drafting_documents "
                "(id, session_id, tenant_id, version, body_text, "
                " gaps_flagged, inferences, created_at) "
                "VALUES (:id, :sid, :tid, 1, :body, CAST(:gaps AS jsonb), CAST(:inf AS jsonb), NOW())"
            ),
            {
                'id': str(doc_id),
                'sid': str(session_id),
                'tid': tenant_id,
                'body': draft_text,
                'gaps': json.dumps([
                    {'location': g.location, 'description': g.description,
                     'suggested_source': g.suggested_source} for g in gaps
                ]),
                'inf': json.dumps([
                    {'location': i.location, 'inference': i.inference,
                     'confidence': i.confidence, 'basis': i.basis} for i in inferences
                ]),
            },
        )
        await session.execute(
            text(
                "INSERT INTO ai_contribution_log "
                "(id, tenant_id, session_id, matter_id, created_by, "
                " module, prompt_category, model_used, prompt_tokens, "
                " completion_tokens, contribution_summary, attorney_reviewed, created_at) "
                "VALUES (:id, :tid, :sid, :mid, :by, 'drafting', 'draft_generation', "
                " :model, :pt, :ct, :summary, FALSE, NOW())"
            ),
            {
                'id': str(contrib_id), 'tid': tenant_id,
                'sid': str(session_id),
                'mid': str(matter_id) if matter_id else None,
                'by': created_by, 'model': model_used,
                'pt': prompt_tokens, 'ct': completion_tokens,
                'summary': (
                    f"Generated first draft {document_type.replace('_', ' ')} "
                    f"from {len(source_docs)} source doc(s). "
                    f"{len(gaps)} gap(s), {len(inferences)} inference(s) flagged."
                ),
            },
        )
        await session.execute(
            text(
                "UPDATE drafting_sessions SET status='active', updated_at=NOW() "
                "WHERE id=:sid AND trim(tenant_id)=:tid"
            ),
            {'sid': str(session_id), 'tid': tenant_id},
        )
        await session.commit()

    return AssemblyResult(
        session_id=session_id, document_id=doc_id, body_text=draft_text,
        gaps=gaps, inferences=inferences, ai_contribution_id=contrib_id,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        model_used=model_used,
    )


async def get_session_documents(session_id: UUID, tenant_id: str) -> list[dict]:
    """Return all document versions for a session, newest first."""
    tenant_id = tenant_id.strip()
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            text(
                "SELECT id, version, body_text, gaps_flagged, inferences, created_at "
                "FROM drafting_documents "
                "WHERE session_id=:sid AND trim(tenant_id)=:tid "
                "ORDER BY version DESC"
            ),
            {'sid': str(session_id), 'tid': tenant_id},
        )
        return [
            {
                'id': str(r.id), 'version': r.version, 'body_text': r.body_text,
                'gaps_flagged': r.gaps_flagged or [], 'inferences': r.inferences or [],
                'created_at': r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows.fetchall()
        ]


async def get_latest_document(session_id: UUID, tenant_id: str) -> Optional[dict]:
    """Return the most recent document version for a session."""
    docs = await get_session_documents(session_id, tenant_id)
    return docs[0] if docs else None
