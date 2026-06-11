"""
jobs/ts_promote.py
Praesidium — Nightly Timeslips Promotion Job

Promotes data from the Timeslips mirror tables (ts_slips, ts_invoices) into
the canonical billing tables (time_entries, invoices) that the billing UI reads.

Architecture:
  ts_slips  ──(promote)──►  time_entries   (billing module reads this)
  ts_invoices ──(promote)──► invoices       (billing module reads this)

Join keys:
  ts_slips.source_client_id → matters.legacy_id  (1:1 — in Timeslips, each "client" is a matter)
  ts_slips.source_tk_id     → ts_timekeepers.ts_tk_id → ts_timekeepers.praesidium_user_id

The job is idempotent: uses INSERT ... ON CONFLICT DO UPDATE keyed on
(tenant_id, source_system, legacy_source_id) so it can be re-run safely.

Scheduling: RQ scheduler, nightly at 2:00 AM CT. Also triggerable from admin UI.

Usage:
    # RQ
    from jobs.ts_promote import run_ts_promote
    queue.enqueue(run_ts_promote, tenant_id="986c0fee-...")

    # CLI
    python -m jobs.ts_promote
    python -m jobs.ts_promote --full    # re-promote everything
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

logger = logging.getLogger("ts_promote")

TENANT_HJMM = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
SOURCE_SYSTEM = "timeslips"
BATCH_SIZE = 2000


async def _promote_slips(tenant_id: str, full: bool = False) -> dict:
    """
    Promote ts_slips → time_entries.

    Join path:
      ts_slips.source_client_id → matters.legacy_id → matters.id (= time_entries.matter_id)
      ts_slips.source_tk_id → ts_timekeepers.ts_tk_id → praesidium_user_id (= time_entries.user_id)

    Only promotes slips that resolve to a matter (orphans stay in ts_slips only).
    Timekeeper resolution is best-effort: falls back to user_id = 0 for unmapped TKs.
    """
    from core.db.base import AsyncSessionLocal

    # Get the high-water mark (last promoted timestamp)
    hwm_filter = ""
    if not full:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT last_promoted_at FROM ts_promotion_hwm
                    WHERE TRIM(tenant_id) = :tid AND entity = 'slips'
                """),
                {"tid": tenant_id.strip()}
            )
            row = result.first()
            if row and row[0]:
                hwm_filter = "AND s.imported_at > :hwm"

    # Count what we'll promote
    async with AsyncSessionLocal() as session:
        count_sql = f"""
            SELECT COUNT(*)
            FROM ts_slips s
            INNER JOIN matters m
                ON m.legacy_id = s.source_client_id
                AND TRIM(m.tenant_id) = TRIM(s.tenant_id)
            WHERE TRIM(s.tenant_id) = :tid
            {hwm_filter}
        """
        params = {"tid": tenant_id.strip()}
        if hwm_filter:
            # Re-read HWM for the param
            hwm_result = await session.execute(
                text("SELECT last_promoted_at FROM ts_promotion_hwm WHERE TRIM(tenant_id) = :tid AND entity = 'slips'"),
                {"tid": tenant_id.strip()}
            )
            hwm_row = hwm_result.first()
            if hwm_row and hwm_row[0]:
                params["hwm"] = hwm_row[0]

        result = await session.execute(text(count_sql), params)
        total = result.scalar() or 0

    if total == 0:
        logger.info("No slips to promote.")
        return {"promoted": 0, "skipped_orphans": 0}

    logger.info(f"Promoting {total} slips → time_entries...")

    # Promote in batches using OFFSET/LIMIT on source_slip_id ordering
    promoted = 0
    offset = 0

    while offset < total:
        async with AsyncSessionLocal() as session:
            promote_sql = f"""
                INSERT INTO time_entries (
                    tenant_id, matter_id, user_id, description, hours, rate,
                    date, billable, status, amount, entry_date,
                    source, source_system, legacy_source_id, legacy_content_hash,
                    timekeeper_name, source_metadata,
                    created_at, updated_at
                )
                SELECT
                    TRIM(s.tenant_id),
                    m.id,
                    COALESCE(tk.praesidium_user_id, 0),
                    COALESCE(s.narrative, ''),
                    COALESCE(s.hours, 0),
                    COALESCE(s.rate, 0),
                    COALESCE(s.slip_date, CURRENT_DATE),
                    CASE WHEN s.trans_type = 1 THEN true ELSE true END,
                    CASE
                        WHEN s.billed = true THEN 'billed'
                        WHEN s.on_hold = true THEN 'hold'
                        ELSE 'draft'
                    END,
                    COALESCE(s.value, 0),
                    s.slip_date,
                    'timeslips',
                    'timeslips',
                    s.source_slip_id,
                    s.content_hash,
                    tk.ts_name,
                    jsonb_build_object(
                        'source_slip_id', s.source_slip_id,
                        'source_client_id', s.source_client_id,
                        'source_tk_id', s.source_tk_id,
                        'trans_type', s.trans_type,
                        'invoice_num', s.invoice_num,
                        'billed_value', s.billed_value,
                        'wip_value', s.wip_value,
                        'activity_id', s.activity_id,
                        'rate_type', s.rate_type,
                        'post_period', s.post_period
                    ),
                    now(),
                    now()
                FROM ts_slips s
                INNER JOIN matters m
                    ON m.legacy_id = s.source_client_id
                    AND TRIM(m.tenant_id) = TRIM(s.tenant_id)
                LEFT JOIN ts_timekeepers tk
                    ON tk.ts_tk_id = s.source_tk_id
                    AND TRIM(tk.tenant_id) = TRIM(s.tenant_id)
                WHERE TRIM(s.tenant_id) = :tid
                {hwm_filter}
                ORDER BY s.source_slip_id
                LIMIT :batch_size OFFSET :offset
                ON CONFLICT (tenant_id, source_system, legacy_source_id)
                    WHERE legacy_source_id IS NOT NULL
                DO UPDATE SET
                    hours         = EXCLUDED.hours,
                    rate          = EXCLUDED.rate,
                    amount        = EXCLUDED.amount,
                    description   = EXCLUDED.description,
                    status        = EXCLUDED.status,
                    legacy_content_hash = EXCLUDED.legacy_content_hash,
                    source_metadata     = EXCLUDED.source_metadata,
                    updated_at    = now()
            """
            batch_params = {**params, "batch_size": BATCH_SIZE, "offset": offset}
            result = await session.execute(text(promote_sql), batch_params)
            batch_count = result.rowcount
            await session.commit()

        promoted += batch_count
        offset += BATCH_SIZE
        if offset % 10000 == 0:
            logger.info(f"  ...promoted {promoted}/{total}")

    # Update HWM
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO ts_promotion_hwm (tenant_id, entity, last_promoted_at, rows_promoted, updated_at)
                VALUES (:tid, 'slips', now(), :rows, now())
                ON CONFLICT (tenant_id, entity)
                DO UPDATE SET
                    last_promoted_at = now(),
                    rows_promoted = ts_promotion_hwm.rows_promoted + :rows,
                    updated_at = now()
            """),
            {"tid": tenant_id, "rows": promoted}
        )
        await session.commit()

    # Count orphans (slips that don't resolve to a matter)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT COUNT(*)
                FROM ts_slips s
                LEFT JOIN matters m
                    ON m.legacy_id = s.source_client_id
                    AND TRIM(m.tenant_id) = TRIM(s.tenant_id)
                WHERE TRIM(s.tenant_id) = :tid
                    AND m.id IS NULL
            """),
            {"tid": tenant_id.strip()}
        )
        orphans = result.scalar() or 0

    logger.info(f"Slips promotion complete: {promoted} promoted, {orphans} orphans (unmapped to matter)")
    return {"promoted": promoted, "skipped_orphans": orphans}


async def _promote_invoices(tenant_id: str, full: bool = False) -> dict:
    """
    Promote ts_invoices → invoices.

    ts_invoices don't carry a client_id directly — they link through ts_slips.
    We resolve matter_id by finding the most common source_client_id on slips
    attached to each invoice, then joining to matters.legacy_id.
    """
    from core.db.base import AsyncSessionLocal

    hwm_filter = ""
    params = {"tid": tenant_id.strip()}

    if not full:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT last_promoted_at FROM ts_promotion_hwm
                    WHERE TRIM(tenant_id) = :tid AND entity = 'invoices'
                """),
                {"tid": tenant_id.strip()}
            )
            row = result.first()
            if row and row[0]:
                hwm_filter = "AND i.updated_at > :hwm"
                params["hwm"] = row[0]

    async with AsyncSessionLocal() as session:
        promote_sql = f"""
            INSERT INTO invoices (
                tenant_id, matter_id, client_id, invoice_number, status,
                charge_fees, charge_costs, total_amount, balance_due,
                slip_start, slip_end,
                source_system, legacy_source_id, legacy_invoice_num,
                created_at, updated_at
            )
            SELECT
                TRIM(i.tenant_id),
                inv_matter.matter_id,
                inv_matter.client_id,
                CAST(i.invoice_num AS VARCHAR),
                CASE
                    WHEN i.paid_in_full = true THEN 'paid'
                    WHEN i.invoice_status = 0 THEN 'draft'
                    ELSE 'sent'
                END,
                COALESCE(i.charge_fees, 0),
                COALESCE(i.charge_costs, 0),
                COALESCE(i.charge_fees, 0) + COALESCE(i.charge_costs, 0),
                CASE WHEN i.paid_in_full = true THEN 0
                     ELSE COALESCE(i.net_due, 0) END,
                CAST(i.slip_start AS DATE),
                CAST(i.slip_end AS DATE),
                'timeslips',
                i.source_invoice_id,
                i.invoice_num,
                now(),
                now()
            FROM ts_invoices i
            LEFT JOIN LATERAL (
                SELECT m.id AS matter_id, m.client_id
                FROM ts_slips s
                INNER JOIN matters m
                    ON m.legacy_id = s.source_client_id
                    AND TRIM(m.tenant_id) = TRIM(s.tenant_id)
                WHERE TRIM(s.tenant_id) = :tid
                    AND s.invoice_num = i.invoice_num
                GROUP BY m.id, m.client_id
                ORDER BY COUNT(*) DESC
                LIMIT 1
            ) inv_matter ON true
            WHERE TRIM(i.tenant_id) = :tid
            {hwm_filter}
            ON CONFLICT (tenant_id, source_system, legacy_source_id)
                WHERE legacy_source_id IS NOT NULL
            DO UPDATE SET
                status       = EXCLUDED.status,
                charge_fees  = EXCLUDED.charge_fees,
                charge_costs = EXCLUDED.charge_costs,
                total_amount = EXCLUDED.total_amount,
                balance_due  = EXCLUDED.balance_due,
                updated_at   = now()
        """
        result = await session.execute(text(promote_sql), params)
        promoted = result.rowcount
        await session.commit()

    # Update HWM
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO ts_promotion_hwm (tenant_id, entity, last_promoted_at, rows_promoted, updated_at)
                VALUES (:tid, 'invoices', now(), :rows, now())
                ON CONFLICT (tenant_id, entity)
                DO UPDATE SET
                    last_promoted_at = now(),
                    rows_promoted = ts_promotion_hwm.rows_promoted + :rows,
                    updated_at = now()
            """),
            {"tid": tenant_id, "rows": promoted}
        )
        await session.commit()

    logger.info(f"Invoices promotion complete: {promoted} promoted")
    return {"promoted": promoted}


async def _run_async(tenant_id: str, full: bool = False) -> dict:
    """Async entry point — promotes slips then invoices."""
    logger.info("=" * 55)
    logger.info(f"Timeslips promotion — tenant={tenant_id}, full={full}")
    logger.info("=" * 55)

    slips_result = await _promote_slips(tenant_id, full=full)
    invoices_result = await _promote_invoices(tenant_id, full=full)

    result = {
        "slips_promoted": slips_result["promoted"],
        "slips_orphans": slips_result["skipped_orphans"],
        "invoices_promoted": invoices_result["promoted"],
    }
    logger.info(f"Promotion complete: {result}")
    return result


def run_ts_promote(tenant_id: str = TENANT_HJMM, full: bool = False) -> dict:
    """
    Sync entry point for RQ / CLI.

    Scheduled nightly at 2:00 AM CT via RQ scheduler.
    Also callable from admin UI or MCP tool.

    Args:
        tenant_id: Tenant UUID
        full: If True, re-promote all slips (ignores HWM). Default: incremental.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_run_async(tenant_id, full=full))
    finally:
        loop.close()


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="Praesidium — Timeslips Promotion Job")
    parser.add_argument("--full", action="store_true", help="Re-promote all (ignore HWM)")
    parser.add_argument("--tenant", default=TENANT_HJMM)
    args = parser.parse_args()

    result = run_ts_promote(tenant_id=args.tenant, full=args.full)
    print(json.dumps(result, indent=2))
