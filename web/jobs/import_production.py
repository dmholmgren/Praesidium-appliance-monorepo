"""
jobs/import_production.py
Component 8 — Production Import — RQ job
Patent Pending — 64/020,027

Fault-tolerant load file ingestion:
  - Parses DAT (Concordance), CSV, OPT, LFP load files
  - Creates production_rows records with raw_row preserved
  - Applies field_map to populate mapped_row
  - Matches documents to ediscovery_documents via Bates or file hash
  - Row-level fault isolation: one bad row never stops the job
  - Updates production status/counts throughout
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("praesidium.jobs.import_production")


def run(production_id: str, tenant_id: str) -> dict:
    """
    RQ job entry point.
    Called by production_import.py after status set to 'running'.
    """
    import psycopg2

    DATABASE_URL = os.environ.get("DATABASE_URL", "")
    db_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

    conn = None
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = False

        production = _load_production(conn, production_id, tenant_id)
        if not production:
            log.error("Production %s not found for tenant %s", production_id, tenant_id)
            return {"error": "not_found"}

        load_file_path = production["load_file_path"]
        if not load_file_path or not os.path.exists(load_file_path):
            _mark_failed(conn, production_id, "Load file not found on disk")
            return {"error": "load_file_missing"}

        field_map = production["field_map"] or {}
        if isinstance(field_map, str):
            field_map = json.loads(field_map)

        fmt = production["load_file_format"] or "csv"

        # Parse load file into rows
        try:
            rows = _parse_load_file(load_file_path, fmt)
        except Exception as exc:
            _mark_failed(conn, production_id, f"Parse error: {exc}")
            return {"error": f"parse_error: {exc}"}

        total = len(rows)
        log.info("Production %s: parsed %d rows from %s", production_id, total, fmt)

        # Update total count
        cur = conn.cursor()
        cur.execute(
            "UPDATE productions SET row_count_total = %s WHERE id = %s",
            (total, production_id),
        )
        conn.commit()

        # ── Row-level ingestion ───────────────────────────────────────────────
        imported = 0
        failed = 0
        skipped = 0

        for row_number, raw_row in enumerate(rows, start=1):
            try:
                result = _process_row(
                    conn=conn,
                    production_id=production_id,
                    tenant_id=tenant_id,
                    row_number=row_number,
                    raw_row=raw_row,
                    field_map=field_map,
                )
                if result == "imported":
                    imported += 1
                elif result == "skipped":
                    skipped += 1
                else:
                    failed += 1
            except Exception as exc:
                log.error("Row %d failed (non-fatal): %s", row_number, exc)
                failed += 1
                _record_row_error(conn, production_id, tenant_id, row_number, raw_row, str(exc))

            # Checkpoint every 100 rows
            if row_number % 100 == 0:
                _update_counts(conn, production_id, imported, failed, skipped)
                log.info(
                    "Production %s: %d/%d rows processed",
                    production_id, row_number, total,
                )

        # Final count update and status
        _update_counts(conn, production_id, imported, failed, skipped)

        if failed == 0 and skipped == 0:
            final_status = "complete"
        elif imported == 0:
            final_status = "failed"
        else:
            final_status = "partial"

        cur = conn.cursor()
        cur.execute(
            """
            UPDATE productions
            SET status = %s, completed_at = %s
            WHERE id = %s
            """,
            (final_status, datetime.now(timezone.utc), production_id),
        )
        conn.commit()

        log.info(
            "Production %s complete: status=%s imported=%d failed=%d skipped=%d",
            production_id, final_status, imported, failed, skipped,
        )
        return {
            "status": final_status,
            "imported": imported,
            "failed": failed,
            "skipped": skipped,
            "total": total,
        }

    except Exception as exc:
        log.error("Production job failed for %s: %s", production_id, exc)
        if conn:
            try:
                conn.rollback()
                _mark_failed(conn, production_id, str(exc)[:500])
            except Exception as inner:
                log.error("Failed to mark production failed: %s", inner)
        return {"error": str(exc)}

    finally:
        if conn:
            conn.close()


# ── Load file parsing ─────────────────────────────────────────────────────────

def _parse_load_file(path: str, fmt: str) -> list[dict]:
    """
    Parse load file into a list of dicts (one per document row).
    Supports: dat (Concordance), csv, opt, lfp.
    Header row consumed — not included in output.
    """
    with open(path, "rb") as f:
        raw = f.read()

    text_data = raw.decode("utf-8", errors="replace")

    if fmt == "dat":
        return _parse_dat(text_data)
    elif fmt in ("opt", "lfp"):
        return _parse_opt(text_data)
    else:
        # csv, dii, concordance — treat as CSV
        return _parse_csv(text_data)


def _parse_dat(text_data: str) -> list[dict]:
    """
    Concordance DAT format:
    þ (0xFE) field delimiter, ÿ (0xFF) quote character, \n row delimiter.
    First row is the header.
    """
    lines = [l for l in text_data.split("\n") if l.strip()]
    if not lines:
        return []

    def split_dat_line(line: str) -> list[str]:
        # Replace Concordance delimiters with safe tokens
        line = line.replace("\xff", "").replace("\xfe", "\x01")
        return [f.strip() for f in line.split("\x01")]

    headers = split_dat_line(lines[0])
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        values = split_dat_line(line)
        # Pad short rows, truncate long rows
        while len(values) < len(headers):
            values.append("")
        row = dict(zip(headers, values[:len(headers)]))
        rows.append(row)
    return rows


def _parse_csv(text_data: str) -> list[dict]:
    """Standard CSV with header row."""
    reader = csv.DictReader(io.StringIO(text_data))
    rows = []
    for row in reader:
        rows.append(dict(row))
    return rows


def _parse_opt(text_data: str) -> list[dict]:
    """
    Opticon OPT / LFP image load file.
    Format: DOCID,VOLUME,PATH,FIRST_PAGE_FLAG,FOLDER,BOX,PAGE_COUNT
    No header row — fixed positional columns.
    """
    columns = ["doc_id", "volume", "path", "first_page_flag", "folder", "box", "page_count"]
    rows = []
    for line in text_data.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        row = {}
        for i, col in enumerate(columns):
            row[col] = parts[i].strip() if i < len(parts) else ""
        rows.append(row)
    return rows


# ── Row processing ────────────────────────────────────────────────────────────

def _process_row(
    conn,
    production_id: str,
    tenant_id: str,
    row_number: int,
    raw_row: dict,
    field_map: dict,
) -> str:
    """
    Process one load file row.
    Returns: 'imported', 'skipped', or 'failed'.
    Raises on unexpected errors — caller handles fault isolation.
    """
    # Apply field map
    mapped_row = _apply_field_map(raw_row, field_map)

    bates_begin = mapped_row.get("bates_begin") or ""
    bates_end = mapped_row.get("bates_end") or ""

    if not bates_begin:
        # No Bates number — skip row (log as skipped, not failed)
        _insert_row(
            conn, production_id, tenant_id, row_number,
            bates_begin, bates_end, raw_row, mapped_row,
            status="skipped",
            error_message="No bates_begin value after field mapping",
            document_id=None,
        )
        return "skipped"

    # Try to match to an existing ediscovery_document
    document_id = _find_document(conn, tenant_id, bates_begin, mapped_row)

    _insert_row(
        conn, production_id, tenant_id, row_number,
        bates_begin, bates_end, raw_row, mapped_row,
        status="imported",
        error_message=None,
        document_id=document_id,
    )
    return "imported"


def _apply_field_map(raw_row: dict, field_map: dict) -> dict:
    """
    Apply field_map to raw_row.
    field_map: {target_field: source_column}
    e.g. {"bates_begin": "BEGDOC", "bates_end": "ENDDOC", "author": "FROM"}
    """
    mapped = {}
    for target_field, source_col in field_map.items():
        if source_col and source_col in raw_row:
            mapped[target_field] = raw_row[source_col]
        else:
            mapped[target_field] = ""
    return mapped


def _find_document(conn, tenant_id: str, bates_begin: str, mapped_row: dict) -> Optional[str]:
    """
    Attempt to match production row to existing ediscovery_document.
    Strategy 1: bates_begin match on ediscovery_documents.bates_begin column (if exists).
    Strategy 2: md5/sha hash match.
    Returns document_id string or None.
    """
    cur = conn.cursor()

    # Strategy 1: Bates match
    try:
        cur.execute(
            """
            SELECT id FROM ediscovery_documents
            WHERE tenant_id = %s AND bates_begin = %s
            LIMIT 1
            """,
            (tenant_id, bates_begin),
        )
        row = cur.fetchone()
        if row:
            return row[0]
    except Exception:
        # bates_begin column may not exist on ediscovery_documents yet
        pass

    # Strategy 2: Hash match
    md5 = mapped_row.get("md5_hash") or ""
    if md5:
        try:
            cur.execute(
                """
                SELECT id FROM ediscovery_documents
                WHERE tenant_id = %s AND file_hash = %s
                LIMIT 1
                """,
                (tenant_id, md5),
            )
            row = cur.fetchone()
            if row:
                return row[0]
        except Exception:
            pass

    return None


def _insert_row(
    conn,
    production_id: str,
    tenant_id: str,
    row_number: int,
    bates_begin: str,
    bates_end: str,
    raw_row: dict,
    mapped_row: dict,
    status: str,
    error_message: Optional[str],
    document_id: Optional[str],
) -> None:
    row_id = str(uuid.uuid4())
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO production_rows (
            id, tenant_id, production_id, row_number,
            bates_begin, bates_end, raw_row, mapped_row,
            document_id, status, error_message, processed_at
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, NOW()
        )
        """,
        (
            row_id,
            tenant_id,
            production_id,
            row_number,
            bates_begin or None,
            bates_end or None,
            json.dumps(raw_row),
            json.dumps(mapped_row),
            document_id,
            status,
            error_message,
        ),
    )
    conn.commit()


def _record_row_error(
    conn,
    production_id: str,
    tenant_id: str,
    row_number: int,
    raw_row: dict,
    error_message: str,
) -> None:
    """Record a row that failed with an unexpected exception."""
    try:
        conn.rollback()
        _insert_row(
            conn, production_id, tenant_id, row_number,
            "", "", raw_row, {},
            status="failed",
            error_message=error_message[:500],
            document_id=None,
        )
    except Exception as exc:
        log.error("Could not record row error for row %d: %s", row_number, exc)


# ── Production state helpers ──────────────────────────────────────────────────

def _load_production(conn, production_id: str, tenant_id: str) -> Optional[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, tenant_id, load_file_path, load_file_format,
               field_map, status
        FROM productions
        WHERE id = %s AND tenant_id = %s
        """,
        (production_id, tenant_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "tenant_id": row[1].strip() if row[1] else "",
        "load_file_path": row[2],
        "load_file_format": row[3],
        "field_map": row[4],
        "status": row[5],
    }


def _update_counts(conn, production_id: str, imported: int, failed: int, skipped: int) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE productions
        SET row_count_imported = %s,
            row_count_failed = %s,
            row_count_skipped = %s
        WHERE id = %s
        """,
        (imported, failed, skipped, production_id),
    )
    conn.commit()


def _mark_failed(conn, production_id: str, error_message: str) -> None:
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE productions
            SET status = 'failed',
                error_message = %s,
                completed_at = %s
            WHERE id = %s
            """,
            (error_message[:500], datetime.now(timezone.utc), production_id),
        )
        conn.commit()
    except Exception as exc:
        log.error("Could not mark production failed: %s", exc)
