"""
modules/billing/services/bill_run_service.py
Core bill run engine — generates prebills from time_entries,
tracks adjustments (N/C, W/O), finalizes invoices with adjustments netted out.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger(__name__)


async def create_bill_run(
    tenant_id: str, period_start: date, period_end: date,
    created_by_id: int, run_name: Optional[str] = None, notes: Optional[str] = None,
) -> dict:
    tid = tenant_id.strip()
    if not run_name:
        run_name = f"Bill Run \u2014 {period_start.strftime('%B %Y')}"

    async with AsyncSessionLocal() as db:
        row = (await db.execute(text("""
            INSERT INTO bill_runs (tenant_id, run_name, billing_period_start,
                billing_period_end, status, created_by_id, notes)
            VALUES (:tid, :name, :start, :end, 'draft', :by, :notes) RETURNING id
        """), {"tid": tid, "name": run_name, "start": period_start,
               "end": period_end, "by": created_by_id, "notes": notes})).fetchone()
        bill_run_id = row[0]
        total_matters = 0
        total_fees = Decimal("0")

        matters_from_te = (await db.execute(text("""
            SELECT te.matter_id, m.matter_name, m.matter_number,
                   m.client_id, c.client_name,
                   COUNT(te.id) AS slip_count, SUM(te.hours) AS total_hours,
                   SUM(COALESCE(te.amount, te.hours * te.rate)) AS fee_total
            FROM time_entries te
            JOIN matters m ON m.id = te.matter_id
            LEFT JOIN clients c ON c.id = m.client_id
            WHERE TRIM(te.tenant_id) = :tid AND te.status = 'draft'
              AND te.billable = true AND te.invoice_id IS NULL
              AND te.date BETWEEN :start AND :end
            GROUP BY te.matter_id, m.matter_name, m.matter_number, m.client_id, c.client_name
            ORDER BY c.client_name, m.matter_number
        """), {"tid": tid, "start": period_start, "end": period_end})).fetchall()

        for m in matters_from_te:
            fee = Decimal(str(m.fee_total or 0))
            prior = (await db.execute(text("""
                SELECT COALESCE(SUM(balance_due), 0) FROM invoices
                WHERE TRIM(tenant_id) = :tid AND matter_id = CAST(:mid AS uuid)
                  AND status NOT IN ('paid', 'void')
            """), {"tid": tid, "mid": str(m.matter_id)})).scalar() or 0

            await db.execute(text("""
                INSERT INTO bill_run_matters (tenant_id, bill_run_id, matter_id, client_id,
                    client_name, matter_name, matter_number, status, fee_total, prior_balance, net_total)
                VALUES (:tid, :brid, CAST(:mid AS uuid), CAST(:cid AS uuid),
                    :cname, :mname, :mnum, 'pending', :fees, :prior, :net)
            """), {"tid": tid, "brid": bill_run_id, "mid": str(m.matter_id),
                   "cid": str(m.client_id) if m.client_id else None,
                   "cname": m.client_name, "mname": m.matter_name,
                   "mnum": m.matter_number, "fees": fee, "prior": prior,
                   "net": fee + Decimal(str(prior))})
            total_matters += 1
            total_fees += fee

        await db.execute(text("""
            UPDATE bill_runs SET total_matters = :tm, total_fees = :tf, status = 'reviewing'
            WHERE id = :brid AND tenant_id = :tid
        """), {"tm": total_matters, "tf": total_fees, "brid": bill_run_id, "tid": tid})
        await db.commit()

    return {"id": bill_run_id, "run_name": run_name, "status": "reviewing",
            "total_matters": total_matters, "total_fees": float(total_fees),
            "period_start": period_start.isoformat(), "period_end": period_end.isoformat()}


async def get_bill_run(tenant_id: str, bill_run_id: int) -> Optional[dict]:
    tid = tenant_id.strip()
    async with AsyncSessionLocal() as db:
        run = (await db.execute(text("""
            SELECT br.*, u.full_name AS created_by_name FROM bill_runs br
            JOIN users u ON u.id = br.created_by_id WHERE br.id = :brid AND br.tenant_id = :tid
        """), {"brid": bill_run_id, "tid": tid})).fetchone()
        if not run: return None
        matters = (await db.execute(text("""
            SELECT brm.* FROM bill_run_matters brm
            WHERE brm.bill_run_id = :brid AND brm.tenant_id = :tid
            ORDER BY brm.client_name, brm.matter_number
        """), {"brid": bill_run_id, "tid": tid})).fetchall()
    return {"bill_run": dict(run._mapping), "matters": [dict(m._mapping) for m in matters]}


async def get_prebill_slips(tenant_id: str, bill_run_matter_id: int) -> dict:
    tid = tenant_id.strip()
    async with AsyncSessionLocal() as db:
        brm = (await db.execute(text("""
            SELECT * FROM bill_run_matters WHERE id = :id AND tenant_id = :tid
        """), {"id": bill_run_matter_id, "tid": tid})).fetchone()
        if not brm: return {"error": "Not found"}

        slips = []
        brm_dict = dict(brm._mapping)
        br = (await db.execute(text("""
            SELECT billing_period_start, billing_period_end FROM bill_runs WHERE id = :brid
        """), {"brid": brm.bill_run_id})).fetchone()
        period_start = br.billing_period_start if br else None
        period_end = br.billing_period_end if br else None

        if brm.matter_id:
            rows = (await db.execute(text("""
                SELECT te.id, te.date AS slip_date, te.hours, te.rate,
                       COALESCE(te.amount, te.hours * te.rate) AS value,
                       te.description AS narrative, te.timekeeper_name, te.utbms_code,
                       'time_entry' AS source
                FROM time_entries te
                WHERE TRIM(te.tenant_id) = :tid AND te.matter_id = CAST(:mid AS uuid)
                  AND te.status = 'draft' AND te.billable = true AND te.invoice_id IS NULL
                  AND te.date >= CAST(:ps AS date) AND te.date <= CAST(:pe AS date)
                ORDER BY te.date, te.timekeeper_name
            """), {"tid": tid, "mid": str(brm.matter_id),
                   "ps": period_start, "pe": period_end})).fetchall()
            slips.extend([dict(r._mapping) for r in rows])

        adjustments = (await db.execute(text("""
            SELECT * FROM prebill_adjustments
            WHERE tenant_id = :tid AND bill_run_matter_id = :brmid ORDER BY created_at
        """), {"tid": tid, "brmid": bill_run_matter_id})).fetchall()

    return {"matter": brm_dict, "slips": slips,
            "adjustments": [dict(a._mapping) for a in adjustments],
            "total_hours": sum(float(s.get("hours") or 0) for s in slips),
            "total_fees": sum(float(s.get("value") or 0) for s in slips)}


async def _recalc_matter_totals(db, tid: str, bill_run_matter_id: int):
    """Recalculate fee_total, writeoff_total, net_total accounting for all adjustments."""
    # Get total N/C + W/O adjustments
    adj = (await db.execute(text("""
        SELECT COALESCE(SUM(CASE WHEN adjustment_type IN ('no_charge', 'write_off')
               THEN CAST(original_value AS numeric) ELSE 0 END), 0) AS total_adj
        FROM prebill_adjustments
        WHERE tenant_id = :tid AND bill_run_matter_id = :brmid
    """), {"tid": tid, "brmid": bill_run_matter_id})).scalar() or 0

    # Get gross fees from time entries
    brm = (await db.execute(text("""
        SELECT matter_id, prior_balance FROM bill_run_matters WHERE id = :id AND tenant_id = :tid
    """), {"id": bill_run_matter_id, "tid": tid})).fetchone()

    if brm and brm.matter_id:
        br = (await db.execute(text("""
            SELECT billing_period_start, billing_period_end FROM bill_runs br
            JOIN bill_run_matters brm ON brm.bill_run_id = br.id
            WHERE brm.id = :brmid
        """), {"brmid": bill_run_matter_id})).fetchone()

        gross = (await db.execute(text("""
            SELECT COALESCE(SUM(COALESCE(te.amount, te.hours * te.rate)), 0)
            FROM time_entries te
            WHERE TRIM(te.tenant_id) = :tid AND te.matter_id = CAST(:mid AS uuid)
              AND te.status = 'draft' AND te.billable = true AND te.invoice_id IS NULL
              AND te.date >= CAST(:ps AS date) AND te.date <= CAST(:pe AS date)
        """), {"tid": tid, "mid": str(brm.matter_id),
               "ps": br.billing_period_start, "pe": br.billing_period_end})).scalar() or 0
    else:
        gross = 0

    gross_dec = Decimal(str(gross))
    adj_dec = Decimal(str(adj))
    prior = Decimal(str(brm.prior_balance or 0)) if brm else Decimal("0")
    net_fees = gross_dec - adj_dec
    net_total = net_fees + prior

    await db.execute(text("""
        UPDATE bill_run_matters
        SET fee_total = :fees, writeoff_total = :adj, net_total = :net
        WHERE id = :id AND tenant_id = :tid
    """), {"fees": float(net_fees), "adj": float(adj_dec),
           "net": float(net_total), "id": bill_run_matter_id, "tid": tid})


async def apply_adjustment(
    tenant_id: str, bill_run_matter_id: int, adjustment_type: str, adjusted_by_id: int,
    time_entry_id: Optional[str] = None, ts_slip_id: Optional[str] = None,
    original_value: Optional[float] = None, adjusted_value: Optional[float] = None,
    original_hours: Optional[float] = None, adjusted_hours: Optional[float] = None,
    original_narrative: Optional[str] = None, adjusted_narrative: Optional[str] = None,
    reason: Optional[str] = None,
) -> int:
    tid = tenant_id.strip()
    async with AsyncSessionLocal() as db:
        row = (await db.execute(text("""
            INSERT INTO prebill_adjustments
                (tenant_id, bill_run_matter_id, time_entry_id, ts_slip_id,
                 adjustment_type, original_value, adjusted_value,
                 original_hours, adjusted_hours,
                 original_narrative, adjusted_narrative, reason, adjusted_by_id)
            VALUES (:tid, :brmid, CAST(NULLIF(:teid, '') AS uuid), NULLIF(:tsid, ''),
                    :type, :ov, :av, :oh, :ah, :on, :an, :reason, :by)
            RETURNING id
        """), {"tid": tid, "brmid": bill_run_matter_id,
               "teid": time_entry_id or "", "tsid": ts_slip_id or "",
               "type": adjustment_type, "ov": original_value, "av": adjusted_value,
               "oh": original_hours, "ah": adjusted_hours,
               "on": original_narrative, "an": adjusted_narrative,
               "reason": reason, "by": adjusted_by_id})).fetchone()

        await _recalc_matter_totals(db, tid, bill_run_matter_id)
        await db.commit()
    return row[0]


async def reverse_adjustment(tenant_id: str, adjustment_id: int) -> dict:
    """Remove a no_charge or write_off adjustment and recalculate."""
    tid = tenant_id.strip()
    async with AsyncSessionLocal() as db:
        adj = (await db.execute(text("""
            SELECT id, bill_run_matter_id, adjustment_type, time_entry_id, original_value
            FROM prebill_adjustments WHERE id = :id AND tenant_id = :tid
        """), {"id": adjustment_id, "tid": tid})).fetchone()
        if not adj:
            return {"error": "Adjustment not found"}

        await db.execute(text("""
            DELETE FROM prebill_adjustments WHERE id = :id AND tenant_id = :tid
        """), {"id": adjustment_id, "tid": tid})

        await _recalc_matter_totals(db, tid, adj.bill_run_matter_id)
        await db.commit()

    return {"reversed": adjustment_id, "type": adj.adjustment_type,
            "time_entry_id": str(adj.time_entry_id) if adj.time_entry_id else None,
            "restored_value": float(adj.original_value or 0)}


async def approve_matter(tenant_id: str, bill_run_matter_id: int,
                         reviewer_id: int, reviewer_notes: Optional[str] = None) -> None:
    tid = tenant_id.strip()
    async with AsyncSessionLocal() as db:
        # Recalculate before approving to ensure totals are current
        await _recalc_matter_totals(db, tid, bill_run_matter_id)
        await db.execute(text("""
            UPDATE bill_run_matters SET status = 'approved', reviewed_by_id = :by,
                reviewed_at = now(), reviewer_notes = :notes
            WHERE id = :id AND tenant_id = :tid
        """), {"by": reviewer_id, "notes": reviewer_notes,
               "id": bill_run_matter_id, "tid": tid})
        await db.commit()


async def skip_matter(tenant_id: str, bill_run_matter_id: int, reason: str = "") -> None:
    tid = tenant_id.strip()
    async with AsyncSessionLocal() as db:
        await db.execute(text("""
            UPDATE bill_run_matters SET status = 'skipped', reviewer_notes = :reason
            WHERE id = :id AND tenant_id = :tid
        """), {"reason": reason, "id": bill_run_matter_id, "tid": tid})
        await db.commit()


async def finalize_bill_run(tenant_id: str, bill_run_id: int, finalized_by_id: int) -> dict:
    tid = tenant_id.strip()
    invoices_created = []

    async with AsyncSessionLocal() as db:
        matters = (await db.execute(text("""
            SELECT * FROM bill_run_matters
            WHERE bill_run_id = :brid AND tenant_id = :tid AND status = 'approved'
            ORDER BY client_name, matter_number
        """), {"brid": bill_run_id, "tid": tid})).fetchall()

        br = (await db.execute(text("""
            SELECT * FROM bill_runs WHERE id = :brid AND tenant_id = :tid
        """), {"brid": bill_run_id, "tid": tid})).fetchone()

        max_num_row = (await db.execute(text("""
            SELECT MAX(CASE WHEN invoice_number ~ '^[0-9]+$'
                THEN CAST(invoice_number AS INTEGER) ELSE 0 END)
            FROM invoices WHERE TRIM(tenant_id) = :tid
        """), {"tid": tid})).fetchone()
        next_num = (max_num_row[0] or 14000) + 1

        for m in matters:
            invoice_number = str(next_num)

            # fee_total and net_total already account for N/C and W/O
            # (recalculated on every approve via _recalc_matter_totals)
            fees = float(m.fee_total or 0)
            net = float(m.net_total or fees)
            wo = float(m.writeoff_total or 0)

            inv = (await db.execute(text("""
                INSERT INTO invoices
                    (tenant_id, matter_id, client_id, invoice_number,
                     invoice_date, due_date, subtotal, total, total_amount,
                     balance_due, bill_run_id, writeoff_total, status)
                VALUES (:tid, CAST(NULLIF(:mid, '') AS uuid),
                        CAST(NULLIF(:cid, '') AS uuid),
                        :num, CURRENT_DATE, CURRENT_DATE + 30,
                        :fees, :net, :net, :net, :brid, :wo, 'open')
                RETURNING id
            """), {"tid": tid, "mid": str(m.matter_id) if m.matter_id else "",
                   "cid": str(m.client_id) if m.client_id else "",
                   "num": invoice_number, "fees": fees, "net": net,
                   "brid": bill_run_id, "wo": wo})).fetchone()
            invoice_id = inv[0]

            # Mark time entries as billed
            if m.matter_id:
                await db.execute(text("""
                    UPDATE time_entries
                    SET status = 'billed', invoice_id = CAST(:inv AS uuid), updated_at = now()
                    WHERE TRIM(tenant_id) = :tid AND matter_id = CAST(:mid AS uuid)
                      AND status = 'draft' AND billable = true AND invoice_id IS NULL
                      AND date BETWEEN :ps AND :pe
                """), {"inv": str(invoice_id), "tid": tid, "mid": str(m.matter_id),
                       "ps": br.billing_period_start, "pe": br.billing_period_end})

            await db.execute(text("""
                UPDATE bill_run_matters
                SET status = 'invoiced', invoice_id = CAST(:inv AS uuid), invoice_number = :num
                WHERE id = :id AND tenant_id = :tid
            """), {"inv": str(invoice_id), "num": invoice_number, "id": m.id, "tid": tid})

            invoices_created.append({"invoice_id": str(invoice_id),
                                     "invoice_number": invoice_number,
                                     "matter_name": m.matter_name, "net_total": net})
            next_num += 1

        total_invoiced = sum(i["net_total"] for i in invoices_created)
        await db.execute(text("""
            UPDATE bill_runs SET status = 'finalized', finalized_by_id = :by,
                finalized_at = now(), total_invoiced = :ti
            WHERE id = :brid AND tenant_id = :tid
        """), {"by": finalized_by_id, "ti": total_invoiced,
               "brid": bill_run_id, "tid": tid})
        await db.commit()

    return {"bill_run_id": bill_run_id, "invoices_created": invoices_created,
            "total_invoiced": total_invoiced}
