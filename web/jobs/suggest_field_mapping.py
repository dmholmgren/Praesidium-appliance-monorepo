"""
jobs/suggest_field_mapping.py
Component 8 — Production Import — AI Field Mapping RQ job
Patent Pending — 64/020,027

Uses Claude to analyze load file column headers and suggest the best
mapping to Praesidium's standard target fields. Result written back to
productions.field_map as a suggested (unconfirmed) mapping.
Attorney must confirm via the field_mapping UI before import can start.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger("praesidium.jobs.suggest_field_mapping")

# Standard Praesidium target fields with descriptions for AI context
TARGET_FIELDS = {
    "bates_begin": "Beginning Bates number / document control number (e.g. ABC00001)",
    "bates_end": "Ending Bates number for multi-page documents (e.g. ABC00005)",
    "doc_date": "Document date — creation, sent, or received date",
    "author": "Author, sender, or From field",
    "recipients": "Recipients, To, CC, or BCC fields",
    "subject": "Subject line or document title",
    "custodian": "Custodian — the person whose files the document came from",
    "doc_type": "Document type or file type (email, contract, memo, etc.)",
    "file_path": "Path to native file",
    "text_path": "Path to extracted text file",
    "confidentiality": "Confidentiality designation or privilege assertion",
    "md5_hash": "MD5 or SHA hash of the document file",
}


def run(production_id: str, tenant_id: str) -> dict:
    """
    RQ job entry point.
    1. Load production record and read first 4KB of load file for headers
    2. Call AI to suggest field mapping
    3. Write suggested mapping back to productions.field_map
    Returns the suggested mapping dict.
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
            log.error("Production %s not found", production_id)
            return {"error": "not_found"}

        load_file_path = production["load_file_path"]
        fmt = production["load_file_format"] or "csv"

        if not load_file_path or not os.path.exists(load_file_path):
            log.error("Load file not found: %s", load_file_path)
            return {"error": "load_file_missing"}

        # Sniff column headers from load file
        columns = _sniff_columns(load_file_path, fmt)
        if not columns:
            log.warning("No columns detected in load file %s", load_file_path)
            return {"error": "no_columns_detected"}

        log.info(
            "Production %s: suggesting mapping for %d columns: %s",
            production_id, len(columns), columns,
        )

        # Get AI suggestion
        suggested_map = _get_ai_suggestion(
            columns=columns,
            fmt=fmt,
            production_name=production["production_name"],
        )

        if not suggested_map:
            log.warning("AI suggestion returned empty map for production %s", production_id)
            suggested_map = {}

        # Write suggested map back to productions
        cur = conn.cursor()
        cur.execute(
            "UPDATE productions SET field_map = %s WHERE id = %s",
            (json.dumps(suggested_map), production_id),
        )
        conn.commit()

        log.info(
            "Production %s: AI mapping complete — %d fields mapped",
            production_id, len([v for v in suggested_map.values() if v]),
        )
        return {"status": "complete", "field_map": suggested_map, "columns_detected": columns}

    except Exception as exc:
        log.error("suggest_field_mapping failed for %s: %s", production_id, exc)
        return {"error": str(exc)}

    finally:
        if conn:
            conn.close()


# ── Column sniffing ───────────────────────────────────────────────────────────

def _sniff_columns(path: str, fmt: str) -> list[str]:
    """Read first line of load file and extract column headers."""
    try:
        with open(path, "rb") as f:
            raw = f.read(8192)
        text_data = raw.decode("utf-8", errors="replace")
        first_line = text_data.split("\n")[0]

        if fmt == "dat":
            # Concordance DAT: þ (0xFE) delimiter, ÿ (0xFF) quote
            cols = first_line.replace("\xff", "").replace("\xfe", "\x01").split("\x01")
            return [c.strip() for c in cols if c.strip()]
        elif fmt in ("opt", "lfp"):
            # OPT/LFP: fixed positional — return known column names
            return ["doc_id", "volume", "path", "first_page_flag", "folder", "box", "page_count"]
        else:
            import csv, io
            reader = csv.reader(io.StringIO(first_line))
            for row in reader:
                return [c.strip() for c in row if c.strip()]
    except Exception as exc:
        log.warning("Column sniff failed: %s", exc)
    return []


# ── AI suggestion ─────────────────────────────────────────────────────────────

def _get_ai_suggestion(
    columns: list[str],
    fmt: str,
    production_name: str,
) -> dict:
    """
    Call Claude via AIService to suggest field mapping.
    Falls back to rule-based mapping if AI is unavailable.
    Returns {target_field: source_column} dict.
    """
    try:
        suggested = _ai_call(columns, fmt, production_name)
        if suggested:
            return suggested
    except Exception as exc:
        log.warning("AI call failed, falling back to rule-based mapping: %s", exc)

    return _rule_based_mapping(columns)


def _ai_call(columns: list[str], fmt: str, production_name: str) -> Optional[dict]:
    """
    Direct Anthropic API call for field mapping suggestion.
    Returns parsed dict or None on failure.
    """
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set — skipping AI call")
        return None

    client = anthropic.Anthropic(api_key=api_key)

    target_descriptions = "\n".join(
        f"  - {field}: {desc}" for field, desc in TARGET_FIELDS.items()
    )

    prompt = f"""You are analyzing a litigation document production load file.

Production name: {production_name}
Load file format: {fmt}
Detected columns in the load file: {json.dumps(columns)}

Target fields to map to:
{target_descriptions}

Task: Map each target field to the most appropriate source column from the detected columns list.
- Only map a target field if you are confident there is a matching source column.
- Leave unmapped fields as null.
- bates_begin is the most critical field — prioritize finding it.
- Return ONLY a JSON object with this exact structure:
  {{"bates_begin": "SOURCE_COL_OR_NULL", "bates_end": "SOURCE_COL_OR_NULL", "doc_date": "SOURCE_COL_OR_NULL", "author": "SOURCE_COL_OR_NULL", "recipients": "SOURCE_COL_OR_NULL", "subject": "SOURCE_COL_OR_NULL", "custodian": "SOURCE_COL_OR_NULL", "doc_type": "SOURCE_COL_OR_NULL", "file_path": "SOURCE_COL_OR_NULL", "text_path": "SOURCE_COL_OR_NULL", "confidentiality": "SOURCE_COL_OR_NULL", "md5_hash": "SOURCE_COL_OR_NULL"}}

Return only the JSON object. No explanation, no markdown, no preamble."""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = message.content[0].text.strip()

    # Strip markdown fences if present
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    suggested = json.loads(raw_text)

    # Validate: only keep entries where value is a real column
    validated = {}
    for target_field, source_col in suggested.items():
        if target_field in TARGET_FIELDS:
            if source_col and source_col in columns:
                validated[target_field] = source_col
            else:
                validated[target_field] = None

    return validated


def _rule_based_mapping(columns: list[str]) -> dict:
    """
    Fallback rule-based field mapping when AI is unavailable.
    Matches common Concordance/Relativity/Logikcull column names.
    """
    col_lower = {c.lower(): c for c in columns}

    rules = {
        "bates_begin": ["begdoc", "beg_doc", "beg bates", "begbates", "docid",
                        "doc_id", "control_number", "control number", "begno"],
        "bates_end":   ["enddoc", "end_doc", "end bates", "endbates", "endno"],
        "doc_date":    ["docdate", "doc_date", "date", "sent", "date sent",
                        "date_sent", "createdate", "create_date"],
        "author":      ["from", "author", "createby", "created_by", "sender"],
        "recipients":  ["to", "recipients", "cc", "bcc", "recipient"],
        "subject":     ["subject", "title", "re", "description"],
        "custodian":   ["custodian", "custodians", "owner"],
        "doc_type":    ["doctype", "doc_type", "filetype", "file_type",
                        "type", "file type"],
        "file_path":   ["native_file", "nativefile", "native file path",
                        "nativepath", "file_path", "filepath"],
        "text_path":   ["text_path", "textpath", "extracted_text",
                        "textfile", "text file path"],
        "confidentiality": ["confidentiality", "privilege", "designation",
                            "priv", "conf"],
        "md5_hash":    ["md5", "md5hash", "hash", "file_hash", "filehash"],
    }

    result = {}
    for target_field, candidates in rules.items():
        matched = None
        for candidate in candidates:
            if candidate in col_lower:
                matched = col_lower[candidate]
                break
        result[target_field] = matched

    return result


# ── Production loader ─────────────────────────────────────────────────────────

def _load_production(conn, production_id: str, tenant_id: str) -> Optional[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, tenant_id, production_name, load_file_path,
               load_file_format, field_map
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
        "production_name": row[2],
        "load_file_path": row[3],
        "load_file_format": row[4],
        "field_map": row[5],
    }
