import decimal
"""
jobs/timeslips_sync.py
Praesidium — Server-Side Timeslips Sync Job

Connects directly to the Firebird database on appserver.hjmmlegal.com:3050
and writes to PostgreSQL using the existing ingest service functions.

Runs as an RQ job on the worker containers. Eliminates the Windows agent
and all HTTP/firewall issues.

Requires: fdb (pip install fdb) + libfbclient.so (apt install libfirebird4.0)
          OR firebird3.0-utils for fbclient

Usage (RQ):
    from jobs.timeslips_sync import run_timeslips_sync
    queue.enqueue(run_timeslips_sync, tenant_id="986c0fee-...", mode="full")

Usage (CLI for testing):
    python -m jobs.timeslips_sync --full
    python -m jobs.timeslips_sync            # incremental
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, date, timedelta, timezone
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger("ts_sync")

# ── Julian date epoch (Firebird/Delphi serial dates) ──────────────────────────
JULIAN_EPOCH = date(1899, 12, 30)

# ── NAMETYPE constants — auto-detected at runtime ────────────────────────────
_NAMETYPE_TIMEKEEPER = None
_NAMETYPE_CLIENT     = None


# ═══════════════════════════════════════════════════════════════════════════════
# Field conversion helpers (same logic as ts_sync_agent.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _julian_to_date(n):
    if n is None:
        return None
    try:
        d = JULIAN_EPOCH + timedelta(days=int(n))
        if d.year < 1990 or d.year > 2030:
            return None
        return d.isoformat()
    except Exception:
        return None


def _seconds_to_hours(n):
    if n is None:
        return 0.0
    try:
        return round(float(n) / 3600.0, 4)
    except Exception:
        return 0.0


def _clean_description(raw):
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    parts = re.split(r'[\x00-\x08\x0b-\x1f]', raw)
    candidates = [p.strip() for p in parts if len(p.strip()) > 3]
    if candidates:
        return max(candidates, key=len)
    return re.sub(r'[^\x20-\x7e\t]', '', raw).strip()


def _safe_float(v):
    if v is None:
        return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0


def _safe_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None




def _sanitize_row(d):
    """Convert bytes to cleaned strings, Decimals to float/int for all values in a dict."""
    from decimal import Decimal
    cleaned = {}
    for k, v in d.items():
        if isinstance(v, bytes):
            cleaned[k] = _clean_description(v)
        elif isinstance(v, Decimal):
            cleaned[k] = float(v)
        else:
            cleaned[k] = v
    return cleaned

# ═══════════════════════════════════════════════════════════════════════════════
# Firebird connection and queries
# ═══════════════════════════════════════════════════════════════════════════════

def _get_firebird_config(tenant_id: str) -> dict:
    """
    Get Firebird connection config for a tenant.
    For now, hardcoded for HJMM. Future: read from a tenant connector_configs table.
    """
    return {
        "host":     os.environ.get("TS_FIREBIRD_HOST", "appserver.hjmmlegal.com"),
        "port":     int(os.environ.get("TS_FIREBIRD_PORT", "3050")),
        "database": os.environ.get("TS_FIREBIRD_DATABASE",
                                   r"E:\Sage\Timeslips\Databases\HJMM 2017.FDB"),
        "user":     os.environ.get("TS_FIREBIRD_USER", "SYSDBA"),
        "password": os.environ.get("TS_FIREBIRD_PASSWORD", "ts_2O17p"),
    }


def _connect_firebird(cfg: dict):
    """Connect to Firebird using fdb driver."""
    import fdb
    logger.info(f"Connecting to Firebird: {cfg['host']}/{cfg['port']}:{cfg['database']}")
    con = fdb.connect(
        host=cfg["host"],
        port=cfg["port"],
        database=cfg["database"],
        user=cfg["user"],
        password=cfg["password"],
        charset="UTF8",
    )
    logger.info("Firebird connected.")
    return con


def _scalar(con, sql, params=()):
    cur = con.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    cur.close()
    return row[0] if row and row[0] is not None else None


def _query(con, sql, params=()):
    cur = con.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    rows = [_sanitize_row(dict(zip(cols, row))) for row in cur.fetchall()]
    cur.close()
    return rows


# ═══════════════════════════════════════════════════════════════════════════════
# NAMETYPE detection and data fetch (mirrors agent logic exactly)
# ═══════════════════════════════════════════════════════════════════════════════

def _detect_nametypes(con):
    global _NAMETYPE_TIMEKEEPER, _NAMETYPE_CLIENT

    types = _query(con, "SELECT NAMETYPE, COUNT(*) AS CNT FROM NAME GROUP BY NAMETYPE ORDER BY CNT DESC")

    sample_userids = _query(con, "SELECT DISTINCT USERID FROM SLPTRANS WHERE USERID IS NOT NULL ROWS 5")
    uid_set = {r["USERID"] for r in sample_userids if r["USERID"]}

    if uid_set:
        ph = ",".join("?" * len(uid_set))
        matches = _query(con, f"SELECT DISTINCT NAMETYPE FROM NAME WHERE RECORDID IN ({ph})", tuple(uid_set))
        if matches:
            _NAMETYPE_TIMEKEEPER = matches[0]["NAMETYPE"]

    sample_clientids = _query(con, "SELECT DISTINCT CLIENTID FROM SLPTRANS WHERE CLIENTID IS NOT NULL ROWS 5")
    cid_set = {r["CLIENTID"] for r in sample_clientids if r["CLIENTID"]}

    if cid_set:
        ph = ",".join("?" * len(cid_set))
        matches = _query(con, f"SELECT DISTINCT NAMETYPE FROM NAME WHERE RECORDID IN ({ph})", tuple(cid_set))
        if matches:
            _NAMETYPE_CLIENT = matches[0]["NAMETYPE"]

    logger.info(f"NAMETYPE detected — timekeeper: {_NAMETYPE_TIMEKEEPER}, client: {_NAMETYPE_CLIENT}")
    logger.info(f"All NAMETYPEs: {[(r['NAMETYPE'], r['CNT']) for r in types]}")


def _fetch_slips(con, last_slip_id=0):
    logger.info(f"Fetching slips with RECORDID > {last_slip_id}...")
    rows = _query(con, """
        SELECT RECORDID, TRANSTYPE, STARTDATE, ENDDATE, CLIENTID, USERID,
               ACTYEXPID, REFERENCEID, TIMESPENT, TIMEESTIMATED, QUANTITY,
               PRICE, RATEVALUE, RATETYPE, TRANSVALUE, BILLEDSLIPVALUE,
               BILLED, BILLSTATUS, HOLD, INVOICEID, INVOICENUM,
               POSTPERIOD, ORIGSLIPID, DESCRIPTION
        FROM SLPTRANS WHERE RECORDID > ? ORDER BY RECORDID
    """, (last_slip_id,))

    slips = []
    new_hwm = last_slip_id
    for r in rows:
        slips.append({
            "slip_id":         r["RECORDID"],
            "trans_type":      _safe_int(r["TRANSTYPE"]),
            "slip_date":       _julian_to_date(r["STARTDATE"]),
            "end_date":        _julian_to_date(r["ENDDATE"]),
            "client_id":       r["CLIENTID"],
            "timekeeper_id":   r["USERID"],
            "activity_id":     r["ACTYEXPID"],
            "reference_id":    r["REFERENCEID"],
            "hours":           _seconds_to_hours(r["TIMESPENT"]),
            "hours_estimated": _seconds_to_hours(r["TIMEESTIMATED"]),
            "quantity":        _safe_float(r["QUANTITY"]),
            "price":           _safe_float(r["PRICE"]),
            "rate":            _safe_float(r["RATEVALUE"]),
            "rate_type":       r["RATETYPE"],
            "value":           _safe_float(r["BILLEDSLIPVALUE"]) if r["BILLED"] == 1
                               else _safe_float(r["TRANSVALUE"]),
            "billed_value":    _safe_float(r["BILLEDSLIPVALUE"]),
            "wip_value":       _safe_float(r["TRANSVALUE"]) if r["BILLED"] != 1 else 0.0,
            "billed":          bool(r["BILLED"]),
            "bill_status":     _safe_int(r["BILLSTATUS"]),
            "on_hold":         bool(r["HOLD"]) if r["HOLD"] is not None else False,
            "invoice_id":      r["INVOICEID"],
            "invoice_num":     r["INVOICENUM"],
            "post_period":     r["POSTPERIOD"],
            "orig_slip_id":    r["ORIGSLIPID"],
            "description":     _clean_description(r["DESCRIPTION"]),
        })
        if r["RECORDID"] and r["RECORDID"] > new_hwm:
            new_hwm = r["RECORDID"]

    logger.info(f"Fetched {len(slips)} slips (new HWM: {new_hwm})")
    return slips, new_hwm


def _fetch_clients(con):
    if _NAMETYPE_CLIENT is None:
        return []
    rows = _query(con, """
        SELECT n.RECORDID, n.NICKNAME1, n.NICKNAME2, n.FULLNAME, n.CLASSIFICATION,
               c.ADDRESSLINE1, c.ADDRESSLINE2, c.CITY, c.STATE, c.ZIP,
               c.PHONE1, c.PHONE2, c.EMAIL, c.NOTES, c.MASTERCLIENTID,
               cx.SUP_ATTORNEY, cx.BILLING_ATTY, cx.CASE_TYPE, cx.CLIENT_STATUS,
               cx.OPENED_, cx.CLOSED_, cx.EST0_BILLINGS, cx.REFERRED_BY,
               cx.OPP0_COUNSEL, cx.PARALEGAL_, cx.ASSOCIATE_
        FROM NAME n
        LEFT JOIN CLIINFOF c  ON c.RECORDID = n.RECORDID
        LEFT JOIN CUSTOMC  cx ON cx.RECORDID = n.RECORDID
        WHERE n.NAMETYPE = ? ORDER BY n.RECORDID
    """, (_NAMETYPE_CLIENT,))

    clients = []
    for r in rows:
        nickname = (r["NICKNAME1"] or "").strip()
        client_code, matter_code = (nickname.split("/", 1) + [""])[:2]
        clients.append({
            "ts_client_id":    r["RECORDID"],
            "nickname1":       r["NICKNAME1"],
            "nickname2":       r["NICKNAME2"],
            "fullname":        r["FULLNAME"],
            "classification":  r["CLASSIFICATION"],
            "client_code":     client_code.strip(),
            "matter_code":     matter_code.strip(),
            "address1":        r["ADDRESSLINE1"],
            "address2":        r["ADDRESSLINE2"],
            "city":            r["CITY"],
            "state":           r["STATE"],
            "zip":             r["ZIP"],
            "phone1":          r["PHONE1"],
            "phone2":          r["PHONE2"],
            "email":           r["EMAIL"],
            "notes":           r["NOTES"],
            "master_client_id": r["MASTERCLIENTID"],
            "sup_attorney":    r["SUP_ATTORNEY"],
            "billing_atty":    r["BILLING_ATTY"],
            "case_type":       r["CASE_TYPE"],
            "client_status":   r["CLIENT_STATUS"],
            "opened":          r["OPENED_"],
            "closed":          r["CLOSED_"],
            "est_billings":    _safe_float(r["EST0_BILLINGS"]),
            "referred_by":     r["REFERRED_BY"],
            "opp_counsel":     r["OPP0_COUNSEL"],
            "paralegal":       r["PARALEGAL_"],
            "associate":       r["ASSOCIATE_"],
        })
    logger.info(f"Fetched {len(clients)} clients.")
    return clients


def _fetch_timekeepers(con):
    if _NAMETYPE_TIMEKEEPER is None:
        return []
    rows = _query(con, """
        SELECT n.RECORDID, n.NICKNAME1, n.NICKNAME2, n.FULLNAME, n.CLASSIFICATION,
               u.INITIALS, u.EMAIL, u.TITLELEVEL, u.MINHOURS, u.MINHOURSTERM
        FROM NAME n
        LEFT JOIN USERINFO u ON u.RECORDID = n.RECORDID
        WHERE n.NAMETYPE = ? ORDER BY n.RECORDID
    """, (_NAMETYPE_TIMEKEEPER,))

    timekeepers = []
    for r in rows:
        timekeepers.append({
            "ts_tk_id":       r["RECORDID"],
            "nickname":       r["NICKNAME1"],
            "fullname":       r["FULLNAME"],
            "initials":       r["INITIALS"],
            "email":          r["EMAIL"],
            "title_level":    r["TITLELEVEL"],
            "min_hours":      _safe_float(r["MINHOURS"]),
            "classification": r["CLASSIFICATION"],
        })
    logger.info(f"Fetched {len(timekeepers)} timekeepers.")
    return timekeepers


def _fetch_invoices(con, last_slip_id=0, is_initial=False):
    if is_initial:
        rows = _query(con, """
            SELECT RECORDID, ARTRANSID, INVOICENUM, CHARGEFEES, CHARGECOSTS,
                   NETDUE, PAIDINFULL, INVOICESTATUS, BILLTYPE, SLIPSTARTDATE, SLIPENDDATE
            FROM INVOICE ORDER BY RECORDID
        """)
    else:
        rows = _query(con, """
            SELECT i.RECORDID, i.ARTRANSID, i.INVOICENUM, i.CHARGEFEES, i.CHARGECOSTS,
                   i.NETDUE, i.PAIDINFULL, i.INVOICESTATUS, i.BILLTYPE, i.SLIPSTARTDATE, i.SLIPENDDATE
            FROM INVOICE i
            WHERE i.PAIDINFULL = 'F'
               OR i.RECORDID IN (SELECT DISTINCT INVOICEID FROM SLPTRANS WHERE RECORDID > ?)
            ORDER BY i.RECORDID
        """, (last_slip_id,))

    invoices = []
    for r in rows:
        invoices.append({
            "ts_invoice_id":  r["RECORDID"],
            "ar_trans_id":    r["ARTRANSID"],
            "invoice_num":    r["INVOICENUM"],
            "charge_fees":    _safe_float(r["CHARGEFEES"]),
            "charge_costs":   _safe_float(r["CHARGECOSTS"]),
            "net_due":        _safe_float(r["NETDUE"]),
            "paid_in_full":   r["PAIDINFULL"] == "T",
            "invoice_status": _safe_int(r["INVOICESTATUS"]),
            "bill_type":      r["BILLTYPE"],
            "slip_start":     _julian_to_date(r["SLIPSTARTDATE"]),
            "slip_end":       _julian_to_date(r["SLIPENDDATE"]),
        })
    logger.info(f"Fetched {len(invoices)} invoices.")
    return invoices


def _fetch_payments(con, is_initial=False):
    if is_initial:
        rows = _query(con, """
            SELECT RECORDID, TRANSID, DATEENTERED, CLIENTID, TRANSVALUE,
                   DESCRIPTION, INVOICENUM, INVOICEID, POSTPERIOD
            FROM ARTRANS WHERE TRANSTYPE = 1 ORDER BY RECORDID
        """)
    else:
        rows = _query(con, """
            SELECT RECORDID, TRANSID, DATEENTERED, CLIENTID, TRANSVALUE,
                   DESCRIPTION, INVOICENUM, INVOICEID, POSTPERIOD
            FROM ARTRANS WHERE TRANSTYPE = 1 ORDER BY RECORDID
        """)

    payments = []
    for r in rows:
        payments.append({
            "ts_payment_id":  r["RECORDID"],
            "trans_id":       r["TRANSID"],
            "date_entered":   _julian_to_date(r["DATEENTERED"]),
            "client_id":      r["CLIENTID"],
            "amount":         abs(_safe_float(r["TRANSVALUE"])),
            "description":    _clean_description(r["DESCRIPTION"]),
            "invoice_num":    r["INVOICENUM"],
            "invoice_id":     r["INVOICEID"],
            "post_period":    r["POSTPERIOD"],
        })
    logger.info(f"Fetched {len(payments)} payments.")
    return payments


# ═══════════════════════════════════════════════════════════════════════════════
# PostgreSQL write (reuses ingest service functions)
# ═══════════════════════════════════════════════════════════════════════════════

async def _write_to_postgres(tenant_id: str, slips, clients, timekeepers,
                              invoices, payments, is_initial: bool, new_hwm: int):
    """Write fetched data to PostgreSQL using the existing ingest service."""
    from modules.connectors.timeslips_ingest_service import (
        _upsert_clients, _upsert_timekeepers, _insert_slips,
        _upsert_invoices, _upsert_payments
    )
    from core.db.base import AsyncSessionLocal

    source_id = "timeslips"

    # Log entry
    log_id = None
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    INSERT INTO billing_import_log (
                        tenant_id, source_id, run_type, status, started_at, detail
                    ) VALUES (:tid, :src, :run_type, 'running', now(), :detail)
                    RETURNING id
                """),
                {
                    "tid":      tenant_id,
                    "src":      source_id,
                    "run_type": "full" if is_initial else "incremental",
                    "detail":   json.dumps({
                        "source": "server_side_sync",
                        "slips": len(slips),
                        "invoices": len(invoices),
                        "payments": len(payments),
                    }),
                }
            )
            row = result.first()
            log_id = row[0] if row else None
            await session.commit()
    except Exception as e:
        logger.warning(f"Could not create billing_import_log: {e}")

    # Clients
    clients_count = 0
    if clients:
        try:
            async with AsyncSessionLocal() as session:
                clients_count = await _upsert_clients(session, tenant_id, clients)
                await session.commit()
            logger.info(f"{clients_count} clients written")
        except Exception as e:
            logger.exception(f"Clients failed: {e}")

    # Timekeepers
    tks_count = 0
    if timekeepers:
        try:
            async with AsyncSessionLocal() as session:
                tks_count = await _upsert_timekeepers(session, tenant_id, timekeepers)
                await session.commit()
            logger.info(f"{tks_count} timekeepers written")
        except Exception as e:
            logger.exception(f"Timekeepers failed: {e}")

    # Slips — batch in groups of 500 to avoid huge transactions
    SLIP_BATCH = 500
    total_new = 0
    total_dedup = 0
    total_errors = 0
    for i in range(0, max(len(slips), 1), SLIP_BATCH):
        batch = slips[i:i + SLIP_BATCH]
        if not batch:
            break
        try:
            async with AsyncSessionLocal() as session:
                result = await _insert_slips(session, tenant_id, source_id, batch)
                await session.commit()
            total_new    += result["new"]
            total_dedup  += result["deduped"]
            total_errors += result["errors"]
            if (i // SLIP_BATCH) % 20 == 0:
                logger.info(f"Slips progress: {i + len(batch)}/{len(slips)} "
                           f"(+{total_new} new, {total_dedup} deduped)")
        except Exception as e:
            logger.exception(f"Slips batch {i}-{i+SLIP_BATCH} failed: {e}")

    logger.info(f"Slips complete: +{total_new} new, {total_dedup} deduped, {total_errors} errors")

    # Invoices — batch in groups of 500
    inv_count = 0
    for i in range(0, max(len(invoices), 1), SLIP_BATCH):
        batch = invoices[i:i + SLIP_BATCH]
        if not batch:
            break
        try:
            async with AsyncSessionLocal() as session:
                inv_count += await _upsert_invoices(session, tenant_id, source_id, batch)
                await session.commit()
        except Exception as e:
            logger.exception(f"Invoices batch failed: {e}")
    logger.info(f"{inv_count} invoices written")

    # Payments — batch in groups of 500
    pmt_count = 0
    for i in range(0, max(len(payments), 1), SLIP_BATCH):
        batch = payments[i:i + SLIP_BATCH]
        if not batch:
            break
        try:
            async with AsyncSessionLocal() as session:
                pmt_count += await _upsert_payments(session, tenant_id, source_id, batch)
                await session.commit()
        except Exception as e:
            logger.exception(f"Payments batch failed: {e}")
    logger.info(f"{pmt_count} payments written")

    # Update HWM in billing_import_sources
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO billing_import_sources (
                        tenant_id, source_id, source_type, config, last_hwm, last_sync_at
                    ) VALUES (:tid, 'timeslips', 'firebird_direct', :config, :hwm, now())
                    ON CONFLICT (tenant_id, source_id)
                    DO UPDATE SET last_hwm = :hwm, last_sync_at = now()
                """),
                {
                    "tid":    tenant_id,
                    "config": json.dumps({"mode": "server_side_direct"}),
                    "hwm":    str(new_hwm),
                }
            )
            await session.commit()
    except Exception as e:
        logger.warning(f"Could not update billing_import_sources: {e}")

    # Finalize log
    if log_id:
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        UPDATE billing_import_log
                        SET status = 'complete', completed_at = now(),
                            slips_new = :new, slips_deduped = :deduped, slips_error = :errors
                        WHERE id = :log_id
                    """),
                    {"new": total_new, "deduped": total_dedup, "errors": total_errors, "log_id": log_id}
                )
                await session.commit()
        except Exception as e:
            logger.warning(f"Could not finalize billing_import_log: {e}")

    return {
        "slips_new": total_new,
        "slips_deduped": total_dedup,
        "slips_errors": total_errors,
        "clients": clients_count,
        "timekeepers": tks_count,
        "invoices": inv_count,
        "payments": pmt_count,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Main sync function — called by RQ or CLI
# ═══════════════════════════════════════════════════════════════════════════════

def run_timeslips_sync(tenant_id: str = "986c0fee-1390-43bb-ad28-8cd1db6de53f",
                        mode: str = "incremental"):
    """
    Main entry point. Reads from Firebird, writes to PostgreSQL.
    mode: "full" or "incremental"

    Called by:
      - RQ scheduler (hourly)
      - Manual trigger via admin UI
      - CLI: python -m jobs.timeslips_sync --full
    """
    logger.info("=" * 55)
    logger.info(f"Timeslips server-side sync — mode={mode}, tenant={tenant_id}")
    logger.info("=" * 55)

    fb_cfg = _get_firebird_config(tenant_id)

    # Determine HWM
    is_initial = (mode == "full")
    last_slip_id = 0

    if not is_initial:
        # Read HWM from billing_import_sources
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            from core.db.base import AsyncSessionLocal

            async def _get_hwm():
                async with AsyncSessionLocal() as session:
                    result = await session.execute(
                        text("""
                            SELECT last_hwm FROM billing_import_sources
                            WHERE TRIM(tenant_id) = :tid AND source_id = 'timeslips'
                        """),
                        {"tid": tenant_id.strip()}
                    )
                    row = result.first()
                    return int(row[0]) if row and row[0] else 0

            last_slip_id = loop.run_until_complete(_get_hwm())
            if last_slip_id == 0:
                is_initial = True
                logger.info("No HWM found — switching to full import")
        except Exception as e:
            logger.warning(f"Could not read HWM: {e} — defaulting to full import")
            is_initial = True

    logger.info(f"Mode: {'full' if is_initial else 'incremental'} | HWM: {last_slip_id}")

    # ── Read from Firebird ────────────────────────────────────────────────
    try:
        con = _connect_firebird(fb_cfg)
        _detect_nametypes(con)

        slips, new_hwm   = _fetch_slips(con, last_slip_id=0 if is_initial else last_slip_id)
        clients           = _fetch_clients(con)     if is_initial else []
        timekeepers       = _fetch_timekeepers(con) if is_initial else []
        invoices          = _fetch_invoices(con, last_slip_id=last_slip_id, is_initial=is_initial)
        payments          = _fetch_payments(con, is_initial=is_initial)
        con.close()
    except Exception as e:
        logger.error(f"Firebird read failed: {e}")
        raise

    if not slips and not is_initial and not invoices and not payments:
        logger.info("Nothing new to sync.")
        return {"status": "no_changes"}

    logger.info(f"Fetched: {len(slips)} slips, {len(clients)} clients, "
               f"{len(timekeepers)} tks, {len(invoices)} invoices, {len(payments)} payments")

    # ── Write to PostgreSQL ───────────────────────────────────────────────
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            _write_to_postgres(
                tenant_id, slips, clients, timekeepers,
                invoices, payments, is_initial, new_hwm
            )
        )
    finally:
        loop.close()

    logger.info(f"Sync complete: {result}")
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="Praesidium — Server-Side Timeslips Sync")
    parser.add_argument("--full", action="store_true", help="Full historical import")
    parser.add_argument("--tenant", default="986c0fee-1390-43bb-ad28-8cd1db6de53f")
    args = parser.parse_args()

    mode = "full" if args.full else "incremental"
    result = run_timeslips_sync(tenant_id=args.tenant, mode=mode)
    print(json.dumps(result, indent=2))
