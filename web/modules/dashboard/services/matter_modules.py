"""COMP 9  — Client Communication Log.
COMP 10 — Engagement Letter Tracking.
COMP 11 — Settlement, Expert Witness, Mediation Modules.

DB pattern: get_session_factory() — matches working eDiscovery pattern.
Panel dispatch signature: async def func(tenant_id: str, matter_id: str)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)


# ── Panel dispatch functions (read-only, called by generic panel route) ───────

async def get_communications(tenant_id: str, matter_id: str) -> list[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT cl.*, u.full_name as logged_by_name
                FROM communication_log cl
                LEFT JOIN users u ON cl.logged_by = u.id AND cl.tenant_id = u.tenant_id
                WHERE cl.tenant_id = :tid AND cl.matter_id = :mid
                ORDER BY cl.occurred_at DESC LIMIT 50
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        return [dict(r._mapping) for r in result.fetchall()]


async def get_settlement_history(tenant_id: str, matter_id: str) -> list[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT * FROM settlements
                WHERE tenant_id = :tid AND matter_id = :mid
                ORDER BY offered_at ASC
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        return [dict(r._mapping) for r in result.fetchall()]


async def get_experts_for_matter(tenant_id: str, matter_id: str) -> list[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT * FROM expert_witnesses
                WHERE tenant_id = :tid AND matter_id = :mid
                ORDER BY created_at ASC
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        return [dict(r._mapping) for r in result.fetchall()]


async def get_mediations_for_matter(tenant_id: str, matter_id: str) -> list[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT * FROM mediations
                WHERE tenant_id = :tid AND matter_id = :mid
                ORDER BY scheduled_date ASC
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        return [dict(r._mapping) for r in result.fetchall()]


async def get_engagement_letters(tenant_id: str, matter_id: str) -> list[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT * FROM engagement_letters
                WHERE tenant_id = :tid AND matter_id = :mid
                ORDER BY created_at DESC
            """),
            {"tid": tenant_id, "mid": matter_id},
        )
        return [dict(r._mapping) for r in result.fetchall()]


# ── Write operations (called from mutation routes) ────────────────────────────

async def log_communication(
    tenant_id: str,
    matter_id: str,
    channel: str,
    direction: str,
    *,
    subject: str | None = None,
    summary: str | None = None,
    logged_by: int | None = None,
) -> dict:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO communication_log
                    (tenant_id, matter_id, channel, direction,
                     subject, summary, occurred_at, logged_by, created_at)
                VALUES
                    (:tid, :mid, :channel, :direction,
                     :subject, :summary, NOW(), :logged_by, NOW())
                RETURNING id
            """),
            {
                "tid": tenant_id, "mid": matter_id,
                "channel": channel, "direction": direction,
                "subject": subject, "summary": summary,
                "logged_by": logged_by,
            },
        )
        await session.commit()
        row = result.fetchone()
        return {"id": row[0]}


async def record_settlement_event(
    tenant_id: str,
    matter_id: str,
    offer_type: str,
    *,
    amount: Decimal | None = None,
    offered_by: str | None = None,
    terms_summary: str | None = None,
    user_id: int | None = None,
) -> dict:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO settlements
                    (tenant_id, matter_id, offer_type, amount,
                     offered_by, terms_summary, offered_at, created_at)
                VALUES
                    (:tid, :mid, :offer_type, :amount,
                     :offered_by, :terms_summary, NOW(), NOW())
                RETURNING id
            """),
            {
                "tid": tenant_id, "mid": matter_id, "offer_type": offer_type,
                "amount": amount, "offered_by": offered_by,
                "terms_summary": terms_summary,
            },
        )
        await session.commit()
        row = result.fetchone()
        return {"id": row[0]}


async def add_expert_witness(
    tenant_id: str,
    matter_id: str,
    name: str,
    *,
    specialty: str | None = None,
    credentials: str | None = None,
    retained_by: str = "us",
    hourly_rate: Decimal | None = None,
    user_id: int | None = None,
) -> dict:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO expert_witnesses
                    (tenant_id, matter_id, name, specialty, credentials,
                     retained_by, hourly_rate, status, created_at, updated_at)
                VALUES
                    (:tid, :mid, :name, :specialty, :credentials,
                     :retained_by, :hourly_rate, 'identified', NOW(), NOW())
                RETURNING id
            """),
            {
                "tid": tenant_id, "mid": matter_id, "name": name,
                "specialty": specialty, "credentials": credentials,
                "retained_by": retained_by, "hourly_rate": hourly_rate,
            },
        )
        await session.commit()
        row = result.fetchone()
        return {"id": row[0]}


async def create_mediation(
    tenant_id: str,
    matter_id: str,
    *,
    mediator_name: str | None = None,
    scheduled_date: datetime | None = None,
    location: str | None = None,
    user_id: int | None = None,
) -> dict:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO mediations
                    (tenant_id, matter_id, mediator_name, scheduled_date,
                     location, outcome, created_at, updated_at)
                VALUES
                    (:tid, :mid, :mediator_name, :scheduled_date,
                     :location, 'pending', NOW(), NOW())
                RETURNING id
            """),
            {
                "tid": tenant_id, "mid": matter_id,
                "mediator_name": mediator_name,
                "scheduled_date": scheduled_date,
                "location": location,
            },
        )
        await session.commit()
        row = result.fetchone()
        return {"id": row[0]}


async def create_engagement_letter(
    tenant_id: str,
    matter_id: str,
    *,
    document_id: str | None = None,
    user_id: int | None = None,
) -> dict:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO engagement_letters
                    (tenant_id, matter_id, document_id, status, created_at, updated_at)
                VALUES
                    (:tid, :mid, :document_id, 'draft', NOW(), NOW())
                RETURNING id
            """),
            {"tid": tenant_id, "mid": matter_id, "document_id": document_id},
        )
        await session.commit()
        row = result.fetchone()
        return {"id": row[0]}


async def get_unsigned_engagement_letters(tenant_id: str) -> list[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT el.*, m.matter_name, c.client_name
                FROM engagement_letters el
                JOIN matters m ON el.matter_id = m.id AND el.tenant_id = m.tenant_id
                LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
                WHERE el.tenant_id = :tid AND el.status NOT IN ('signed', 'declined')
                ORDER BY el.created_at ASC
            """),
            {"tid": tenant_id},
        )
        return [dict(r._mapping) for r in result.fetchall()]
