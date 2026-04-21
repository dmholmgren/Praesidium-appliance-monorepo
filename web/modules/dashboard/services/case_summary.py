"""COMP 2 — AI Case Summary & Causes of Action.

DB pattern: get_session_factory() — matches working eDiscovery pattern.
Panel dispatch signature: async def get_summary_panel_data(tenant_id, matter_id)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from core.db.base import AsyncSessionLocal
from core.services import get_ai_service

logger = logging.getLogger(__name__)


# ── Panel dispatch function ───────────────────────────────────

async def get_summary_panel_data(tenant_id: str, matter_id: str) -> dict:
    """Panel registry dispatch — returns summary + COA data for template."""
    async with AsyncSessionLocal() as session:
        # Latest complete summary
        sum_result = await session.execute(
            text("""
                SELECT * FROM matter_summaries
                WHERE tenant_id = :tid AND matter_id = :mid AND status = 'complete'
                ORDER BY generated_at DESC LIMIT 1
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        summary_row = sum_result.mappings().fetchone()
        summary = dict(summary_row) if summary_row else None

        # Causes of action
        coa_result = await session.execute(
            text("""
                SELECT * FROM causes_of_action
                WHERE tenant_id = :tid AND matter_id = :mid
                ORDER BY count_number ASC
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        coas = [dict(r._mapping) for r in coa_result.fetchall()]

    return {"summary": summary, "causes_of_action": coas}


# ── RQ Job Entry Point (runs on PROC-01) ─────────────────────

def generate_matter_summary_job(tenant_id: str, matter_id: str) -> None:
    import asyncio
    asyncio.run(_generate_summary_async(tenant_id, matter_id))


async def _generate_summary_async(tenant_id: str, matter_id: str) -> None:
    async with AsyncSessionLocal() as session:
        # Get or create summary record
        existing = await session.execute(
            text("""
                SELECT id FROM matter_summaries
                WHERE tenant_id = :tid AND matter_id = :mid
                ORDER BY created_at DESC LIMIT 1
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        row = existing.fetchone()

        if row:
            summary_id = row[0]
            await session.execute(
                text("UPDATE matter_summaries SET status = 'processing' WHERE id = :id"),
                {"id": summary_id},
            )
        else:
            ins = await session.execute(
                text("""
                    INSERT INTO matter_summaries
                        (tenant_id, matter_id, generated_at, status, created_at)
                    VALUES (:tid, :mid, NOW(), 'processing', NOW())
                    RETURNING id
                """),
                {"tid": tenant_id, "mid": matter_id},
            )
            summary_id = ins.fetchone()[0]
        await session.commit()

        try:
            # Gather documents
            docs_result = await session.execute(
                text("""
                    SELECT extracted_text FROM documents
                    WHERE tenant_id = :tid AND matter_id = :mid
                    AND extracted_text IS NOT NULL
                    ORDER BY created_at DESC LIMIT 20
                """),
                {"tid": tenant_id, "mid": matter_id},
            )
            doc_texts = [r[0] for r in docs_result.fetchall() if r[0]]
            doc_count = len(doc_texts)

            if not doc_texts:
                await session.execute(
                    text("""UPDATE matter_summaries SET status = 'failed',
                        error_message = 'No documents found' WHERE id = :id"""),
                    {"id": summary_id},
                )
                await session.commit()
                return

            combined = "\n\n---\n\n".join(doc_texts)[:30000]
            ai = get_ai_service(tenant_id)

            summary_result = await ai.complete(
                prompt=_SUMMARY_PROMPT.format(text=combined),
                system=_SUMMARY_SYSTEM,
                max_tokens=2000,
                temperature=0.2,
            )
            parsed = _parse_summary_response(summary_result.content)

            await session.execute(
                text("""
                    UPDATE matter_summaries SET
                        overview_paragraph = :overview,
                        critical_issues_paragraph = :critical,
                        doc_count_at_generation = :doc_count,
                        generated_at = NOW(),
                        status = 'complete'
                    WHERE id = :id
                """),
                {
                    "overview": parsed.get("overview", ""),
                    "critical": parsed.get("critical_issues", ""),
                    "doc_count": doc_count,
                    "id": summary_id,
                },
            )
            await session.commit()

        except Exception as e:
            logger.exception("Summary generation failed for matter %s", matter_id)
            await session.execute(
                text("""UPDATE matter_summaries SET status = 'failed',
                    error_message = :err WHERE id = :id"""),
                {"err": str(e)[:1000], "id": summary_id},
            )
            await session.commit()


async def override_coa_element(
    tenant_id: str,
    element_id: int,
    new_status: str,
    note: str,
    user_id: int,
) -> dict:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE coa_elements SET
                    attorney_override_status = :status,
                    attorney_override_note = :note,
                    overridden_by = :user_id,
                    overridden_at = NOW(),
                    updated_at = NOW()
                WHERE id = :id AND tenant_id = :tid
            """),
            {"status": new_status, "note": note, "user_id": user_id,
             "id": element_id, "tid": tenant_id},
        )
        await session.commit()
    return {"id": element_id, "attorney_override_status": new_status}


async def get_coa_elements(
    tenant_id: str,
    coa_id: int,
) -> list[dict]:
    """
    Return all elements for a single cause of action, ordered by id.
    Called from GET /dashboard/matter/{matter_id}/coa/{coa_id}/elements.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT
                    e.id,
                    e.element_name,
                    e.status,
                    e.supporting_evidence,
                    e.undermining_evidence,
                    e.discovery_gaps,
                    e.pending_motions,
                    e.attorney_override_status,
                    e.attorney_override_note,
                    e.overridden_at,
                    u.full_name AS overridden_by_name
                FROM coa_elements e
                LEFT JOIN users u ON u.id = e.overridden_by
                WHERE e.cause_of_action_id = :coa_id
                  AND e.tenant_id = :tid
                ORDER BY e.id ASC
            """),
            {"coa_id": coa_id, "tid": tenant_id},
        )
        return [dict(r._mapping) for r in result.fetchall()]


async def generate_status_report(tenant_id: str, matter_id: str) -> str:
    async with AsyncSessionLocal() as session:
        sum_result = await session.execute(
            text("""
                SELECT overview_paragraph, critical_issues_paragraph
                FROM matter_summaries
                WHERE tenant_id = :tid AND matter_id = :mid AND status = 'complete'
                ORDER BY generated_at DESC LIMIT 1
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        summary = sum_result.mappings().fetchone()

        coa_result = await session.execute(
            text("""
                SELECT count_number, title, status FROM causes_of_action
                WHERE tenant_id = :tid AND matter_id = :mid ORDER BY count_number
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        coas = coa_result.fetchall()

    ai = get_ai_service(tenant_id)
    prompt = (
        "Generate a professional client status report letter.\n\n"
        f"OVERVIEW:\n{summary['overview_paragraph'] if summary else 'Not yet generated.'}\n\n"
        f"CRITICAL ISSUES:\n{summary['critical_issues_paragraph'] if summary else 'Not yet generated.'}\n\n"
        "CAUSES OF ACTION:\n"
    )
    for coa in coas:
        prompt += f"- Count {coa[0]}: {coa[1]} (Status: {coa[2]})\n"

    result = await ai.complete(
        prompt=prompt,
        system="You are a legal writing assistant. Generate a professional client status letter.",
        max_tokens=3000,
        temperature=0.3,
    )
    return result.content


async def regenerate_if_needed(tenant_id: str, matter_id: str) -> bool:
    import os
    from rq import Queue
    from redis import Redis

    async with AsyncSessionLocal() as session:
        doc_count_result = await session.execute(
            text("SELECT COUNT(*) FROM documents WHERE tenant_id = :tid AND matter_id = :mid"),
            {"tid": tenant_id, "mid": matter_id},
        )
        current_count = doc_count_result.scalar() or 0

        existing = await session.execute(
            text("""
                SELECT doc_count_at_generation FROM matter_summaries
                WHERE tenant_id = :tid AND matter_id = :mid AND status = 'complete'
                ORDER BY generated_at DESC LIMIT 1
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        row = existing.fetchone()

    if row and current_count <= (row[0] or 0):
        return False

    redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
    q = Queue("proc01", connection=Redis.from_url(redis_url))
    q.enqueue(generate_matter_summary_job, tenant_id, matter_id, job_timeout="10m")
    return True


def _parse_summary_response(content: str) -> dict[str, str]:
    try:
        data = json.loads(content)
        return {
            "overview": data.get("overview", ""),
            "critical_issues": data.get("critical_issues", ""),
        }
    except json.JSONDecodeError:
        parts = content.strip().split("\n\n", 1)
        return {
            "overview": parts[0] if parts else content,
            "critical_issues": parts[1] if len(parts) > 1 else "",
        }


_SUMMARY_SYSTEM = (
    "You are an expert legal analyst. Return JSON with two keys: "
    "'overview' and 'critical_issues'. Each value is a single paragraph."
)
_SUMMARY_PROMPT = (
    "Analyze these case documents and produce a two-paragraph summary.\n\n"
    "Paragraph 1 (overview): Client, adverse parties, court, judge, status, trial date.\n"
    "Paragraph 2 (critical_issues): Key legal/factual issues, risks, urgent items.\n\n"
    "DOCUMENTS:\n{text}"
)
