"""COMP 6 — Critical Date Memo (Real Estate).
COMP 7 — Title Company Letter Comparison.

Auto-triggered by matter type = real_estate.  Parses contract dates,
generates Word memo, calendars all dates, and compares with title letters.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from modules.dashboard.services._audit_safe import safe_audit
from core.db.session import TenantSession
from core.services import get_ai_service, get_calendar_service, get_storage_service
from modules.dashboard.models import CriticalDateMemo, TitleDateComparison

logger = logging.getLogger(__name__)


# ── COMP 6: Critical Date Memo ────────────────────────────────

def generate_critical_date_memo_job(
    tenant_id: str, matter_id: int, document_id: int,
) -> None:
    """RQ job: generate critical date memo for a real estate contract."""
    import asyncio
    asyncio.run(_generate_memo_async(tenant_id, matter_id, document_id))


async def _generate_memo_async(
    tenant_id: str, matter_id: int, document_id: int,
) -> None:
    from core.db.session import get_tenant_session

    async with get_tenant_session(tenant_id) as ts:
        memo = CriticalDateMemo(
            tenant_id=tenant_id,
            matter_id=matter_id,
            source_document_id=document_id,
            status="processing",
            created_at=datetime.now(timezone.utc),
        )
        ts.session.add(memo)
        await ts.session.flush()

        try:
            # Get document text
            doc_row = await ts.query_all(
                "SELECT extracted_text FROM documents WHERE tenant_id = :tid AND id = :did",
                {"tid": tenant_id, "did": document_id},
            )
            if not doc_row or not doc_row[0].get("extracted_text"):
                memo.status = "failed"
                memo.dates_extracted = {"error": "No text content in source document"}
                await ts.session.flush()
                return

            contract_text = doc_row[0]["extracted_text"]

            # Extract dates via AI
            ai = get_ai_service(tenant_id)
            result = await ai.complete(
                prompt=_DATE_EXTRACTION_PROMPT.format(text=contract_text[:20000]),
                system=_DATE_EXTRACTION_SYSTEM,
                max_tokens=4000,
                temperature=0.1,
            )

            dates = _parse_dates_response(result.content)
            memo.dates_extracted = dates

            # Calculate derived dates (business days vs calendar days)
            for d in dates:
                if d.get("calculation_type") == "business_days" and d.get("anchor_date"):
                    d["calculated_date"] = _add_business_days(
                        d["anchor_date"], d.get("day_count", 0)
                    )

            # Generate Word memo document
            memo_doc_id = await _generate_word_memo(ts, matter_id, dates)
            memo.memo_document_id = memo_doc_id

            # Calendar all dates
            calendar = get_calendar_service(tenant_id)
            matter_row = await ts.query_all(
                "SELECT matter_name FROM matters WHERE tenant_id = :tid AND id = :mid",
                {"tid": tenant_id, "mid": matter_id},
            )
            matter_name = matter_row[0]["matter_name"] if matter_row else f"Matter {matter_id}"

            for d in dates:
                date_val = d.get("explicit_date") or d.get("calculated_date")
                if date_val:
                    await calendar.create_event(
                        tenant_id,
                        title=f"{matter_name} — {d.get('name', 'Deadline')}",
                        date=date_val,
                        matter_id=matter_id,
                        alert_minutes=_get_alert_minutes(d),
                    )

            # Set option period escalating alerts
            for d in dates:
                if "option" in d.get("name", "").lower():
                    for hours_before in [72, 48, 24, 12, 6, 2]:
                        date_val = d.get("explicit_date") or d.get("calculated_date")
                        if date_val:
                            await calendar.create_event(
                                tenant_id,
                                title=f"⚠ OPTION PERIOD — {hours_before}h remaining — {matter_name}",
                                date=date_val,
                                alert_minutes=hours_before * 60,
                                matter_id=matter_id,
                                priority="critical",
                            )

            memo.status = "complete"
            memo.generated_at = datetime.now(timezone.utc)
            await ts.session.flush()

            await safe_audit(
                ts, "CREATE", "critical_date_memos", memo.id,
                new_values={"date_count": len(dates), "status": "complete"},
            )

        except Exception as e:
            logger.exception("Critical date memo generation failed")
            memo.status = "failed"
            memo.dates_extracted = {"error": str(e)[:500]}
            await ts.session.flush()


# ── COMP 7: Title Company Letter Comparison ───────────────────

def compare_title_dates_job(
    tenant_id: str, matter_id: int, title_document_id: int,
) -> None:
    """RQ job: compare title company letter dates with our critical date memo."""
    import asyncio
    asyncio.run(_compare_title_async(tenant_id, matter_id, title_document_id))


async def _compare_title_async(
    tenant_id: str, matter_id: int, title_document_id: int,
) -> None:
    from core.db.session import get_tenant_session

    async with get_tenant_session(tenant_id) as ts:
        # Find the latest memo for this matter
        memo_rows = await ts.query_all(
            "SELECT id, dates_extracted FROM critical_date_memos "
            "WHERE tenant_id = :tid AND matter_id = :mid AND status = 'complete' "
            "ORDER BY generated_at DESC LIMIT 1",
            {"tid": tenant_id, "mid": matter_id},
        )
        if not memo_rows:
            logger.warning("No critical date memo found for matter %s", matter_id)
            return

        memo_id = memo_rows[0]["id"]
        our_dates = memo_rows[0].get("dates_extracted", [])

        comparison = TitleDateComparison(
            tenant_id=tenant_id,
            matter_id=matter_id,
            memo_id=memo_id,
            title_document_id=title_document_id,
            status="processing",
            created_at=datetime.now(timezone.utc),
        )
        ts.session.add(comparison)
        await ts.session.flush()

        try:
            # Extract dates from title company letter
            doc_row = await ts.query_all(
                "SELECT extracted_text FROM documents WHERE tenant_id = :tid AND id = :did",
                {"tid": tenant_id, "did": title_document_id},
            )
            if not doc_row or not doc_row[0].get("extracted_text"):
                comparison.status = "failed"
                await ts.session.flush()
                return

            ai = get_ai_service(tenant_id)
            result = await ai.complete(
                prompt=_TITLE_EXTRACTION_PROMPT.format(text=doc_row[0]["extracted_text"][:15000]),
                system=_DATE_EXTRACTION_SYSTEM,
                max_tokens=3000,
                temperature=0.1,
            )
            title_dates = _parse_dates_response(result.content)

            # Compare
            rows = _compare_date_lists(our_dates, title_dates)
            comparison.comparison_rows = rows
            comparison.status = "complete"
            await ts.session.flush()

            # Create tasks for red discrepancies
            from modules.dashboard.services.task_system import create_task
            for row in rows:
                if row["category"] == "red_diff":
                    await create_task(
                        ts,
                        title=f"Title date discrepancy: {row['description']}",
                        description=(
                            f"Our date: {row.get('our_date', 'N/A')} | "
                            f"Title co: {row.get('title_date', 'N/A')} | "
                            f"Difference: {row.get('day_diff', '?')} days"
                        ),
                        matter_id=matter_id,
                        source="title_discrepancy",
                        priority="critical",
                    )

            await safe_audit(
                ts, "CREATE", "title_date_comparisons", comparison.id,
                new_values={"row_count": len(rows), "red_count": sum(1 for r in rows if r["category"] == "red_diff")},
            )

        except Exception as e:
            logger.exception("Title comparison failed")
            comparison.status = "failed"
            await ts.session.flush()


def _compare_date_lists(
    our_dates: list[dict], title_dates: list[dict],
) -> list[dict[str, Any]]:
    """Compare two sets of extracted dates.

    Categories: match, yellow_diff (1-2 days), red_diff (3+ days),
                only_in_memo (blue), only_in_letter (orange).
    """
    rows: list[dict[str, Any]] = []
    title_matched: set[int] = set()

    for od in our_dates:
        our_name = od.get("name", "").lower()
        our_d = od.get("explicit_date") or od.get("calculated_date")
        best_match = None
        best_score = 0

        for i, td in enumerate(title_dates):
            if i in title_matched:
                continue
            from rapidfuzz import fuzz
            score = fuzz.token_sort_ratio(our_name, td.get("name", "").lower())
            if score > best_score:
                best_score = score
                best_match = (i, td)

        if best_match and best_score >= 60:
            i, td = best_match
            title_matched.add(i)
            title_d = td.get("explicit_date") or td.get("calculated_date")

            category = "match"
            day_diff = 0
            if our_d and title_d:
                try:
                    from datetime import date as dt_date
                    o = datetime.fromisoformat(our_d).date() if isinstance(our_d, str) else our_d
                    t = datetime.fromisoformat(title_d).date() if isinstance(title_d, str) else title_d
                    day_diff = abs((o - t).days)
                    if day_diff == 0:
                        category = "match"
                    elif day_diff <= 2:
                        category = "yellow_diff"
                    else:
                        category = "red_diff"
                except (ValueError, TypeError):
                    category = "yellow_diff"

            rows.append({
                "description": od.get("name", "Unknown"),
                "our_date": our_d,
                "title_date": title_d,
                "category": category,
                "day_diff": day_diff,
            })
        else:
            rows.append({
                "description": od.get("name", "Unknown"),
                "our_date": our_d,
                "title_date": None,
                "category": "only_in_memo",
                "day_diff": None,
            })

    # Unmatched title dates
    for i, td in enumerate(title_dates):
        if i not in title_matched:
            rows.append({
                "description": td.get("name", "Unknown"),
                "our_date": None,
                "title_date": td.get("explicit_date") or td.get("calculated_date"),
                "category": "only_in_letter",
                "day_diff": None,
            })

    return rows


# ── Word Document Generation ──────────────────────────────────

async def _generate_word_memo(
    ts: TenantSession, matter_id: int, dates: list[dict],
) -> int:
    """Generate .docx critical date memo and save to DMS.  Returns document ID."""
    # This would use python-docx in production.  Stub for doc creation:
    storage = get_storage_service(ts.tenant_id)

    matter_row = await ts.query_all(
        "SELECT matter_name FROM matters WHERE tenant_id = :tid AND id = :mid",
        {"tid": ts.tenant_id, "mid": matter_id},
    )
    matter_name = matter_row[0]["matter_name"] if matter_row else f"Matter {matter_id}"

    # Create document record
    doc_id = await ts.insert("documents", {
        "tenant_id": ts.tenant_id,
        "matter_id": matter_id,
        "file_name": f"Critical_Date_Memo_{matter_name.replace(' ', '_')}.docx",
        "doc_type": "critical_date_memo",
        "status": "final",
    })

    # In production: generate .docx with python-docx, upload via StorageService
    # await storage.upload(ts.tenant_id, doc_id, file_bytes)

    return doc_id


# ── Helpers ───────────────────────────────────────────────────

def _parse_dates_response(content: str) -> list[dict]:
    try:
        data = json.loads(content)
        if isinstance(data, list):
            return data
        return data.get("dates", [])
    except json.JSONDecodeError:
        return []


def _add_business_days(start_date: str, days: int) -> str:
    """Add business days to a date string, skipping weekends."""
    from datetime import date as dt_date, timedelta

    try:
        d = datetime.fromisoformat(start_date).date() if isinstance(start_date, str) else start_date
    except (ValueError, TypeError):
        return start_date

    added = 0
    while added < days:
        d += timedelta(days=1)
        if d.weekday() < 5:  # Mon-Fri
            added += 1
    return d.isoformat()


def _get_alert_minutes(date_entry: dict) -> int:
    """Determine alert timing based on deadline type."""
    name = date_entry.get("name", "").lower()
    if "option" in name:
        return 72 * 60  # 72 hours
    if "closing" in name or "earnest" in name:
        return 48 * 60
    return 24 * 60  # Default 24h


# ── AI Prompts ────────────────────────────────────────────────

_DATE_EXTRACTION_SYSTEM = (
    "You are a real estate contract analyst.  Return a JSON array of dates. "
    "Each object: {{name, explicit_date (ISO format or null), "
    "anchor_date (ISO or null), day_count (int or null), "
    "calculation_type (calendar_days|business_days|null), "
    "contract_paragraph, consequence_if_missed, action_required}}"
)

_DATE_EXTRACTION_PROMPT = (
    "Extract every date or time period from this real estate contract. "
    "For calculated dates, identify the anchor date and number of days. "
    "Distinguish calendar days from business days per Texas rules.\n\n"
    "CONTRACT TEXT:\n{text}"
)

_TITLE_EXTRACTION_PROMPT = (
    "Extract every deadline and date from this title company letter. "
    "Return same JSON format as a real estate contract date extraction.\n\n"
    "TITLE COMPANY LETTER:\n{text}"
)
