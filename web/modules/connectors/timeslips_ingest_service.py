"""
modules/connectors/timeslips_ingest_service.py
Praesidium — Timeslips Ingest Service
Full implementation of the /api/connectors/timeslips/ingest endpoint.

Replaces the M10 C2 stub in router.py.

Processes batched payloads from ts_sync_agent.py (v2.0):
  - Validates X-Connector-Key against credentials_vault
  - Upserts ts_clients, ts_timekeepers on initial batch
  - Inserts ts_slips with SHA-256 content-hash dedup
  - Upserts ts_invoices, ts_payments
  - Logs each run to billing_import_log
  - Returns counts for agent confirmation

Field map (agent → database):
  slip_id          → source_slip_id
  trans_type       → trans_type
  slip_date        → slip_date
  client_id        → source_client_id
  timekeeper_id    → source_tk_id
  hours            → hours
  value / billed_value / wip_value → respective columns
  description      → narrative
  billed           → billed
  invoice_num      → invoice_num
"""

import hashlib
import json
import logging
from datetime import datetime, date, timezone
from typing import Optional

from sqlalchemy import text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)


def _parse_date(v):
    """Convert ISO date string or None to date object. asyncpg requires date objects not strings."""
    if v is None:
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


# ── Key validation ─────────────────────────────────────────────────────────────

async def validate_timeslips_key(tenant_id: str, api_key: str) -> bool:
    """
    Validate X-Connector-Key against credentials_vault.
    Accepts the hardcoded key from ts_sync_agent_config.json for hjmm-prod,
    or a vault-stored key for commercial tenants.
    """
    if not api_key or not tenant_id:
        return False
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT 1 FROM credentials_vault
                    WHERE TRIM(tenant_id) = :tid
                      AND provider = 'timeslips'
                      AND key_type = 'ingest_api_key'
                      AND encrypted_key = :key
                    LIMIT 1
                """),
                {"tid": tenant_id.strip(), "key": api_key.strip()}
            )
            row = result.first()
            if row:
                return True
    except Exception as e:
        logger.warning(f"[ts_ingest] Key validation DB error: {e}")

    return False


# ── Content hash (dedup key) ───────────────────────────────────────────────────

def _slip_hash(tenant_id: str, slip: dict) -> str:
    """
    SHA-256 dedup hash matching ts_sync_agent.py dedup architecture:
    SHA256(tenant_id + source_slip_id + client_id + tk_id + date + hours + narrative)
    Normalized: lowercase narrative, 4-decimal hours.
    """
    narrative = (slip.get("description") or "").lower().strip()
    hours     = f"{float(slip.get('hours') or 0):.4f}"
    parts = "|".join([
        str(tenant_id).strip(),
        str(slip.get("slip_id") or ""),
        str(slip.get("client_id") or ""),
        str(slip.get("timekeeper_id") or ""),
        str(slip.get("slip_date") or ""),
        hours,
        narrative,
    ])
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


# ── Upsert helpers ─────────────────────────────────────────────────────────────

async def _upsert_clients(session, tenant_id: str, clients: list) -> int:
    if not clients:
        return 0
    count = 0
    for c in clients:
        try:
            await session.execute(
                text("""
                    INSERT INTO ts_clients (
                        tenant_id, source_id, ts_client_id,
                        ts_name, ts_address, ts_email, ts_phone, ts_raw,
                        client_code, matter_code,
                        address1, address2, city, state, zip,
                        phone1, email, notes,
                        case_type, client_status, sup_attorney, billing_atty,
                        opened, closed, referred_by, opp_counsel,
                        paralegal, associate, est_billings,
                        raw_data
                    ) VALUES (
                        :tid, 'timeslips', :ts_client_id,
                        :ts_name, :ts_address, :ts_email, :ts_phone, :ts_raw,
                        :client_code, :matter_code,
                        :addr1, :addr2, :city, :state, :zip,
                        :phone1, :email, :notes,
                        :case_type, :client_status, :sup_atty, :bill_atty,
                        :opened, :closed, :referred_by, :opp_counsel,
                        :paralegal, :associate, :est_billings,
                        :raw_data
                    )
                    ON CONFLICT (tenant_id, source_id, ts_client_id)
                    DO UPDATE SET
                        ts_name       = EXCLUDED.ts_name,
                        ts_raw        = EXCLUDED.ts_raw,
                        client_code   = EXCLUDED.client_code,
                        matter_code   = EXCLUDED.matter_code,
                        case_type     = EXCLUDED.case_type,
                        client_status = EXCLUDED.client_status,
                        sup_attorney  = EXCLUDED.sup_attorney,
                        billing_atty  = EXCLUDED.billing_atty,
                        last_synced_at = now(),
                        updated_at    = now()
                """),
                {
                    "tid":           tenant_id,
                    "ts_client_id":  str(c.get("ts_client_id") or ""),
                    "ts_name":       str(c.get("fullname") or "") or None,
                    "ts_address":    str(c.get("address1") or "") or None,
                    "ts_email":      str(c.get("email") or "") or None,
                    "ts_phone":      str(c.get("phone1") or "") or None,
                    "ts_raw":        json.dumps(c),
                    "client_code":   c.get("client_code"),
                    "matter_code":   c.get("matter_code"),
                    "addr1":         c.get("address1"),
                    "addr2":         c.get("address2"),
                    "city":          c.get("city"),
                    "state":         c.get("state"),
                    "zip":           c.get("zip"),
                    "phone1":        c.get("phone1"),
                    "email":         c.get("email"),
                    "notes":         c.get("notes"),
                    "case_type":     str(c.get("case_type") or "") or None,
                    "client_status": str(c.get("client_status") or "") or None,
                    "sup_atty":      str(c.get("sup_attorney") or "") or None,
                    "bill_atty":     str(c.get("billing_atty") or "") or None,
                    "opened":        str(c.get("opened") or "") or None,
                    "closed":        str(c.get("closed") or "") or None,
                    "referred_by":   str(c.get("referred_by") or "") or None,
                    "opp_counsel":   str(c.get("opp_counsel") or "") or None,
                    "paralegal":     str(c.get("paralegal") or "") or None,
                    "associate":     str(c.get("associate") or "") or None,
                    "est_billings":  float(c.get("est_billings") or 0),
                    "raw_data":      json.dumps(c),
                }
            )
            count += 1
        except Exception as e:
            logger.exception(f"[ts_ingest] client upsert error (id={c.get('ts_client_id')}): {e}")
    return count


async def _upsert_timekeepers(session, tenant_id: str, timekeepers: list) -> int:
    if not timekeepers:
        return 0
    count = 0
    for tk in timekeepers:
        try:
            await session.execute(
                text("""
                    INSERT INTO ts_timekeepers (
                        tenant_id, source_id, ts_tk_id,
                        ts_name, ts_initials, ts_raw,
                        initials, email, title_level, raw_data
                    ) VALUES (
                        :tid, 'timeslips', :ts_tk_id,
                        :ts_name, :ts_initials, :ts_raw,
                        :initials, :email, :title_level, :raw_data
                    )
                    ON CONFLICT (tenant_id, source_id, ts_tk_id)
                    DO UPDATE SET
                        ts_name       = EXCLUDED.ts_name,
                        ts_initials   = EXCLUDED.ts_initials,
                        ts_raw        = EXCLUDED.ts_raw,
                        initials      = EXCLUDED.initials,
                        email         = EXCLUDED.email,
                        last_synced_at = now(),
                        updated_at    = now()
                """),
                {
                    "tid":         tenant_id,
                    "ts_tk_id":    str(tk.get("ts_tk_id") or ""),
                    "ts_name":     str(tk.get("fullname") or "") or None,
                    "ts_initials": str(tk.get("initials") or "") or None,
                    "ts_raw":      json.dumps(tk),
                    "initials":    str(tk.get("initials") or "") or None,
                    "email":       str(tk.get("email") or "") or None,
                    "title_level": str(tk.get("title_level") or "") or None,
                    "raw_data":    json.dumps(tk),
                }
            )
            count += 1
        except Exception as e:
            logger.exception(f"[ts_ingest] timekeeper upsert error (id={tk.get('ts_tk_id')}): {e}")
    return count


async def _insert_slips(session, tenant_id: str, source_id: str, slips: list) -> dict:
    new_count    = 0
    dedup_count  = 0
    error_count  = 0

    for slip in slips:
        content_hash = _slip_hash(tenant_id, slip)
        try:
            result = await session.execute(
                text("""
                    INSERT INTO ts_slips (
                        tenant_id, source_id, source_slip_id, content_hash,
                        trans_type, slip_date, end_date,
                        source_client_id, source_tk_id,
                        activity_id, reference_id,
                        hours, hours_estimated,
                        quantity, price, rate, rate_type,
                        value, billed_value, wip_value,
                        billed, bill_status, on_hold,
                        invoice_id, invoice_num, post_period,
                        orig_slip_id, narrative, raw_data
                    ) VALUES (
                        :tid, :src_id, :slip_id, :hash,
                        :trans_type, :slip_date, :end_date,
                        :client_id, :tk_id,
                        :activity_id, :reference_id,
                        :hours, :hours_est,
                        :qty, :price, :rate, :rate_type,
                        :value, :billed_val, :wip_val,
                        :billed, :bill_status, :on_hold,
                        :invoice_id, :invoice_num, :post_period,
                        :orig_slip_id, :narrative, :raw
                    )
                    ON CONFLICT (tenant_id, source_id, content_hash) DO NOTHING
                """),
                {
                    "tid":          tenant_id,
                    "src_id":       source_id,
                    "slip_id":      str(slip.get("slip_id") or ""),
                    "hash":         content_hash,
                    "trans_type":   slip.get("trans_type"),
                    "slip_date":    _parse_date(slip.get("slip_date")),
                    "end_date":     _parse_date(slip.get("end_date")),
                    "client_id":    str(slip.get("client_id") or "") or None,
                    "tk_id":        str(slip.get("timekeeper_id") or "") or None,
                    "activity_id":  str(slip.get("activity_id") or "") or None,
                    "reference_id": str(slip.get("reference_id") or "") or None,
                    "hours":        slip.get("hours") or 0,
                    "hours_est":    slip.get("hours_estimated") or 0,
                    "qty":          min(float(slip.get("quantity") or 0), 99999999999999.99),
                    "price":        min(float(slip.get("price") or 0), 99999999999999.99),
                    "rate":         min(float(slip.get("rate") or 0), 99999999999999.99),
                    "rate_type":    str(slip.get("rate_type") or "") or None,
                    "value":        min(float(slip.get("value") or 0), 99999999999999.99),
                    "billed_val":   min(float(slip.get("billed_value") or 0), 99999999999999.99),
                    "wip_val":      min(float(slip.get("wip_value") or 0), 99999999999999.99),
                    "billed":       bool(slip.get("billed")),
                    "bill_status":  slip.get("bill_status"),
                    "on_hold":      bool(slip.get("on_hold")),
                    "invoice_id":   str(slip.get("invoice_id") or "") or None,
                    "invoice_num":  slip.get("invoice_num") or None,
                    "post_period":  str(slip.get("post_period") or "") or None,
                    "orig_slip_id": str(slip.get("orig_slip_id") or "") or None,
                    "narrative":    slip.get("description") or "",
                    "raw":          json.dumps(slip),
                }
            )
            if result.rowcount == 0:
                dedup_count += 1
            else:
                new_count += 1
        except Exception as e:
            error_count += 1
            logger.warning(f"[ts_ingest] slip insert error (id={slip.get('slip_id')}): {e}")

    return {"new": new_count, "deduped": dedup_count, "errors": error_count}


async def _upsert_invoices(session, tenant_id: str, source_id: str, invoices: list) -> int:
    if not invoices:
        return 0
    count = 0
    for inv in invoices:
        try:
            await session.execute(
                text("""
                    INSERT INTO ts_invoices (
                        tenant_id, source_id, source_invoice_id,
                        invoice_num, charge_fees, charge_costs,
                        net_due, paid_in_full, invoice_status,
                        slip_start, slip_end, raw_data
                    ) VALUES (
                        :tid, :src_id, :inv_id,
                        :inv_num, :fees, :costs,
                        :net_due, :pif, :status,
                        :slip_start, :slip_end, :raw
                    )
                    ON CONFLICT (tenant_id, source_id, source_invoice_id)
                    DO UPDATE SET
                        charge_fees   = EXCLUDED.charge_fees,
                        charge_costs  = EXCLUDED.charge_costs,
                        net_due       = EXCLUDED.net_due,
                        paid_in_full  = EXCLUDED.paid_in_full,
                        invoice_status = EXCLUDED.invoice_status,
                        updated_at    = now()
                """),
                {
                    "tid":        tenant_id,
                    "src_id":     source_id,
                    "inv_id":     str(inv.get("ts_invoice_id") or ""),
                    "inv_num":    inv.get("invoice_num"),
                    "fees":       inv.get("charge_fees") or 0,
                    "costs":      inv.get("charge_costs") or 0,
                    "net_due":    inv.get("net_due") or 0,
                    "pif":        bool(inv.get("paid_in_full")),
                    "status":     inv.get("invoice_status"),
                    "slip_start": _parse_date(inv.get("slip_start")),
                    "slip_end":   _parse_date(inv.get("slip_end")),
                    "raw":        json.dumps(inv),
                }
            )
            count += 1
        except Exception as e:
            logger.warning(f"[ts_ingest] invoice upsert error: {e}")
    return count


async def _upsert_payments(session, tenant_id: str, source_id: str, payments: list) -> int:
    if not payments:
        return 0
    count = 0
    for pmt in payments:
        try:
            await session.execute(
                text("""
                    INSERT INTO ts_payments (
                        tenant_id, source_id, source_payment_id,
                        date_entered, source_client_id,
                        amount, description, invoice_num,
                        source_invoice_id, post_period, raw_data
                    ) VALUES (
                        :tid, :src_id, :pmt_id,
                        :date_entered, :client_id,
                        :amount, :desc, :inv_num,
                        :inv_id, :post_period, :raw
                    )
                    ON CONFLICT (tenant_id, source_id, source_payment_id) DO NOTHING
                """),
                {
                    "tid":          tenant_id,
                    "src_id":       source_id,
                    "pmt_id":       str(pmt.get("ts_payment_id") or ""),
                    "date_entered": _parse_date(pmt.get("date_entered")),
                    "client_id":    str(pmt.get("client_id") or "") or None,
                    "amount":       pmt.get("amount") or 0,
                    "desc":         pmt.get("description") or "",
                    "inv_num":      pmt.get("invoice_num"),
                    "inv_id":       str(pmt.get("invoice_id") or "") or None,
                    "post_period":  str(pmt.get("post_period") or "") or None,
                    "raw":          json.dumps(pmt),
                }
            )
            count += 1
        except Exception as e:
            logger.warning(f"[ts_ingest] payment upsert error: {e}")
    return count


# ── Main ingest handler ────────────────────────────────────────────────────────

async def process_timeslips_ingest(tenant_id: str, body: dict) -> dict:
    """
    Full ingest implementation. Called by the router endpoint.
    Handles batched payloads from ts_sync_agent.py v2.0.

    Returns dict with counts for the agent response.
    """
    source_id     = "timeslips"
    slips         = body.get("slips", [])
    clients       = body.get("clients", [])
    timekeepers   = body.get("timekeepers", [])
    invoices      = body.get("invoices", [])
    payments      = body.get("payments", [])
    is_initial    = body.get("is_initial", False)
    batch_index   = body.get("batch_index", 0)
    batch_count   = body.get("batch_count", 1)
    agent_version = body.get("agent_version", "unknown")

    # Create billing_import_log entry on first batch
    log_id = None
    if batch_index == 0:
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text("""
                        INSERT INTO billing_import_log (
                            tenant_id, source_id, run_type, status, started_at, detail
                        ) VALUES (
                            :tid, :src, :run_type, 'running', now(),
                            :detail
                        )
                        RETURNING id
                    """),
                    {
                        "tid":      tenant_id,
                        "src":      source_id,
                        "run_type": "full" if is_initial else "incremental",
                        "detail":   json.dumps({
                            "agent_version": agent_version,
                            "batch_count":   batch_count,
                            "is_initial":    is_initial,
                        }),
                    }
                )
                row = result.first()
                log_id = row[0] if row else None
                await session.commit()
        except Exception as e:
            logger.warning(f"[ts_ingest] Could not create billing_import_log: {e}")

    # Process each section independently — one section failing won't kill others
    clients_count     = 0
    timekeepers_count = 0
    slip_result       = {"new": 0, "deduped": 0, "errors": 0}
    invoices_count    = 0
    payments_count    = 0

    # Metadata only on first batch — each in its own transaction
    if batch_index == 0:
        if clients:
            try:
                async with AsyncSessionLocal() as session:
                    clients_count = await _upsert_clients(session, tenant_id, clients)
                    await session.commit()
                logger.info(f"[ts_ingest] {clients_count} clients written")
            except Exception as e:
                logger.exception(f"[ts_ingest] clients failed — {type(e).__name__}: {e}")

        if timekeepers:
            try:
                async with AsyncSessionLocal() as session:
                    timekeepers_count = await _upsert_timekeepers(session, tenant_id, timekeepers)
                    await session.commit()
                logger.info(f"[ts_ingest] {timekeepers_count} timekeepers written")
            except Exception as e:
                logger.exception(f"[ts_ingest] timekeepers failed — {type(e).__name__}: {e}")

    # Slips
    try:
        async with AsyncSessionLocal() as session:
            slip_result = await _insert_slips(session, tenant_id, source_id, slips)
            await session.commit()
    except Exception as e:
        logger.exception(f"[ts_ingest] slips failed: {e}")

    # Invoices
    if invoices:
        try:
            async with AsyncSessionLocal() as session:
                invoices_count = await _upsert_invoices(session, tenant_id, source_id, invoices)
                await session.commit()
        except Exception as e:
            logger.exception(f"[ts_ingest] invoices failed: {e}")

    # Payments
    if payments:
        try:
            async with AsyncSessionLocal() as session:
                payments_count = await _upsert_payments(session, tenant_id, source_id, payments)
                await session.commit()
        except Exception as e:
            logger.exception(f"[ts_ingest] payments failed: {e}")

    logger.info(
        f"[ts_ingest] batch {batch_index+1}/{batch_count} — "
        f"slips: +{slip_result['new']} new, {slip_result['deduped']} deduped, "
        f"{slip_result['errors']} errors | "
        f"clients: {clients_count} | timekeepers: {timekeepers_count} | "
        f"invoices: {invoices_count} | payments: {payments_count}"
    )

    # Update log entry on last batch
    if batch_index == batch_count - 1 and log_id:
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        UPDATE billing_import_log
                        SET status       = 'complete',
                            completed_at = now(),
                            slips_new    = slips_new + :new,
                            slips_deduped = slips_deduped + :deduped,
                            slips_error  = slips_error + :errors
                        WHERE id = :log_id
                    """),
                    {
                        "new":     slip_result["new"],
                        "deduped": slip_result["deduped"],
                        "errors":  slip_result["errors"],
                        "log_id":  log_id,
                    }
                )
                await session.commit()
        except Exception as e:
            logger.warning(f"[ts_ingest] Could not update billing_import_log: {e}")

    return {
        "slips_new":      slip_result["new"],
        "slips_deduped":  slip_result["deduped"],
        "slips_errors":   slip_result["errors"],
        "clients":        clients_count,
        "timekeepers":    timekeepers_count,
        "invoices":       invoices_count,
        "payments":       payments_count,
        "batch_index":    batch_index,
        "batch_count":    batch_count,
    }
