"""COMP 8 — Automated Task System + Commitment Detection.

DB pattern: get_session_factory() — matches working eDiscovery pattern.
Panel dispatch signature: async def get_tasks_for_matter(tenant_id, matter_id)
Fixed: GROUP_CONCAT → STRING_AGG (PostgreSQL)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from core.db.base import AsyncSessionLocal
from core.services import get_ai_service

logger = logging.getLogger(__name__)


# ── Panel dispatch function ───────────────────────────────────

async def get_tasks_for_matter(tenant_id: str, matter_id: str) -> list[dict]:
    """Panel registry dispatch — tasks list with assignees."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT t.*,
                       STRING_AGG(u.full_name, ', ') as assignees
                FROM tasks t
                LEFT JOIN task_assignments ta
                    ON t.id = ta.task_id AND t.tenant_id = ta.tenant_id
                LEFT JOIN users u
                    ON ta.user_id = u.id AND ta.tenant_id = u.tenant_id
                WHERE t.tenant_id = :tid AND t.matter_id = :mid
                GROUP BY t.id
                ORDER BY t.due_date ASC NULLS LAST, t.priority ASC
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        return [dict(r._mapping) for r in result.fetchall()]


# ── Write operations ──────────────────────────────────────────

async def create_task(
    tenant_id: str,
    title: str,
    *,
    description: str | None = None,
    matter_id: str | None = None,
    source: str = "manual",
    priority: str = "medium",
    due_date: datetime | None = None,
    created_by: int | None = None,
    assigned_to: list[int] | None = None,
) -> dict:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO tasks
                    (tenant_id, matter_id, title, description, source, priority,
                     status, due_date, created_by, created_at, updated_at)
                VALUES
                    (:tid, :mid, :title, :description, :source, :priority,
                     'open', :due_date, :created_by, NOW(), NOW())
                RETURNING id
            """),
            {
                "tid": tenant_id, "mid": matter_id, "title": title,
                "description": description, "source": source,
                "priority": priority, "due_date": due_date,
                "created_by": created_by,
            },
        )
        task_id = result.fetchone()[0]

        if assigned_to:
            for uid in assigned_to:
                await session.execute(
                    text("""
                        INSERT INTO task_assignments
                            (tenant_id, task_id, user_id, assigned_by, assigned_at)
                        VALUES (:tid, :task_id, :uid, :assigned_by, NOW())
                    """),
                    {"tid": tenant_id, "task_id": task_id,
                     "uid": uid, "assigned_by": created_by},
                )

        await session.commit()
    return {"id": task_id, "status": "open"}


async def update_task_status(
    tenant_id: str,
    task_id: int,
    new_status: str,
    user_id: int,
) -> dict:
    async with AsyncSessionLocal() as session:
        completed_at = datetime.now(timezone.utc) if new_status == "complete" else None
        await session.execute(
            text("""
                UPDATE tasks SET
                    status = :status,
                    updated_at = NOW(),
                    completed_at = :completed_at
                WHERE id = :id AND tenant_id = :tid
            """),
            {"status": new_status, "completed_at": completed_at,
             "id": task_id, "tid": tenant_id},
        )
        await session.commit()
    return {"id": task_id, "status": new_status}


async def get_tasks_for_user(tenant_id: str, user_id: int) -> list[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT t.*, m.matter_name FROM tasks t
                JOIN task_assignments ta ON t.id = ta.task_id AND t.tenant_id = ta.tenant_id
                LEFT JOIN matters m ON t.matter_id = m.id AND t.tenant_id = m.tenant_id
                WHERE t.tenant_id = :tid AND ta.user_id = :uid
                AND t.status NOT IN ('complete', 'cancelled')
                ORDER BY t.due_date ASC NULLS LAST, t.priority ASC
            """),
            {"tid": tenant_id, "uid": user_id},
        )
        return [dict(r._mapping) for r in result.fetchall()]


# ── Commitment Detection (RQ Job) ─────────────────────────────

def detect_commitments_job(tenant_id: str) -> None:
    import asyncio
    asyncio.run(_detect_commitments_async(tenant_id))


async def _detect_commitments_async(tenant_id: str) -> None:
    email_svc = get_ai_service(tenant_id)  # placeholder
    ai = get_ai_service(tenant_id)
    # Implementation deferred — shell preserved for RQ registration


def _parse_commitments(content: str) -> list[dict]:
    try:
        data = json.loads(content)
        return data if isinstance(data, list) else data.get("commitments", [])
    except json.JSONDecodeError:
        return []


def _parse_deadline(deadline_str: str | None) -> datetime | None:
    if not deadline_str:
        return None
    try:
        return datetime.fromisoformat(deadline_str)
    except (ValueError, TypeError):
        return None


_COMMITMENT_SYSTEM = (
    "You identify action items and deadlines in legal communications. "
    "Return JSON array: [{action, description, deadline (ISO or null), "
    "priority (critical|high|medium|low)}]."
)
_COMMITMENT_PROMPT = (
    "Identify commitments, action items, or deadlines in this communication.\n\n"
    "COMMUNICATION:\n{text}"
)
