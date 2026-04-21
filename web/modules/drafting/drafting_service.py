"""
modules/drafting/drafting_service.py

Session orchestrator for Module 4 — Document Generation & Assembly.

Responsibilities:
    - Create and manage drafting_sessions
    - Coordinate assembly_service -> sanity_service pipeline
    - Expose session state for route layer
    - Handle session dismissal and status transitions

This is the single entry point the router calls. It owns drafting_sessions
CRUD. All other services receive session_id as a parameter.

Called by:
    drafting_router.py (route layer only)

Never called by:
    Templates, background jobs, or other services
"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.drafting.assembly_service import assemble_document, AssemblyResult
from modules.drafting.sanity_service import run_sanity_check, SanityRunResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

async def create_session(
    tenant_id: str,
    matter_id: Optional[UUID],
    document_type: str,
    practice_area: str,
    title: Optional[str],
    template_id: Optional[UUID],
    source_doc_ids: list[str],
    assembly_prompt: Optional[str],
    created_by: Optional[int],
) -> dict:
    """
    Create a new drafting session and return its full record.
    Does NOT run assembly or sanity check — those are separate calls.
    """
    tenant_id = tenant_id.strip()
    session_id = uuid4()

    import json
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO drafting_sessions "
                "(id, tenant_id, matter_id, created_by, title, "
                " document_type, practice_area, template_id, "
                " status, assembly_prompt, source_doc_ids, "
                " created_at, updated_at) "
                "VALUES "
                "(:id, :tid, :mid, :by, :title, "
                " :dtype, :parea, :tmpl, "
                " 'active', :prompt, CAST(:docs AS jsonb), "
                " NOW(), NOW())"
            ),
            {
                'id': str(session_id),
                'tid': tenant_id,
                'mid': str(matter_id) if matter_id else None,
                'by': created_by,
                'title': title or f"Draft {document_type.replace('_', ' ').title()}",
                'dtype': document_type,
                'parea': practice_area,
                'tmpl': str(template_id) if template_id else None,
                'prompt': assembly_prompt,
                'docs': json.dumps(source_doc_ids),
            },
        )
        await session.commit()

    return await get_session(session_id, tenant_id)


async def get_session(session_id: UUID, tenant_id: str) -> Optional[dict]:
    """Return full session record including latest document and sanity results."""
    tenant_id = tenant_id.strip()
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            text(
                "SELECT s.id, s.tenant_id, s.matter_id, s.created_by, "
                "       s.title, s.document_type, s.practice_area, "
                "       s.template_id, s.status, s.assembly_prompt, "
                "       s.source_doc_ids, s.created_at, s.updated_at, "
                "       m.matter_name as matter_name, m.matter_number "
                "FROM drafting_sessions s "
                "LEFT JOIN matters m ON s.matter_id = m.id "
                "WHERE s.id = :sid AND trim(s.tenant_id) = :tid"
            ),
            {'sid': str(session_id), 'tid': tenant_id},
        )
        r = row.fetchone()
        if not r:
            return None

        return {
            'id': str(r.id),
            'tenant_id': r.tenant_id.strip(),
            'matter_id': str(r.matter_id) if r.matter_id else None,
            'matter_name': r.matter_name,
            'matter_number': r.matter_number,
            'created_by': r.created_by,
            'title': r.title,
            'document_type': r.document_type,
            'practice_area': r.practice_area,
            'template_id': str(r.template_id) if r.template_id else None,
            'status': r.status,
            'assembly_prompt': r.assembly_prompt,
            'source_doc_ids': r.source_doc_ids or [],
            'created_at': r.created_at.isoformat() if r.created_at else None,
            'updated_at': r.updated_at.isoformat() if r.updated_at else None,
        }


async def list_sessions(
    tenant_id: str,
    matter_id: Optional[UUID] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """List drafting sessions for a tenant, optionally filtered."""
    tenant_id = tenant_id.strip()

    filters = ["trim(s.tenant_id) = :tid", "s.status != 'dismissed'"]
    params: dict = {'tid': tenant_id, 'limit': limit, 'offset': offset}

    if matter_id:
        filters.append("s.matter_id = :mid")
        params['mid'] = str(matter_id)

    if status:
        filters.append("s.status = :status")
        params['status'] = status

    where = ' AND '.join(filters)

    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            text(
                f"SELECT s.id, s.title, s.document_type, s.practice_area, "
                f"       s.status, s.created_at, s.updated_at, "
                f"       m.matter_name as matter_name "
                f"FROM drafting_sessions s "
                f"LEFT JOIN matters m ON s.matter_id = m.id "
                f"WHERE {where} "
                f"ORDER BY s.updated_at DESC "
                f"LIMIT :limit OFFSET :offset"
            ),
            params,
        )
        return [
            {
                'id': str(r.id),
                'title': r.title,
                'document_type': r.document_type,
                'practice_area': r.practice_area,
                'status': r.status,
                'matter_name': r.matter_name,
                'created_at': r.created_at.isoformat() if r.created_at else None,
                'updated_at': r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows.fetchall()
        ]


async def dismiss_session(session_id: UUID, tenant_id: str) -> bool:
    """Mark a session as dismissed. Returns True if updated."""
    tenant_id = tenant_id.strip()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "UPDATE drafting_sessions "
                "SET status='dismissed', updated_at=NOW() "
                "WHERE id=:sid AND trim(tenant_id)=:tid "
                "  AND status != 'dismissed'"
            ),
            {'sid': str(session_id), 'tid': tenant_id},
        )
        await session.commit()
        return result.rowcount > 0


# ---------------------------------------------------------------------------
# Assembly pipeline
# ---------------------------------------------------------------------------

async def run_assembly(
    session_id: UUID,
    tenant_id: str,
    created_by: Optional[int],
) -> AssemblyResult:
    """
    Run document assembly for an existing session.
    Loads session parameters from DB, calls assembly_service.assemble_document.
    Returns AssemblyResult with document_id and draft content.
    """
    tenant_id = tenant_id.strip()
    sess = await get_session(session_id, tenant_id)
    if not sess:
        raise ValueError(f"Session {session_id} not found for tenant {tenant_id!r}")

    matter_id = UUID(sess['matter_id']) if sess['matter_id'] else None
    template_id = UUID(sess['template_id']) if sess['template_id'] else None
    source_doc_ids = sess.get('source_doc_ids') or []

    return await assemble_document(
        session_id=session_id,
        tenant_id=tenant_id,
        matter_id=matter_id,
        document_type=sess['document_type'],
        practice_area=sess['practice_area'],
        template_id=template_id,
        source_doc_ids=source_doc_ids,
        user_instructions=sess.get('assembly_prompt'),
        created_by=created_by,
    )


# ---------------------------------------------------------------------------
# Sanity check pipeline
# ---------------------------------------------------------------------------

async def run_sanity(
    session_id: UUID,
    tenant_id: str,
) -> SanityRunResult:
    """
    Run the 9-layer sanity check against the latest draft for a session.
    Loads draft text and matter context from DB, calls sanity_service.

    Raises ValueError if no draft document exists for the session yet.
    """
    tenant_id = tenant_id.strip()

    # Load latest draft
    async with AsyncSessionLocal() as session:
        doc_row = await session.execute(
            text(
                "SELECT body_text FROM drafting_documents "
                "WHERE session_id=:sid AND trim(tenant_id)=:tid "
                "ORDER BY version DESC LIMIT 1"
            ),
            {'sid': str(session_id), 'tid': tenant_id},
        )
        doc = doc_row.fetchone()
        if not doc:
            raise ValueError(
                f"No draft document found for session {session_id}. "
                "Run assembly first."
            )
        draft_text = doc.body_text or ''

    # Load session + matter context
    sess = await get_session(session_id, tenant_id)
    matter_id = UUID(sess['matter_id']) if sess and sess['matter_id'] else None
    practice_area = sess['practice_area'] if sess else 'general'

    matter_context = {}
    if matter_id:
        async with AsyncSessionLocal() as session:
            row = await session.execute(
                text(
                    "SELECT m.matter_name as name, m.matter_number, m.practice_area, "
                    "       c.name as client_name "
                    "FROM matters m "
                    "LEFT JOIN clients c ON m.client_id = c.id "
                    "WHERE m.id=:mid AND trim(m.tenant_id)=:tid"
                ),
                {'mid': str(matter_id), 'tid': tenant_id},
            )
            r = row.fetchone()
            if r:
                matter_context = {
                    'matter_name': r.name,
                    'matter_number': r.matter_number,
                    'client_name': r.client_name,
                    'practice_area': r.practice_area,
                }

    return await run_sanity_check(
        session_id=session_id,
        tenant_id=tenant_id,
        matter_id=matter_id,
        draft_text=draft_text,
        practice_area=practice_area,
        matter_context=matter_context,
    )


# ---------------------------------------------------------------------------
# Session summary — used by the panel UI
# ---------------------------------------------------------------------------

async def get_session_summary(session_id: UUID, tenant_id: str) -> Optional[dict]:
    """
    Return a complete session summary including:
    - Session metadata
    - Latest document (body_text, gaps, inferences)
    - Sanity check results (all layers)
    - Bates insertion log (pending items)
    - AI contribution log entries
    """
    from modules.drafting.assembly_service import get_latest_document
    from modules.drafting.sanity_service import get_sanity_results
    from modules.drafting.bates_service import get_bates_insertions

    tenant_id = tenant_id.strip()

    sess = await get_session(session_id, tenant_id)
    if not sess:
        return None

    latest_doc = await get_latest_document(session_id, tenant_id)
    sanity_results = await get_sanity_results(session_id, tenant_id)
    bates_items = await get_bates_insertions(session_id, tenant_id)

    # AI contribution summary for this session
    async with AsyncSessionLocal() as session:
        contrib_rows = await session.execute(
            text(
                "SELECT model_used, prompt_tokens, completion_tokens, "
                "       contribution_summary, attorney_reviewed, created_at "
                "FROM ai_contribution_log "
                "WHERE session_id=:sid AND trim(tenant_id)=:tid "
                "ORDER BY created_at"
            ),
            {'sid': str(session_id), 'tid': tenant_id},
        )
        contributions = [
            {
                'model_used': r.model_used,
                'prompt_tokens': r.prompt_tokens,
                'completion_tokens': r.completion_tokens,
                'contribution_summary': r.contribution_summary,
                'attorney_reviewed': r.attorney_reviewed,
                'created_at': r.created_at.isoformat() if r.created_at else None,
            }
            for r in contrib_rows.fetchall()
        ]

    # Compute sanity overall
    critical = sum(1 for s in sanity_results if s['result'] == 'critical')
    warnings = sum(1 for s in sanity_results if s['result'] == 'warning')
    if critical:
        sanity_overall = 'critical'
    elif warnings:
        sanity_overall = 'warning'
    elif sanity_results:
        sanity_overall = 'pass'
    else:
        sanity_overall = 'not_run'

    # Pending bates items
    pending_bates = [b for b in bates_items if b['disposition'] == 'pending']
    critical_bates = [
        b for b in bates_items
        if b['flag_clawback'] or b['flag_privilege_log']
    ]

    return {
        'session': sess,
        'latest_document': latest_doc,
        'sanity': {
            'overall': sanity_overall,
            'critical_count': critical,
            'warning_count': warnings,
            'layers': sanity_results,
        },
        'bates': {
            'total': len(bates_items),
            'pending': len(pending_bates),
            'critical_flags': len(critical_bates),
            'items': bates_items,
        },
        'ai_contributions': contributions,
    }
