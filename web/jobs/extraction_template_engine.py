"""
jobs/extraction_template_engine.py

Praesidium Extraction Template Engine — Three-Tier Parser

Reads extraction_template JSONB from parsing_strategy_rules,
applies regex → structural → AI escalation extractors,
writes results to primitive tables:
  document_sections, document_parties, document_defined_terms,
  document_deadlines, causes_of_action, document_metadata

Each extracted row links to extraction_run_id for audit/versioning.
Superseded rows are never deleted — superseded_by_run_id marks old data.

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg2
from psycopg2.extras import Json

log = logging.getLogger("praesidium.jobs.extraction_template_engine")


# ═══════════════════════════════════════════════════════════════════════════════
# DB CONNECTION (same pattern as ingestion_pipeline.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _get_db_conn():
    raw = os.environ.get("DATABASE_URL", "")
    url = raw.replace("postgresql+asyncpg://", "postgresql://")
    at = url.rfind("@")
    rest = url[at + 1:]
    userinfo = url[len("postgresql://"):at]
    colon = userinfo.rfind(":")
    user = userinfo[:colon]
    password = userinfo[colon + 1:]
    slash = rest.find("/")
    hostport = rest[:slash]
    dbname = rest[slash + 1:].split("?")[0]
    if ":" in hostport:
        host, port = hostport.rsplit(":", 1)
    else:
        host, port = hostport, "5432"
    return psycopg2.connect(
        host=host, port=int(port), dbname=dbname, user=user, password=password,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# CATEGORY → SPEC DOCUMENT TYPE MAPPING
# Maps taxonomy categories/codes to extraction template document_type keys
# in parsing_strategy_rules
# ═══════════════════════════════════════════════════════════════════════════════

TAXONOMY_TO_PARSE_TYPE = {
    # ── Pleadings (petitions, answers, counterclaims) ───────────────
    "pleading": "pleading",
    "petition": "pleading",
    "amended_petition": "pleading",
    "answer": "pleading",
    "counterclaim": "pleading",
    "cross_claim": "pleading",
    "third_party_petition": "pleading",
    "intervention": "pleading",
    "interpleader": "pleading",

    # ── Motions → own template ──────────────────────────────────────
    "motion": "motion",
    "motion_compel": "motion",
    "motion_continuance": "motion",
    "motion_dismiss": "motion",
    "motion_protective_order": "motion",
    "motion_quash": "motion",
    "motion_summary_judgment": "motion",
    "motion_strike": "motion",
    "motion_transfer": "motion",
    "motion_sever": "motion",
    "motion_consolidate": "motion",
    "motion_reconsider": "motion",
    "motion_exclude": "motion",
    "motion_sanctions": "motion",
    "motion_default_judgment": "motion",
    "motion_new_trial": "motion",
    "motion_jnov": "motion",
    "motion_bifurcate": "motion",
    "motion_stay": "motion",
    "motion_intervene": "motion",
    "motion_amend": "motion",

    # ── Briefs / Responses → own template ───────────────────────────
    "reply_brief": "brief",
    "response_to_motion": "brief",
    "sur_reply": "brief",
    "memorandum_of_law": "brief",
    "trial_brief": "brief",
    "appellate_brief": "brief",

    # ── Court Orders → own template ─────────────────────────────────
    "court_order": "court_order",
    "ruling": "court_order",
    "agreed_order": "court_order",
    "protective_order": "court_order",
    "temporary_restraining_order": "court_order",

    # ── Judgments → own template ────────────────────────────────────
    "judgment": "judgment",
    "final_judgment": "judgment",
    "partial_judgment": "judgment",
    "default_judgment": "judgment",
    "summary_judgment": "judgment",
    "consent_judgment": "judgment",

    # ── Scheduling / docket control orders ──────────────────────────
    "scheduling_order": "scheduling_order",
    "docket_control_order": "scheduling_order",
    "case_management_order": "scheduling_order",

    # ── Affidavits / Declarations → own template ────────────────────
    "affidavit": "affidavit",
    "declaration": "affidavit",
    "sworn_statement": "affidavit",
    "verification": "affidavit",

    # ── Expert Reports → own template ───────────────────────────────
    "expert_report": "expert_report",
    "expert_cv": None,  # basic metadata only

    # ── Discovery → own template ────────────────────────────────────
    "discovery_request": "discovery",
    "interrogatories": "discovery",
    "request_admissions": "discovery",
    "request_production": "discovery",

    # ── Discovery Responses → own template ──────────────────────────
    "discovery_response": "discovery_response",
    "interrogatory_answers": "discovery_response",
    "rfa_responses": "discovery_response",
    "rfp_responses": "discovery_response",

    # ── Subpoenas → own template ────────────────────────────────────
    "subpoena": "subpoena",
    "subpoena_duces_tecum": "subpoena",
    "deposition_notice": "subpoena",

    # ── Depositions ─────────────────────────────────────────────────
    "deposition_transcript": "deposition",

    # ── Disclosures → own template ──────────────────────────────────
    "disclosure": "disclosure",
    "initial_disclosure": "disclosure",
    "supplemental_disclosure": "disclosure",
    "rule_194_disclosure": "disclosure",
    "rule_26a_disclosure": "disclosure",

    # ── Correspondence → own template ───────────────────────────────
    "correspondence": "correspondence",
    "client_letter": "correspondence",
    "court_letter": "correspondence",
    "demand_letter": "correspondence",
    "opposing_counsel_letter": "correspondence",
    "settlement_letter": "correspondence",

    # ── Contracts ───────────────────────────────────────────────────
    "contract": "contract",
    "assignment": "contract",
    "employment_agreement": "contract",
    "guaranty": "contract",
    "indemnity_agreement": "contract",
    "lease": "contract",
    "loan_agreement": "contract",
    "master_service_agreement": "contract",
    "nda": "contract",
    "purchase_agreement": "loi",
    "settlement_agreement": "contract",
    "mediated_settlement": "contract",

    # ── Real Estate ─────────────────────────────────────────────────
    "mortgage": "real_estate",
    "promissory_note": "real_estate",
    "deed": "real_estate",
    "deed_of_trust": "real_estate",
    
    # ── LOI / Purchase Agreement → property extraction ──────────
    "loi": "loi",
    "letter_of_intent": "loi",
    
    # ── LOI / Purchase Agreement → property extraction ──────────
    "loi": "loi",
    "letter_of_intent": "loi",

    # ── Email ───────────────────────────────────────────────────────
    "email": "email",
    "email_chain": "email",

    # ── Financial ───────────────────────────────────────────────────
    "financial_statement": "financial_statement",
    "invoice": "invoice",
    "tax_return": "financial_statement",

    # ── Corporate ───────────────────────────────────────────────────
    "articles_incorporation": "corporate",
    "bylaws": "corporate",
    "certificate": "corporate",
    "corporate_doc": "corporate",
    "resolution": "corporate",

    # ── Data / Media / Unknown ──────────────────────────────────────
    "spreadsheet": None,
    "photo_image": None,
    "unknown": None,
}


# ═══════════════════════════════════════════════════════════════════════════════
# DATE PARSING UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date_str(text: str, dayfirst: bool = False) -> str | None:
    """Parse a date string into ISO format (YYYY-MM-DD). Returns None on failure.

    Ambiguous numeric slash dates (both parts <= 12) are controlled by
    `dayfirst`: False -> M/D/Y (US default), True -> D/M/Y. When one part is
    > 12 the order is unambiguous and `dayfirst` is ignored. Impossible dates
    (e.g. 2/30) return None rather than producing an invalid ISO string."""
    if not text:
        return None
    text = text.strip().rstrip(".,;")

    # Numeric slash format: A/B/YYYY or A/B/YY.
    # Disambiguate day vs month with the >12 rule; only fall back to `dayfirst`
    # when both components are <= 12. NOTE (verified against live corpus):
    # Spanish-language docs here are overwhelmingly US M/D/Y (US-counterparty
    # trade docs, ~1144 M/D vs ~23 D/M per 2000 sampled) — do NOT flip on
    # detected_language.
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", text)
    if m:
        a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000 if year < 50 else 1900
        if a > 12 and b <= 12:
            day, month = a, b            # unambiguous D/M/Y
        elif b > 12 and a <= 12:
            month, day = a, b            # unambiguous M/D/Y
        elif a <= 12 and b <= 12:
            month, day = (b, a) if dayfirst else (a, b)
        else:
            return None                  # both > 12 — not a valid date
        try:
            datetime(year, month, day)   # reject impossible dates (2/30, 4/31)
            return f"{year:04d}-{month:02d}-{day:02d}"
        except (ValueError, OverflowError):
            return None

    # Try "Month DD, YYYY" or "Month DD YYYY"
    m = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", text)
    if m:
        month_name = m.group(1).lower().rstrip(".")
        month = MONTH_MAP.get(month_name)
        if month:
            day, year = int(m.group(2)), int(m.group(3))
            try:
                return f"{year:04d}-{month:02d}-{day:02d}"
            except (ValueError, OverflowError):
                pass

    # Try "DD Month YYYY"
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\.?,?\s+(\d{4})", text)
    if m:
        month_name = m.group(2).lower().rstrip(".")
        month = MONTH_MAP.get(month_name)
        if month:
            day, year = int(m.group(1)), int(m.group(3))
            try:
                return f"{year:04d}-{month:02d}-{day:02d}"
            except (ValueError, OverflowError):
                pass

    # ISO format already
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return m.group(0)

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# CORE ENGINE: run_extraction_template()
# ═══════════════════════════════════════════════════════════════════════════════

def run_extraction_template(
    tenant_id: str,
    document_id: str,
    run_id: str,
    content_text: str,
    document_type_code: str,
    document_type_id: str | None = None,
    source_table: str = "dms",         # "dms" or "ediscovery"
    matter_id: str | None = None,
    file_path: str | None = None,
) -> dict:
    """
    Main entry point. Looks up the parsing_strategy_rule for the document_type,
    runs all field extractors (regex → structural), writes results to
    primitive tables, returns extraction summary.

    If no parsing_strategy_rule exists, falls back to basic metadata only.
    """
    tid = (tenant_id or "").strip()
    doc_col = "dms_document_id" if source_table == "dms" else "ediscovery_document_id"
    other_col = "ediscovery_document_id" if source_table == "dms" else "dms_document_id"

    # Resolve the parse type from taxonomy code
    parse_type = TAXONOMY_TO_PARSE_TYPE.get(document_type_code)
    if not parse_type:
        log.info("No extraction template for type %s, basic metadata only", document_type_code)
        return _extract_basic_metadata(
            tid, document_id, run_id, content_text, doc_col, other_col, file_path,
        )

    conn = _get_db_conn()
    conn.autocommit = False
    cur = conn.cursor()

    try:
        # Look up parsing_strategy_rule
        cur.execute(
            """SELECT id, extraction_template, section_delimiters,
                      times_applied, times_succeeded
               FROM parsing_strategy_rules
               WHERE document_type = %s AND is_active = true
               ORDER BY success_rate DESC NULLS LAST, created_at DESC
               LIMIT 1""",
            (parse_type,),
        )
        rule_row = cur.fetchone()

        if not rule_row or not rule_row[1]:
            log.info("No parsing_strategy_rule for %s, basic metadata only", parse_type)
            cur.close()
            conn.close()
            return _extract_basic_metadata(
                tid, document_id, run_id, content_text, doc_col, other_col, file_path,
            )

        rule_id, template_json, section_delimiters, times_applied, times_succeeded = rule_row
        template = template_json if isinstance(template_json, dict) else json.loads(template_json)

        # Apply text cap
        text_cap = template.get("text_cap", 200000)
        text = content_text[:text_cap] if content_text else ""

        # Supersede any prior extraction for this document
        _supersede_prior_extractions(cur, tid, document_id, run_id, doc_col)

        # Track results
        results = {
            "sections": 0,
            "parties": 0,
            "defined_terms": 0,
            "deadlines": 0,
            "causes_of_action": 0,
            "citations": 0,
            "pii": 0,
            "fields_extracted": 0,
            "fields_failed": [],
            "extraction_method": "template_regex",
        }

        # ── SECTION PARSER ──────────────────────────────────────────────
        section_parser = template.get("section_parser", {})
        sections_map = {}   # section_index → section_uuid (for FK references)

        if section_parser.get("delimiter_pattern"):
            sections_map = _extract_sections(
                cur, tid, document_id, run_id, text, section_parser,
                doc_col, other_col, parse_type,
            )
            results["sections"] = len(sections_map)

        # ── FIELD EXTRACTORS ────────────────────────────────────────────
        fields = template.get("fields", [])
        missing_required = []

        for field_def in fields:
            name = field_def.get("name", "")
            target_table = field_def.get("target_table")
            method = field_def.get("extraction_method", "regex")
            required = field_def.get("required", False)
            escalate = field_def.get("escalate_if_missing", False)

            # Skip section-type fields (handled by section_parser above)
            if name in ("numbered_paragraphs", "sections", "qa_pairs") and sections_map:
                continue

            extracted = False

            if method == "regex" and field_def.get("pattern"):
                extracted = _run_regex_field(
                    cur, tid, document_id, run_id, text,
                    field_def, doc_col, other_col, sections_map, matter_id,
                )
            elif method == "structural":
                extracted = _run_structural_field(
                    cur, tid, document_id, run_id, text,
                    field_def, doc_col, other_col, sections_map, matter_id,
                )

            if extracted:
                results["fields_extracted"] += 1
                if target_table == "document_parties":
                    results["parties"] += 1 if isinstance(extracted, bool) else extracted
                elif target_table == "document_deadlines":
                    results["deadlines"] += 1 if isinstance(extracted, bool) else extracted
                elif target_table == "document_defined_terms":
                    results["defined_terms"] += 1 if isinstance(extracted, bool) else extracted
                elif target_table == "causes_of_action":
                    results["causes_of_action"] += 1 if isinstance(extracted, bool) else extracted
            elif required:
                missing_required.append(name)
                if escalate:
                    results["fields_failed"].append(name)

        # ── CITATIONS / AUTHORITIES (every classified doc type) ─────────
        results["citations"] = _extract_citations_structural(
            cur, tid, document_id, run_id, text, doc_col,
        )

        # PII (every classified doc type; Tier-1 deterministic)
        results["pii"] = _extract_pii(
            cur, tid, document_id, run_id, text, doc_col,
        )

        # Write basic metadata row too
        _write_metadata(
            cur, tid, document_id, run_id, content_text, doc_col, other_col, file_path,
        )

        # Update parsing_strategy_rule stats
        succeeded = len(missing_required) == 0
        cur.execute(
            """UPDATE parsing_strategy_rules
               SET times_applied = times_applied + 1,
                   times_succeeded = times_succeeded + %s,
                   success_rate = CASE
                     WHEN times_applied + 1 > 0
                     THEN (times_succeeded + %s)::numeric / (times_applied + 1)
                     ELSE NULL END,
                   last_applied_at = NOW(),
                   updated_at = NOW()
               WHERE id = %s""",
            (1 if succeeded else 0, 1 if succeeded else 0, rule_id),
        )

        conn.commit()

        # ── TIER 2 ESCALATION ───────────────────────────────────────────
        escalate_fields = [f for f in results["fields_failed"]]
        if escalate_fields:
            log.info(
                "Tier 1 missing required fields for %s (%s): %s — attempting Tier 2 Haiku",
                document_id, parse_type, escalate_fields,
            )
            try:
                haiku_results = _escalate_to_haiku(
                    tid, document_id, run_id, text, parse_type,
                    escalate_fields, template, doc_col, other_col, matter_id,
                )
                if haiku_results:
                    results["extraction_method"] = "template_regex+haiku"
                    for k, v in haiku_results.items():
                        results[k] = results.get(k, 0) + v
            except Exception as exc:
                log.warning("Tier 2 Haiku escalation failed for %s: %s", document_id, exc)

        log.info(
            "Extraction complete: doc=%s type=%s sections=%d parties=%d terms=%d deadlines=%d",
            document_id, parse_type, results["sections"], results["parties"],
            results["defined_terms"], results["deadlines"],
        )
        return results

    except Exception as exc:
        conn.rollback()
        log.exception("run_extraction_template failed for %s: %s", document_id, exc)
        return {"error": str(exc)}
    finally:
        cur.close()
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# SUPERSEDE PRIOR EXTRACTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _supersede_prior_extractions(cur, tid, document_id, run_id, doc_col):
    """Mark all prior extraction rows for this document as superseded by this run."""
    tables = [
        "document_sections", "document_parties", "document_defined_terms",
        "document_deadlines", "document_metadata", "document_citations",
        "document_entities",
    ]
    for table in tables:
        cur.execute(
            f"""UPDATE {table}
                SET superseded_by_run_id = %s
                WHERE TRIM(tenant_id) = %s
                  AND {doc_col} = %s
                  AND superseded_by_run_id IS NULL
                  AND extraction_run_id != %s""",
            (run_id, tid, document_id, run_id),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION PARSER
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_sections(
    cur, tid, document_id, run_id, text, section_parser, doc_col, other_col, parse_type,
) -> dict:
    """
    Parse document into sections using delimiter_pattern.
    Returns dict of section_index → section_uuid for FK references.
    """
    delimiter_type = section_parser.get("delimiter_type", "numbered")
    pattern = section_parser.get("delimiter_pattern", "")
    if not pattern:
        return {}

    try:
        regex = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        log.warning("Bad section delimiter pattern for %s: %s", parse_type, exc)
        return {}

    matches = list(regex.finditer(text))
    if not matches:
        return {}

    sections_map = {}
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()

        if not content or len(content) < 3:
            continue

        # Extract label from match groups
        label = match.group(1).strip() if match.lastindex and match.lastindex >= 1 else None
        if label and len(label) > 200:
            label = label[:197] + "..."

        # Determine section type based on delimiter_type
        section_type = _section_type_from_delimiter(delimiter_type, parse_type)

        # Calculate page from form feeds
        page_start = text[:start].count("\f") + 1
        page_end = text[:end].count("\f") + 1

        # Nesting depth (basic: count leading whitespace or numbering depth)
        depth = _calc_nesting_depth(content, delimiter_type)

        sec_id = str(uuid.uuid4())
        cur.execute(
            f"""INSERT INTO document_sections
                (id, tenant_id, {doc_col}, extraction_run_id,
                 section_index, section_type, section_label, content,
                 char_start, char_end, page_start, page_end,
                 nesting_depth, extracted_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
            (sec_id, tid, document_id, run_id,
             i, section_type, label, content,
             start, end, page_start, page_end, depth),
        )
        sections_map[i] = sec_id

    return sections_map


def _section_type_from_delimiter(delimiter_type: str, parse_type: str) -> str:
    mapping = {
        "numbered": "numbered_paragraph",
        "article": "contract_section",
        "exhibit": "exhibit",
        "paragraph": "paragraph",
        "qa": "qa_pair",
        "email_thread": "email_message",
    }
    return mapping.get(delimiter_type, f"{parse_type}_section")


def _calc_nesting_depth(content: str, delimiter_type: str) -> int:
    """Basic nesting depth from content structure."""
    if delimiter_type == "article":
        # ARTICLE = 0, Section X.Y = 1, (a)/(b) = 2
        if re.match(r"(?i)^ARTICLE\s", content):
            return 0
        if re.match(r"^\d+\.\d+", content):
            return 1
        if re.match(r"^\s*\([a-z]\)", content):
            return 2
    elif delimiter_type == "numbered":
        # Indentation-based
        stripped = content.lstrip()
        indent = len(content) - len(stripped)
        if indent > 8:
            return 2
        elif indent > 4:
            return 1
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# REGEX FIELD EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════════════

def _run_regex_field(
    cur, tid, document_id, run_id, text, field_def, doc_col, other_col,
    sections_map, matter_id,
) -> int | bool:
    """
    Run a regex pattern against text and write results to the target table.
    Returns count of rows inserted, or False if no match.
    """
    pattern = field_def.get("pattern", "")
    target_table = field_def.get("target_table")
    name = field_def.get("name", "")

    if not pattern:
        return False

    flags = re.MULTILINE | re.DOTALL if "\\Z" in pattern or "^" in pattern else re.MULTILINE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        log.warning("Bad regex for field %s: %s", name, exc)
        return False

    matches = list(regex.finditer(text))
    if not matches:
        return False

    count = 0

    # ── document_sections ───────────────────────────────────────────
    if target_table == "document_sections":
        section_type = field_def.get("section_type", name)
        for i, m in enumerate(matches):
            content = m.group(0).strip()
            label_text = m.group(1).strip() if m.lastindex and m.lastindex >= 1 else None
            if label_text and len(label_text) > 200:
                label_text = label_text[:197] + "..."
            if not content:
                continue
            page_start = text[:m.start()].count("\f") + 1
            cur.execute(
                f"""INSERT INTO document_sections
                    (id, tenant_id, {doc_col}, extraction_run_id,
                     section_index, section_type, section_label, content,
                     char_start, char_end, page_start, nesting_depth, extracted_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, NOW())""",
                (str(uuid.uuid4()), tid, document_id, run_id,
                 len(sections_map) + i, section_type, label_text, content,
                 m.start(), m.end(), page_start),
            )
            count += 1

    # ── document_deadlines ──────────────────────────────────────────
    elif target_table == "document_deadlines":
        deadline_type = field_def.get("deadline_type", name)
        for m in matches:
            raw_date = m.group(1).strip() if m.lastindex and m.lastindex >= 1 else m.group(0).strip()
            parsed = _parse_date_str(raw_date)
            # Find nearby context for deadline_text
            start = max(0, m.start() - 80)
            end = min(len(text), m.end() + 20)
            context = text[start:end].replace("\n", " ").strip()

            cur.execute(
                f"""INSERT INTO document_deadlines
                    (id, tenant_id, {doc_col}, extraction_run_id,
                     extraction_method, deadline_text, deadline_date,
                     confidence, extracted_at)
                    VALUES (%s, %s, %s, %s, 'regex', %s, %s, 0.85, NOW())""",
                (str(uuid.uuid4()), tid, document_id, run_id,
                 context[:500], parsed),
            )
            count += 1

    # ── document_defined_terms ──────────────────────────────────────
    elif target_table == "document_defined_terms":
        for m in matches:
            term = m.group(1).strip() if m.lastindex and m.lastindex >= 1 else m.group(0).strip()
            if not term or len(term) > 500:
                continue
            # Get surrounding context as definition
            def_start = m.end()
            def_end = min(len(text), def_start + 500)
            # Find sentence end
            sent_end = text.find(".", def_start)
            if sent_end > 0 and sent_end < def_end:
                def_end = sent_end + 1
            definition = text[def_start:def_end].strip()
            if not definition:
                definition = "(definition not captured)"

            page_num = text[:m.start()].count("\f") + 1

            cur.execute(
                f"""INSERT INTO document_defined_terms
                    (id, tenant_id, {doc_col}, extraction_run_id,
                     extraction_method, term, definition_text,
                     page_number, char_start, char_end,
                     confidence, extracted_at)
                    VALUES (%s, %s, %s, %s, 'regex', %s, %s, %s, %s, %s, 0.85, NOW())""",
                (str(uuid.uuid4()), tid, document_id, run_id,
                 term[:500], definition[:5000], page_num, m.start(), m.end()),
            )
            count += 1

    # ── document_parties ────────────────────────────────────────────
    elif target_table == "document_parties":
        party_role = field_def.get("party_role", None)
        for m in matches:
            party_name = m.group(1).strip() if m.lastindex and m.lastindex >= 1 else m.group(0).strip()
            if not party_name or len(party_name) < 2:
                continue
            cur.execute(
                f"""INSERT INTO document_parties
                    (id, tenant_id, {doc_col}, extraction_run_id,
                     extraction_method, party_name, confidence, extracted_at)
                    VALUES (%s, %s, %s, %s, 'regex', %s, 0.80, NOW())""",
                (str(uuid.uuid4()), tid, document_id, run_id, party_name[:500]),
            )
            count += 1

    # ── causes_of_action ────────────────────────────────────────────
    elif target_table == "causes_of_action" and matter_id:
        for i, m in enumerate(matches):
            title = m.group(1).strip() if m.lastindex and m.lastindex >= 1 else m.group(0).strip()
            if not title:
                continue
            cur.execute(
                """INSERT INTO causes_of_action
                   (tenant_id, matter_id, title, count_number, status, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, 'not_developed', NOW(), NOW())""",
                (tid, matter_id, title[:500], i + 1),
            )
            count += 1

    # ── No target table (metadata-only field like case_number, court) ──
    elif not target_table:
        # These are captured but not written to a separate table.
        # They can be stored in document_metadata JSONB later.
        return True

    return count if count > 0 else False


# ═══════════════════════════════════════════════════════════════════════════════
# STRUCTURAL FIELD EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════════════

def _run_structural_field(
    cur, tid, document_id, run_id, text, field_def, doc_col, other_col,
    sections_map, matter_id,
) -> int | bool:
    """
    Structural extraction — heuristic parsers for complex fields that can't
    be captured by a single regex. Each document type has specific logic.
    Returns count of rows inserted, or False if nothing extracted.
    """
    name = field_def.get("name", "")
    target_table = field_def.get("target_table")

    # ── Party extraction from caption (pleadings) ───────────────────
    if name == "parties" and target_table == "document_parties":
        return _extract_parties_structural(
            cur, tid, document_id, run_id, text, field_def, doc_col,
        )

    # ── Causes of action from COUNT headers ─────────────────────────
    if name == "causes_of_action" and target_table == "causes_of_action":
        return _extract_causes_structural(
            cur, tid, document_id, run_id, text, matter_id,
        )

    # ── Deadline extraction (scheduling orders, court orders, judgments) ──
    if name in ("deadlines", "compliance_deadlines", "post_judgment_deadlines") and target_table == "document_deadlines":
        return _extract_deadlines_structural(
            cur, tid, document_id, run_id, text, doc_col,
        )

    # ── Party extraction from caption (pleadings, motions, orders) ──

    # ── Defined term usage scanning ─────────────────────────────────
    if name == "defined_term_usage":
        return _scan_defined_term_usage(cur, tid, document_id, run_id, text, doc_col, sections_map)

    # ── Email body extraction ───────────────────────────────────────
    if name == "body" and target_table == "email_routing_queue":
        # Email body is handled by the email extractor in ingestion_pipeline
        return False

    # ── Financial line items ────────────────────────────────────────
    if name == "line_items":
        return _extract_financial_line_items(
            cur, tid, document_id, run_id, text, doc_col,
        )

    # ── Contract sections (ARTICLE/Section hierarchy) ───────────────
    if name == "sections" and target_table == "document_sections" and sections_map:
        # Already handled by section_parser
        return len(sections_map) if sections_map else False

    # ── Generic structural: Haiku needed ────────────────────────────
    log.debug("Structural field %s not implemented for Tier 1, will escalate", name)
    return False


# ─── PARTY EXTRACTION (STRUCTURAL) ─────────────────────────────────────────

# parties-extractor v2
# parties-extractor v3
def _extract_parties_structural(cur, tid, document_id, run_id, text, field_def, doc_col) -> int:
    """Extract parties from a litigation caption, narrative intro, contract
    recital, or counsel block.

    v3 precision pass: real docket sheets and orders repeat role labels
    throughout the body, so role-anchoring over a wide zone over-extracts
    badly. We (a) cap the role-anchored scan at the first decree/docket
    marker, (b) skip runaway blobs, and (c) gate each candidate against
    decree / venue / doc-title noise. Recall on docket sheets and
    order-quoting letters is intentionally sacrificed for precision.

    Litigation in this corpus is US (TX/CA courts); the Spanish-language docs
    are trade invoices, not pleadings.
    """
    found = []  # (name, confidence)

    zone = _clean_caption_zone(text, limit=4000)

    # Caption ends at the first body/decree/docket marker; only scan above it.
    body = re.search(
        r"(?i)\b(?:comes?\s+now|now\s+comes|to\s+the\s+honorable|"
        r"reporter'?s|preliminary\s+statement|table\s+of\s+contents|"
        r"now\s+into\s+court|now,?\s+therefore|"
        r"it\s+is\s+(?:hereby\s+)?ordered|ordered,?\s+adjudged|"
        r"\badjudged\b|\bdecreed\b|date\s+filed|nature\s+of\s+suit|"
        r"highest\s+offense|attorney\s+appearance|document\s+filed|"
        r"dktrpt|cause:)",
        zone,
    )
    cap = body.start() if body else len(zone)
    caption_scan = zone[:cap]

    # Strategy 1: role-label anchored (columned + multi-line captions)
    prev_end = 0
    for m in _ROLE_RE.finditer(caption_scan):
        label = m.group(1).lower()
        blob = _isolate_name_blob(caption_scan[prev_end:m.start()])
        prev_end = m.end()
        if not blob or len(blob) > 200:        # runaway blob = decree prose
            continue
        conf = 0.82 if _is_plaintiff_label(label) else 0.78
        for party in _split_party_names(blob):
            found.append((party, conf))

    # Strategy 2: narrative intro (always runs; deduped downstream)
    mp = re.search(
        r"(?i)comes?\s+now\s+(.{3,160}?)\s*,?\s*(?:plaintiff|petitioner)", zone,
    )
    if mp:
        for party in _split_party_names(mp.group(1)):
            found.append((party, 0.72))
    md = re.search(
        r"(?i)(?:complaining\s+of|against)\s+(?:defendants?|respondents?)\s+"
        r"(.{3,200}?)(?:\(|\bin\s+support\b|\bwould\b|;|$)",
        zone,
    )
    if md:
        for party in _split_party_names(md.group(1)):
            found.append((party, 0.70))

    # Strategy 3: contract recital (fallback)
    if not found:
        cm = re.search(
            r"(?i)(?:by\s+and\s+between|between)\s+(.+?)\s+and\s+"
            r"(.+?)(?:\s*\(|\s*,?\s*(?:a|an)\s|\s*,?\s*whose|\s*\.)",
            text[:3000],
        )
        if cm:
            for grp in (1, 2):
                for party in _split_party_names(cm.group(grp)):
                    found.append((party, 0.70))

    # Strategy 4: counsel signature block (fallback)
    if not found:
        for cmatch in re.finditer(
            r"(?i)(?:attorneys?|counsel)\s+for\s+(?:the\s+)?"
            r"(?:plaintiff|defendant|petitioner|respondent|movant|appellant|appellee)s?"
            r"[,:]?\s*(.{3,120}?)(?:\n|$)",
            text[:6000],
        ):
            for party in _split_party_names(cmatch.group(1)):
                found.append((party, 0.55))

    best = {}
    for name, conf in found:
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if len(key) < 2:
            continue
        if key not in best or conf > best[key][1]:
            best[key] = (name, conf)

    count = 0
    for name, conf in best.values():
        cur.execute(
            f"""INSERT INTO document_parties
                (id, tenant_id, {doc_col}, extraction_run_id,
                 extraction_method, party_name, confidence, extracted_at)
                VALUES (%s, %s, %s, %s, 'structural', %s, %s, NOW())""",
            (str(uuid.uuid4()), tid, document_id, run_id, name[:500], conf),
        )
        count += 1

    return count


_ROLE_LABELS = (
    r"Plaintiffs?|Defendants?|Petitioners?|Respondents?|"
    r"Counter[- ]?Plaintiffs?|Counter[- ]?Defendants?|"
    r"Cross[- ]?Plaintiffs?|Cross[- ]?Defendants?|"
    r"Third[- ]?Party\s+Plaintiffs?|Third[- ]?Party\s+Defendants?|"
    r"Claimants?|Movants?|Appellants?|Appellees?|Intervenors?|"
    r"Demandantes?|Demandados?|Demandadas?"
)
_ROLE_RE = re.compile(r"\b(" + _ROLE_LABELS + r")\b\.?,?", re.IGNORECASE)

# Header / divider cues; party name is the text AFTER the last cue in a blob.
# Longest-first ordering (versus|vs|v) so "vs." is not matched as bare "v".
_CAPTION_CUE_RE = re.compile(
    r"(?i)\b(?:in\s+the\b|judicial\s+district|district\s+court|court|division|"
    r"department|judge|hon\.|county\s+of|state\s+of|district\s+of|for\s+the\b|"
    r"versus|vs\.?|v\.?|comes?\s+now|complaining\s+of|in\s+re\b|"
    r"civil\s+action\s+no\.?|cause\s+no\.?|case\s+no\.?|no\.)"
)

_DESCRIPTOR_RE = re.compile(
    r"(?i),?\s+an?\b.{0,40}?\b(?:corporation|company|partnership|"
    r"limited\s+liability\s+company|l\.l\.c\.|llc|l\.l\.p\.|llp|l\.p\.|lp|"
    r"trust|individual|proprietorship|association|entity)\b\.?"
)

_NAME_SPLIT_SUFFIX = (
    r"(?i:inc|incorporated|llc|l\.l\.c\.|llp|l\.l\.p\.|lp|l\.p\.|co|corp|"
    r"corporation|company|ltd|n\.a\.|p\.c\.|p\.a\.|pllc|pc|pa|trust)\b\.?,?"
)
_NAME_SPLIT_RE = re.compile(
    r"\s*;\s*"
    r"|\s*,?\s+and\s+(?=[A-Z0-9])"
    r"|\s*,\s+(?!" + _NAME_SPLIT_SUFFIX + r")(?=[A-Z0-9])"
)

_PARTY_REJECT_RE = re.compile(
    r"(?i)^(?:no\.?|cause\b|civil\s+action|case\b|volume|vol\.?|page|pages|"
    r"individually|collectively|inclusive|et\s+al\.?|defendants?\b|"
    r"plaintiffs?\b|the\s+above|to:)"
)

# Decree / docket / venue / doc-title content that cannot appear in a real
# party name. Note: deliberately excludes legit entity words (company, co,
# inc, trust, associates) so corporate parties survive.
_NOISE_RE = re.compile(
    r"(?i)\b(?:ordered|adjudged|decreed|granted|denied|dismissed|sustained|"
    r"overruled|hereby|shall|therefore|whereas|recover|prejudice|"
    r"judicial\s+district|county|nature\s+of\s+suit|offense|detention|"
    r"appearance|document\s+filed|motion|petition|writ|habeas|declaration|"
    r"opposition|conspiracy|substance|claim|amount|hearing|disposition|"
    r"terminated|complaint|request\s+for|response|answers|disclosure|"
    r"interrogator|subpoena|exhibit)\b"
)

_STATE_ONLY = {
    "TEXAS", "CALIFORNIA", "NEW YORK", "FLORIDA", "ARIZONA", "NEVADA",
    "DELAWARE", "ILLINOIS", "GEORGIA", "COLORADO", "WASHINGTON", "OREGON",
    "OHIO", "MICHIGAN", "VIRGINIA", "MASSACHUSETTS", "NEW JERSEY",
}


def _clean_caption_zone(text: str, limit: int = 4000) -> str:
    """Normalize the caption zone so multi-line, columned party names become
    contiguous. Strips leading AND lone line numbers and right-margin column
    rails () | * section-sign), then flattens to a single spaced blob."""
    if not text:
        return ""
    out = []
    for ln in text[:limit].splitlines():
        s = re.sub(r"^\s*\d{1,3}\s+(?=\D)", "", ln)      # leading line numbers
        if re.fullmatch(r"\s*\d{1,4}\s*", s):              # lone line-number line
            continue
        s = re.split(r"[\u00a7|*]", s, maxsplit=1)[0]      # drop right of rail char
        s = re.sub(r"\s\)\s.*$", "", s)                    # ) rail mid-line + right col
        s = re.sub(r"\s\)\s*$", "", s)                     # trailing ) rail
        if not re.search(r"[A-Za-z0-9]", s):               # pure-rail / empty line
            continue
        out.append(s.strip())
    flat = " ".join(x for x in out if x)
    return re.sub(r"\s{2,}", " ", flat)


def _isolate_name_blob(blob: str) -> str:
    """Given text preceding a role label, return just the party-name portion by
    cutting everything up to and including the last caption/header/venue cue.
    Trailing '.' is preserved (entity abbreviations like 'Inc.')."""
    blob = re.sub(r"\s{2,}", " ", blob).strip()
    if not blob:
        return ""
    last = None
    for m in _CAPTION_CUE_RE.finditer(blob):
        last = m
    if last:
        blob = blob[last.end():]
    return blob.strip(" ,;:-")


def _is_plaintiff_label(label: str) -> bool:
    l = label.lower().replace("-", "").replace(" ", "")
    return l.startswith((
        "plaintiff", "petitioner", "claimant", "movant", "appellant",
        "counterplaintiff", "crossplaintiff", "thirdpartyplaintiff",
        "demandante", "actor",
    ))


def _tidy_party(p: str) -> str:
    """Trim stray list punctuation while preserving a trailing abbreviation
    period (Inc. / Co. / L.L.C.) and internal hyphens (DOES 1-51)."""
    p = p.strip()
    p = re.sub(r"(?i)^to:?\s+", "", p)            # stray "TO:" prefix (RFD address)
    p = re.sub(r"^[\s,;:.\-]+", "", p)            # leading stray punctuation
    p = re.sub(r"[\s,;:\-]+$", "", p)             # trailing stray (keeps '.')
    p = re.sub(r"(?i)[\s,]*\binclusive\b\.?$", "", p).strip()
    p = re.sub(r"(?i)^and\s+", "", p).strip()
    p = re.sub(r"[\s,;:\-]+$", "", p)
    return p


def _split_party_names(raw: str) -> list[str]:
    """Split a raw party-name blob into individual, cleaned party names."""
    if not raw:
        return []
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = re.sub(
        r",?\s*(?:" + _ROLE_LABELS + r")\.?\s*$", "", raw, flags=re.IGNORECASE,
    )
    raw = re.sub(r"\([^)]*\)?", "", raw)                       # parentheticals
    raw = re.sub(r"[\"\u201c\u201d\u2018\u2019']", "", raw)    # quotes
    raw = _DESCRIPTOR_RE.sub("", raw)                          # entity descriptors
    out = []
    for p in _NAME_SPLIT_RE.split(raw):
        p = _tidy_party(p)
        if _looks_like_party(p):
            out.append(p[:500])
    return out


def _looks_like_party(s: str) -> bool:
    """Heuristic gate: reject case numbers, descriptors, bare labels, decree /
    venue / doc-title noise, and bare state names."""
    if not s or len(s) < 3 or len(s) > 300:
        return False
    if _PARTY_REJECT_RE.match(s):
        return False
    if _NOISE_RE.search(s):
        return False
    if s.strip().rstrip(".").upper() in _STATE_ONLY:
        return False
    letters = sum(ch.isalpha() for ch in s)
    if letters < 3:
        return False
    if sum(ch.isdigit() for ch in s) > letters:
        return False
    return True


# ─── CAUSES OF ACTION (STRUCTURAL) ─────────────────────────────────────────

def _extract_causes_structural(cur, tid, document_id, run_id, text, matter_id) -> int:
    """Extract COUNT/CAUSE OF ACTION headers from pleading text."""
    if not matter_id:
        return 0

    pattern = re.compile(
        r"(?i)(?:^|\n)\s*(?:COUNT\s+([IVXLCDM\d]+)|"
        r"(?:\w+\s+)?CAUSE\s+OF\s+ACTION)\s*[:\s\-—]*\s*(.+?)(?:\n|\r|$)",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    count = 0
    for i, m in enumerate(matches):
        count_num_str = m.group(1)
        title = (m.group(2) or "").strip()
        if not title:
            continue
        title = re.sub(r"\s+", " ", title)
        count_num = _roman_to_int(count_num_str) if count_num_str else (i + 1)

        cur.execute(
            """INSERT INTO causes_of_action
               (tenant_id, matter_id, title, count_number, status, created_at, updated_at)
               VALUES (%s, %s, %s, %s, 'not_developed', NOW(), NOW())""",
            (tid, matter_id, title[:500], count_num),
        )
        count += 1

    return count


def _roman_to_int(s: str) -> int | None:
    """Convert Roman numeral to integer. Returns int(s) if already a number."""
    if not s:
        return None
    s = s.strip().upper()
    if s.isdigit():
        return int(s)
    roman_vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    prev = 0
    for ch in reversed(s):
        val = roman_vals.get(ch, 0)
        if val < prev:
            total -= val
        else:
            total += val
        prev = val
    return total if total > 0 else None


# ─── DEADLINE EXTRACTION (STRUCTURAL) ───────────────────────────────────────

DEADLINE_TYPE_KEYWORDS = {
    "discovery": "discovery_cutoff",
    "expert": "expert_designation",
    "expert discovery": "expert_discovery_cutoff",
    "mediat": "mediation_deadline",
    "dispositive": "dispositive_motion_deadline",
    "summary judgment": "dispositive_motion_deadline",
    "pretrial": "pretrial_conference",
    "pre-trial": "pretrial_conference",
    "trial": "trial_date",
    "jury": "jury_fee_deadline",
    "answer": "answer_deadline",
    "closing": "closing_date",
    "inspection": "inspection_deadline",
    "survey": "survey_deadline",
    "financing": "financing_deadline",
    "option": "option_period",
    "title": "title_commitment",
    "effective": "effective_date",
    "expir": "expiration_date",
    "terminat": "termination_date",
}

# Date extraction patterns
# Order matters: abbreviated month with period first (Sept. 30, 2026),
# then full/abbreviated without period, then numeric formats
DATE_PATTERNS = [
    (re.compile(r"([A-Za-z]+\.\s*\d{1,2},?\s+\d{4})"), None),   # Sept. 30, 2026
    (re.compile(r"([A-Za-z]+\s+\d{1,2},?\s+\d{4})"), None),      # September 30, 2026
    (re.compile(r"(\d{1,2}/\d{1,2}/\d{2,4})"), None),              # 9/30/2026
    (re.compile(r"(\d{4}-\d{2}-\d{2})"), None),                     # 2026-09-30
]

# Calculated deadline pattern
CALC_PATTERN = re.compile(
    r"(\d+)\s+(?:days?|business\s+days?|weeks?)\s+(?:before|after|prior\s+to)\s+(.+?)(?:\.|$)",
    re.IGNORECASE,
)


def _extract_deadlines_structural(cur, tid, document_id, run_id, text, doc_col) -> int:
    """
    Extract dates from text with context-based relevance filtering.

    Only extracts dates that appear near scheduling-relevant keywords.
    This prevents pulling narrative dates (e-filing timestamps, WHEREAS
    recitals, deposition signatures, financial statement numbers).

    Scheduling orders and court orders get looser filtering since most
    dates in those documents are operative deadlines.
    """
    # Keywords that indicate a date is an operative deadline, not narrative
    DEADLINE_KEYWORDS = re.compile(
        r"(?i)("
        r"deadline|cutoff|cut[\-\s]?off|due\s+(?:date|by|on|before)|"
        r"shall\s+(?:be\s+)?(?:filed|served|completed|exchanged|designated)|"
        r"no\s+later\s+than|on\s+or\s+before|must\s+(?:be\s+)?(?:filed|completed)|"
        r"hearing|trial|conference|mediation|arbitration|"
        r"discovery|deposition|expert|designation|rebuttal|"
        r"joinder|amended\s+plead|intervention|"
        r"motions?\s+(?:in\s+limine|for\s+summary|dispositive)|"
        r"response|reply|objection|"
        r"comply|compliance|ordered|"
        r"pretrial|pre[\-\s]?trial|docket\s+call|"
        r"jury\s+fee|"
        r"close[sd]?|expires?|terminat|"
        r"(?:deliver|produce|provide|submit|file|serve)\s"
        r")"
    )

    # Negative keywords — dates near these are likely narrative, not deadlines
    NARRATIVE_KEYWORDS = re.compile(
        r"(?i)("
        r"filed\s+(?:on|this)|was\s+filed|"
        r"on\s+(?:or\s+about|approximately)|"
        r"dated|signed\s+this|entered\s+(?:on|this)|"
        r"e[\-]?filed\s+on|"
        r"born\s+on|"
        r"certificate\s+of\s+service|"
        r"hereby\s+certif|"
        r"notary|commission\s+expires|"
        r"page\s+\d+\s+of|"
        r"reporter|csr\s+no|shorthand"
        r")"
    )

    count = 0
    seen_positions = set()  # Dedup by character position

    for pat, _ in DATE_PATTERNS:
        for m in pat.finditer(text):
            raw_date = m.group(1).strip()
            parsed = _parse_date_str(raw_date)

            # Skip unparseable dates
            if not parsed:
                continue

            # Dedup by start position (± 5 chars to catch overlapping patterns)
            pos_bucket = m.start() // 5
            if pos_bucket in seen_positions:
                continue
            seen_positions.add(pos_bucket)

            # Get context window: 250 chars before, 100 after
            ctx_start = max(0, m.start() - 250)
            ctx_end = min(len(text), m.end() + 100)
            context = text[ctx_start:ctx_end]

            # Check for deadline-relevant keywords in context
            has_deadline_keyword = bool(DEADLINE_KEYWORDS.search(context))
            has_narrative_keyword = bool(NARRATIVE_KEYWORDS.search(context))

            # Decision: include or skip
            if has_narrative_keyword and not has_deadline_keyword:
                continue  # Narrative date, skip

            if not has_deadline_keyword:
                continue  # No scheduling relevance, skip

            # Build display text from nearby context
            display_start = max(0, m.start() - 100)
            display_end = min(len(text), m.end() + 100)
            display_text = text[display_start:display_end].replace("\n", " ").strip()

            # Classify deadline type from keywords
            deadline_type = None
            context_lower = context.lower()
            for keyword, dtype in DEADLINE_TYPE_KEYWORDS.items():
                if keyword in context_lower:
                    deadline_type = dtype
                    break

            # Higher confidence if deadline keyword is very close (within 50 chars)
            close_context = text[max(0, m.start() - 50):min(len(text), m.end() + 50)]
            confidence = 0.90 if DEADLINE_KEYWORDS.search(close_context) else 0.70

            cur.execute(
                f"""INSERT INTO document_deadlines
                    (id, tenant_id, {doc_col}, extraction_run_id,
                     extraction_method, deadline_text, deadline_date,
                     is_calculated, confidence, extracted_at)
                    VALUES (%s, %s, %s, %s, 'structural', %s, %s, false, %s, NOW())""",
                (str(uuid.uuid4()), tid, document_id, run_id,
                 display_text[:500], parsed, confidence),
            )
            count += 1

    # Calculated deadlines (these are always relevant — "30 days before trial")
    for m in CALC_PATTERN.finditer(text):
        ctx_start = max(0, m.start() - 80)
        ctx_end = min(len(text), m.end() + 20)
        context = text[ctx_start:ctx_end].replace("\n", " ").strip()
        basis = m.group(0).strip()

        cur.execute(
            f"""INSERT INTO document_deadlines
                (id, tenant_id, {doc_col}, extraction_run_id,
                 extraction_method, deadline_text, is_calculated,
                 calculation_basis, confidence, extracted_at)
                VALUES (%s, %s, %s, %s, 'structural', %s, true, %s, 0.70, NOW())""",
            (str(uuid.uuid4()), tid, document_id, run_id,
             context[:500], basis[:500]),
        )
        count += 1

    return count


def _scan_defined_term_usage(cur, tid, document_id, run_id, text, doc_col, sections_map) -> int:
    """After terms are extracted, scan for usage across sections."""
    # Get all defined terms for this document
    cur.execute(
        f"""SELECT id, term FROM document_defined_terms
            WHERE TRIM(tenant_id) = %s AND {doc_col} = %s
              AND extraction_run_id = %s AND superseded_by_run_id IS NULL""",
        (tid, document_id, run_id),
    )
    terms = cur.fetchall()
    if not terms:
        return False

    count = 0
    for term_id, term_text in terms:
        # Count occurrences (case-sensitive for defined terms)
        usage_count = 0
        usage_section_ids = []

        for sec_idx, sec_id in sections_map.items():
            # Would need section content; for now count in full text
            pass

        # Simple full-text count
        try:
            escaped = re.escape(term_text)
            usage_count = len(re.findall(escaped, text))
        except re.error:
            usage_count = text.count(term_text)

        if usage_count > 0:
            cur.execute(
                """UPDATE document_defined_terms
                   SET usage_count = %s
                   WHERE id = %s""",
                (usage_count, term_id),
            )
            count += 1

    return count


# ─── FINANCIAL LINE ITEMS ──────────────────────────────────────────────────

def _extract_financial_line_items(cur, tid, document_id, run_id, text, doc_col) -> int:
    """Parse tabular financial data into document_sections."""
    # Match lines with a text label followed by dollar amounts
    pattern = re.compile(
        r"^(.{3,60}?)\s{2,}(\$?\s*[\d,]+\.?\d*)\s*$",
        re.MULTILINE,
    )
    count = 0
    for i, m in enumerate(pattern.finditer(text)):
        label = m.group(1).strip()
        amount = m.group(2).strip()
        content = f"{label}: {amount}"
        depth = 1 if label.startswith(" ") or label.startswith("\t") else 0

        cur.execute(
            f"""INSERT INTO document_sections
                (id, tenant_id, {doc_col}, extraction_run_id,
                 section_index, section_type, section_label, content,
                 char_start, char_end, nesting_depth, extracted_at)
                VALUES (%s, %s, %s, %s, %s, 'financial_line_item', %s, %s, %s, %s, %s, NOW())""",
            (str(uuid.uuid4()), tid, document_id, run_id,
             i, label[:200], content, m.start(), m.end(), depth),
        )
        count += 1

    return count


# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# --- citation/authority extractor ---
# CITATIONS / LEGAL AUTHORITIES (STRUCTURAL) -> document_citations
# ═══════════════════════════════════════════════════════════════════════════════

_REPORTERS = (
    r"U\.S\.|S\.\s?Ct\.|L\.\s?Ed\.(?:\s?2d)?|"
    r"F\.\s?Supp\.(?:\s?[23]d)?|F\.(?:\s?App'x|[23]d)?|"
    r"S\.W\.(?:[23]d)?|N\.E\.(?:[23]d)?|N\.W\.(?:[23]d)?|"
    r"S\.E\.(?:[23]d)?|So\.(?:[23]d)?|P\.(?:[23]d)?|A\.(?:[23]d)?"
)

CITATION_PATTERNS = [
    # case reporter:  123 S.W.3d 456
    (re.compile(r"\b\d{1,4}\s+(?:%s)\s+\d{1,4}\b" % _REPORTERS), "case", 0.88),
    # federal statute:  42 U.S.C. § 1983
    (re.compile(r"\b\d{1,2}\s+U\.?S\.?C\.?\s+§+\s*\d+[A-Za-z0-9\-().]*"), "statute", 0.92),
    # federal regulation:  29 C.F.R. § 1604.11
    (re.compile(r"\b\d{1,3}\s+C\.?F\.?R\.?\s+§+\s*\d+[A-Za-z0-9\-().]*"), "regulation", 0.92),
    # Texas codes:  Tex. Civ. Prac. & Rem. Code § 16.003
    (re.compile(r"\bTex\.\s+[A-Z][A-Za-z.&'\s]{2,45}?Code(?:\s+Ann\.)?\s+§+\s*[\d.]+[A-Za-z0-9()]*"), "statute", 0.90),
    # rules of procedure/evidence:  Tex. R. Civ. P. 166a / Fed. R. Evid. 702
    (re.compile(r"\b(?:Tex\.|Fed\.)\s+R\.\s+(?:Civ\.|App\.|Evid\.|Crim\.)\s*(?:P|Proc|Evid|App)?\.?\s*\d+[A-Za-z0-9().\-]*"), "rule", 0.90),
    # constitutional:  U.S. Const. art. III / Tex. Const. art. I, § 19
    (re.compile(r"\b(?:U\.S\.|Tex\.)\s+Const\.\s+art\.\s+[IVXLC]+(?:,?\s*§+\s*\d+[A-Za-z0-9]*)?"), "constitution", 0.90),
]


def _normalize_citation(raw: str) -> str:
    return re.sub(r"\s+", " ", raw).strip().rstrip(".,;")


def _extract_citations_structural(cur, tid, document_id, run_id, text, doc_col) -> int:
    """Extract legal authorities to document_citations. Deterministic Tier-1;
    runs for every classified document type. Deduped per document by normalized
    citation text."""
    if not text:
        return 0

    count = 0
    seen = set()

    for regex, ctype, conf in CITATION_PATTERNS:
        for m in regex.finditer(text):
            raw = m.group(0).strip()
            norm = _normalize_citation(raw)
            if not norm or len(norm) > 300:
                continue
            key = norm.upper()
            if key in seen:
                continue
            seen.add(key)

            page_num = text[:m.start()].count("\f") + 1
            cur.execute(
                f"""INSERT INTO document_citations
                    (id, tenant_id, {doc_col}, extraction_run_id,
                     extraction_method, citation_text, citation_type,
                     normalized_citation, page_number, char_start, char_end,
                     confidence, extracted_at)
                    VALUES (%s, %s, %s, %s, 'structural', %s, %s, %s, %s, %s, %s, %s, NOW())""",
                (str(uuid.uuid4()), tid, document_id, run_id,
                 raw[:500], ctype, norm[:500], page_num, m.start(), m.end(), conf),
            )
            count += 1

    return count


# ==========================================================================
# PII (Tier-1 deterministic; runs for every classified doc type)
# ==========================================================================

def _norm_digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _luhn_ok(digits: str) -> bool:
    if not (13 <= len(digits) <= 19):
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _aba_ok(digits: str) -> bool:
    if len(digits) != 9:
        return False
    d = [ord(c) - 48 for c in digits]
    chk = (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7])
           + (d[2] + d[5] + d[8])) % 10
    return chk == 0


# Skip a numeric match if one of these labels sits immediately before it:
# almost always a docket / case / invoice / account number, NOT PII.
_PII_NEG_CTX = re.compile(
    r"(?:case|cause|docket|civil\s+action|no\.?|number|"
    r"inv(?:oice)?|acct|account|loan|policy|claim|matter|bates)"
    r"\s*[:#.]?\s*$",
    re.IGNORECASE,
)
_PII_ROUTING_CTX = re.compile(r"(routing|aba|rtn|ach|transit)", re.IGNORECASE)
_PII_DOB_CTX = re.compile(
    r"(d\.?o\.?b\.?|date\s+of\s+birth|born(?:\s+on)?)\s*[:.]?\s*$", re.IGNORECASE
)
_PII_DL_CTX = re.compile(
    r"(driver'?s?\s+lic|dl\s*(?:no|#)|dln|operator'?s?\s+lic)", re.IGNORECASE
)
_PII_PASSPORT_CTX = re.compile(r"passport", re.IGNORECASE)

_PII_LOOKBACK = 40  # chars of preceding context tested against cues

# (regex, pii_type, confidence, validator, require_ctx_regex, check_neg)
#   validator     : None | "luhn" | "aba"
#   require_ctx   : None | compiled regex that MUST match the preceding window
#   check_neg     : if True, skip when _PII_NEG_CTX matches the preceding window
# Credit-card validation. Luhn alone yields ~10% false positives on random
# account/loan numbers (Fortis borrowing-base report). Require a valid issuer
# prefix + exact network length, AND either canonical grouping (4-4-4-4 /
# Amex 4-6-5) or an explicit card-context cue.
_PII_CC_CTX = re.compile(
    r"(credit\s*card|card\s*(?:no|num|#)|visa|mastercard|amex|"
    r"american\s+express|discover|ending\s+in|cc\s*#?)",
    re.IGNORECASE,
)


def _cc_iin_ok(digits: str) -> bool:
    n = len(digits)
    if n == 15:
        return digits[:2] in ("34", "37")                  # Amex
    if n == 16:
        if digits[0] == "4":                               # Visa
            return True
        if digits[:2] in ("51", "52", "53", "54", "55"):   # MasterCard
            return True
        if 2221 <= int(digits[:4]) <= 2720:                # MasterCard 2-series
            return True
        if digits[:4] == "6011" or digits[:2] == "65":     # Discover
            return True
        if digits[:2] == "35":                             # JCB
            return True
        return False
    return False


def _cc_valid(raw: str, window: str) -> bool:
    digits = _norm_digits(raw)
    if not _luhn_ok(digits) or not _cc_iin_ok(digits):
        return False
    seps = raw.count(" ") + raw.count("-")
    grouped = (len(digits) == 16 and seps == 3) or (len(digits) == 15 and seps == 2)
    if grouped:
        return True
    if seps == 0:                      # bare run: require an explicit card cue
        return bool(_PII_CC_CTX.search(window))
    return False                       # irregular spacing -> reject


def _id_token_ok(raw: str, window: str) -> bool:
    # Driver's-license / passport numbers always contain at least one digit.
    # Rejects all-caps dictionary words (NUMBER, SPECIAL, WARRANTY) that match
    # [A-Z0-9]{6,12} when a license/passport cue happens to sit nearby.
    return any(ch.isdigit() for ch in raw)


PII_PATTERNS = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
     "email", 0.95, None, None, False),
    (re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
     "ssn", 0.90, None, None, True),
    (re.compile(r"\b\d{2}-\d{7}\b"),
     "ein", 0.72, None, None, True),
    (re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(\d{3}\)\s?|\d{3}[-.\s])\d{3}[-.\s]\d{4}(?!\d)"),
     "phone", 0.80, None, None, True),
    (re.compile(r"\b(?:\d[ -]?){13,18}\d\b"),
     "credit_card", 0.86, _cc_valid, None, True),
    (re.compile(r"\b\d{9}\b"),
     "bank_routing", 0.80, "aba", _PII_ROUTING_CTX, False),
    (re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|"
                r"\b[A-Za-z]+\.?\s+\d{1,2},?\s+\d{4}\b"),
     "dob", 0.80, None, _PII_DOB_CTX, False),
    (re.compile(r"\b[A-Z0-9]{6,12}\b"),
     "drivers_license", 0.62, _id_token_ok, _PII_DL_CTX, False),
    (re.compile(r"\b[A-Z0-9]{6,9}\b"),
     "passport", 0.68, _id_token_ok, _PII_PASSPORT_CTX, False),
    (re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
                r"(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
     "ip_address", 0.65, None, None, False),
]


def _pii_normalize(ptype: str, raw: str) -> str | None:
    if ptype == "email":
        return raw.strip().lower()[:500]
    if ptype in ("ssn", "ein", "credit_card", "bank_routing"):
        return _norm_digits(raw)[:500]
    if ptype == "phone":
        return _norm_digits(raw)[-10:]
    if ptype == "dob":
        return (_parse_date_str(raw) or _norm_digits(raw))[:500]
    if ptype in ("drivers_license", "passport"):
        return raw.strip().upper()[:500]
    return raw.strip()[:500]


def _extract_pii(cur, tid, document_id, run_id, text, doc_col) -> int:
    """Tier-1 deterministic PII capture to document_entities.is_pii.
    Mirrors _extract_citations_structural: family table, finditer,
    per-document dedup on (pii_type, normalized_value)."""
    if not text:
        return 0

    count = 0
    seen = set()

    for regex, ptype, conf, validator, require_ctx, check_neg in PII_PATTERNS:
        for m in regex.finditer(text):
            raw = m.group(0).strip()
            if not raw:
                continue

            window = text[max(0, m.start() - _PII_LOOKBACK):m.start()]
            if check_neg and _PII_NEG_CTX.search(window):
                continue
            if require_ctx is not None and not require_ctx.search(window):
                continue
            if callable(validator):
                if not validator(raw, window):
                    continue
            elif validator == "luhn" and not _luhn_ok(_norm_digits(raw)):
                continue
            elif validator == "aba" and not _aba_ok(_norm_digits(raw)):
                continue

            norm = _pii_normalize(ptype, raw)
            key = (ptype, (norm or raw).upper())
            if key in seen:
                continue
            seen.add(key)

            page_num = text[:m.start()].count("\f") + 1
            cur.execute(
                f"""INSERT INTO document_entities
                    (id, tenant_id, {doc_col}, extraction_run_id,
                     extraction_method, entity_text, entity_type,
                     normalized_value, page_number, char_start, char_end,
                     confidence, is_pii, pii_type, extracted_at)
                    VALUES (%s, %s, %s, %s, 'structural', %s, 'pii', %s,
                            %s, %s, %s, %s, true, %s, NOW())""",
                (str(uuid.uuid4()), tid, document_id, run_id,
                 raw[:500], norm, page_num, m.start(), m.end(), conf, ptype),
            )
            count += 1

    return count


def _normalize_coa_title(raw) -> str | None:
    """
    Normalize a cause of action value from Haiku into a clean title string.
    Handles: str, dict with 'cause'/'cause_of_action'/'title'/'name' keys,
    nested 'data' lists, and None.
    Returns cleaned title or None if unparseable.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        # Reject if it looks like a stringified dict/list
        if s.startswith("{") or s.startswith("["):
            try:
                parsed = __import__("json").loads(s)
                return _normalize_coa_title(parsed)
            except Exception:
                pass
        # Reject junk
        if not s or s.lower() in ("none", "null", "n/a"):
            return None
        if len(s) > 300:
            return None  # Probably a paragraph, not a title
        return s

    if isinstance(raw, dict):
        # Try known keys in priority order
        for key in ("cause", "cause_of_action", "title", "name"):
            val = raw.get(key)
            if val and isinstance(val, str) and val.lower() not in ("none", "null"):
                return val.strip()
        # Check for 'data' wrapper (list of sub-COAs)
        data = raw.get("data") or raw.get("values") or raw.get("value")
        if isinstance(data, list):
            # Return first COA title from the list
            for item in data:
                t = _normalize_coa_title(item)
                if t:
                    return t
        if isinstance(data, str):
            return _normalize_coa_title(data)
        return None

    if isinstance(raw, list):
        for item in raw:
            t = _normalize_coa_title(item)
            if t:
                return t
        return None

    return None


def _extract_coa_titles_from_haiku(field_data) -> list[str]:
    """
    Given Haiku's response for a causes_of_action field, extract all
    COA titles as a flat list of clean strings.
    Handles: single dict, list of dicts, nested 'data' lists, plain strings.
    """
    titles = []

    items = field_data if isinstance(field_data, list) else [field_data]
    for item in items:
        if isinstance(item, dict):
            # Check for 'data'/'values' wrapper containing a list of COAs
            data = item.get("data") or item.get("values") or item.get("value")
            if isinstance(data, list):
                for sub in data:
                    t = _normalize_coa_title(sub)
                    if t:
                        titles.append(t)
                continue
            # Single dict
            t = _normalize_coa_title(item)
            if t:
                titles.append(t)
        else:
            t = _normalize_coa_title(item)
            if t:
                titles.append(t)

    # Deduplicate (case-insensitive) while preserving order
    seen = set()
    unique = []
    for t in titles:
        key = t.strip().upper()
        if key not in seen:
            seen.add(key)
            unique.append(t)

    return unique


# TIER 2 — HAIKU ESCALATION
# ═══════════════════════════════════════════════════════════════════════════════

# --- escalation routed through adapter ---
def _run_async(coro):
    """Run an async coroutine from this sync engine. Plain asyncio.run() in the
    normal (no running loop) case; defensively offloads to a worker thread if a
    loop is already running."""
    import asyncio
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


def _escalate_to_haiku(
    tid, document_id, run_id, text, parse_type, missing_fields,
    template, doc_col, other_col, matter_id,
) -> dict | None:
    """
    Send first 4K chars to Haiku for extraction of fields that
    Tier 1 regex/structural could not capture.
    Returns dict of counts by table, or None on failure.
    """
    # Build field schema for prompt
    fields_for_prompt = []
    for field_def in template.get("fields", []):
        if field_def.get("name") in missing_fields:
            fields_for_prompt.append({
                "name": field_def["name"],
                "target_table": field_def.get("target_table"),
                "description": field_def.get("structural_rule", field_def.get("name")),
                "required": field_def.get("required", False),
            })

    prompt = f"""You are a legal document parser. Analyze this document excerpt and extract structured data.

Document type detected: {parse_type}
Required fields that Tier 1 could not extract: {', '.join(missing_fields)}

Text (first 4000 chars):
{text[:4000]}

Return JSON with these fields:
{json.dumps(fields_for_prompt, indent=2)}

Rules:
- Only extract what is explicitly stated in the text
- Set confidence 0.0-1.0 for each field
- If a field is not present, set value to null
- For dates, use ISO format (YYYY-MM-DD)
- For parties, include both name and role
- Return ONLY valid JSON, no markdown, no preamble"""

    # Route the escalation through the multi-provider choke point: model is
    # chosen by the ('intelligence','extraction_escalation') ai_model_routing
    # row (Ollama by default, cloud burst optional), and the call is attributed
    # in ai_api_calls. Prompt + downstream JSON handling are unchanged.
    from modules.intelligence.anthropic_adapter import call as _ai_call, AICallContext

    _ctx = AICallContext(
        tenant_id=tid,
        module="intelligence",
        purpose="extraction_escalation",
        matter_id=matter_id,
        document_id=str(document_id) if document_id else None,
        document_source="dms_documents",
    )
    try:
        _res = _run_async(
            _ai_call(_ctx, raw_user_prompt=prompt, max_tokens_override=2000)
        )
        response_text = _res.text or ""
    except Exception as exc:
        log.warning("Tier 2 escalation call failed for %s: %s", document_id, exc)
        return None

    # Strip markdown fences
    response_text = response_text.strip()
    if response_text.startswith("```"):
        response_text = re.sub(r"^```(?:json)?\s*", "", response_text)
        response_text = re.sub(r"\s*```$", "", response_text)

    try:
        extracted = json.loads(response_text)
    except json.JSONDecodeError:
        log.warning("Haiku returned non-JSON response for %s", document_id)
        return None

    # Write extracted fields to DB
    conn = _get_db_conn()
    conn.autocommit = False
    cur = conn.cursor()
    results = {"parties": 0, "deadlines": 0, "defined_terms": 0, "causes_of_action": 0}

    try:
        # Normalize extracted to dict — Claude sometimes returns a list
        if isinstance(extracted, list):
            # Convert list of dicts into a single merged dict keyed by field name
            merged = {}
            for item in extracted:
                if isinstance(item, dict):
                    # If item has a 'name' or 'field' key, use it
                    fname = item.get("name") or item.get("field")
                    if fname:
                        merged[fname] = item.get("value", item)
                    else:
                        # Try to match by known field names
                        for k, v in item.items():
                            merged[k] = v
            extracted = merged
            log.info("Haiku returned list, normalized to dict with keys: %s",
                     list(extracted.keys()))

        if not isinstance(extracted, dict):
            log.warning("Haiku returned unexpected type %s for %s, skipping",
                        type(extracted).__name__, document_id)
            return None

        for field_name, field_data in extracted.items():
            if field_data is None:
                continue

            # Map field to target table and write
            field_def = next(
                (f for f in template.get("fields", []) if f.get("name") == field_name),
                None,
            )
            if not field_def:
                continue

            target_table = field_def.get("target_table")
            confidence = 0.75  # Haiku confidence floor

            if isinstance(field_data, dict) and "confidence" in field_data:
                confidence = float(field_data["confidence"])
                field_data = field_data.get("value", field_data)

            if target_table == "document_parties":
                parties = field_data if isinstance(field_data, list) else [field_data]
                for party in parties:
                    pname = party.get("name", str(party)) if isinstance(party, dict) else str(party)
                    if pname and len(pname) > 2:
                        cur.execute(
                            f"""INSERT INTO document_parties
                                (id, tenant_id, {doc_col}, extraction_run_id,
                                 extraction_method, party_name, confidence, extracted_at)
                                VALUES (%s, %s, %s, %s, 'ai_haiku', %s, %s, NOW())""",
                            (str(uuid.uuid4()), tid, document_id, run_id,
                             pname[:500], confidence),
                        )
                        results["parties"] += 1

            elif target_table == "document_deadlines":
                deadlines = field_data if isinstance(field_data, list) else [field_data]
                for dl in deadlines:
                    if isinstance(dl, dict):
                        dl_text = dl.get("label", dl.get("text", str(dl)))
                        dl_date = _parse_date_str(dl.get("date", ""))
                    else:
                        dl_text = str(dl)
                        dl_date = _parse_date_str(str(dl))
                    cur.execute(
                        f"""INSERT INTO document_deadlines
                            (id, tenant_id, {doc_col}, extraction_run_id,
                             extraction_method, deadline_text, deadline_date,
                             confidence, extracted_at)
                            VALUES (%s, %s, %s, %s, 'ai_haiku', %s, %s, %s, NOW())""",
                        (str(uuid.uuid4()), tid, document_id, run_id,
                         str(dl_text)[:500], dl_date, confidence),
                    )
                    results["deadlines"] += 1

            elif target_table == "document_defined_terms":
                terms = field_data if isinstance(field_data, list) else [field_data]
                for t in terms:
                    if isinstance(t, dict):
                        term = t.get("term", str(t))
                        defn = t.get("definition", "(AI-extracted)")
                    else:
                        term = str(t)
                        defn = "(AI-extracted)"
                    if term and len(term) > 1:
                        cur.execute(
                            f"""INSERT INTO document_defined_terms
                                (id, tenant_id, {doc_col}, extraction_run_id,
                                 extraction_method, term, definition_text,
                                 confidence, extracted_at)
                                VALUES (%s, %s, %s, %s, 'ai_haiku', %s, %s, %s, NOW())""",
                            (str(uuid.uuid4()), tid, document_id, run_id,
                             term[:500], defn[:5000], confidence),
                        )
                        results["defined_terms"] += 1

            elif target_table == "causes_of_action" and matter_id:
                titles = _extract_coa_titles_from_haiku(field_data)
                for ci, title in enumerate(titles):
                    cur.execute(
                        """INSERT INTO causes_of_action
                           (tenant_id, matter_id, title, count_number, status,
                            created_at, updated_at)
                           VALUES (%s, %s, %s, %s, 'not_developed', NOW(), NOW())""",
                        (tid, matter_id, title[:500], ci + 1),
                    )
                    results["causes_of_action"] += 1

        conn.commit()
        return results

    except Exception as exc:
        conn.rollback()
        log.warning("Haiku result write failed for %s: %s", document_id, exc)
        return None
    finally:
        cur.close()
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# BASIC METADATA (fallback when no template exists)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_basic_metadata(tid, document_id, run_id, content_text, doc_col, other_col, file_path):
    """Write basic metadata row when no extraction template applies."""
    conn = _get_db_conn()
    conn.autocommit = False
    cur = conn.cursor()
    try:
        _write_metadata(cur, tid, document_id, run_id, content_text, doc_col, other_col, file_path)
        conn.commit()
        page_count = content_text.count("\f") + 1 if content_text else 0
        word_count = len(content_text.split()) if content_text else 0
        return {"sections": 0, "parties": 0, "defined_terms": 0, "deadlines": 0,
                "extraction_method": "basic_metadata", "page_count": page_count,
                "word_count": word_count}
    except Exception as exc:
        conn.rollback()
        log.warning("Basic metadata write failed for %s: %s", document_id, exc)
        return {"error": str(exc)}
    finally:
        cur.close()
        conn.close()


def _write_metadata(cur, tid, document_id, run_id, content_text, doc_col, other_col, file_path):
    """Write document_metadata row."""
    from pathlib import Path
    filename = Path(file_path).name if file_path else None
    page_count = content_text.count("\f") + 1 if content_text else None
    word_count = len(content_text.split()) if content_text else None

    cur.execute(
        f"""INSERT INTO document_metadata
            (id, tenant_id, {doc_col}, extraction_run_id,
             extraction_method, title, page_count, word_count, extracted_at)
            VALUES (%s, %s, %s, %s, 'template_engine', %s, %s, %s, NOW())
            ON CONFLICT DO NOTHING""",
        (str(uuid.uuid4()), tid, document_id, run_id,
         filename, page_count, word_count),
    )
