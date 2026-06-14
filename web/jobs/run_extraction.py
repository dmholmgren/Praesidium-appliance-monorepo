#!/usr/bin/env python3
"""
jobs/run_extraction.py

Run the extraction template engine against existing dms_documents that have
text but haven't been through primitive extraction yet.

Re-classifies each document using the same heuristic patterns as ingest_sample,
creates an extraction_run for audit trail, then calls run_extraction_template()
for each document.

Usage:
    docker exec praesidium-web python3 /app/jobs/run_extraction.py \
        --tenant 986c0fee-1390-43bb-ad28-8cd1db6de53f

    # Limit to N documents:
        --limit 20

    # Only specific document types:
        --type pleading

    # Re-extract (supersedes prior runs):
        --re-extract

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

log = logging.getLogger("run_extraction")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def get_db_conn():
    url = os.environ.get("DATABASE_URL", "")
    url = url.replace("postgresql+asyncpg://", "postgresql://")
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


# ── Classification (same patterns as ingest_sample.py) ────────────────────────

DOC_TYPE_PATTERNS = {
    "pleading": {
        "markers": [
            re.compile(r"(?i)(cause\s+no|case\s+no|civil\s+action|in\s+the\s+(district|county|circuit)\s+court)"),
            re.compile(r"(?i)(plaintiff|defendant|petitioner|respondent)"),
            re.compile(r"(?i)(complaint|answer|petition|counterclaim|cross[- ]?claim|intervention)"),
        ],
        "min_matches": 2,
    },
    "motion": {
        "markers": [
            re.compile(r"(?i)(motion\s+(to|for)\s+\w+)"),
            re.compile(r"(?i)(comes?\s+now|hereby\s+moves?|respectfully\s+moves?)"),
            re.compile(r"(?i)(prayer|wherefore|relief\s+requested|certificate\s+of\s+conference)"),
        ],
        "min_matches": 2,
    },
    "brief": {
        "markers": [
            re.compile(r"(?i)(response\s+(to|in\s+opposition)|reply\s+(brief|to)|sur-?reply|memorandum\s+(?:of|in))"),
            re.compile(r"(?i)(argument|standard\s+of\s+review|summary\s+of\s+(?:the\s+)?argument)"),
            re.compile(r"(?i)(plaintiff|defendant|petitioner|respondent|appellant|appellee|movant|non-?movant)s?(?:['']s?)?\s+(?:response|reply|brief|opposition|memorandum)"),
        ],
        "min_matches": 2,
    },
    "court_order": {
        "markers": [
            re.compile(r"(?i)(it\s+is\s+(?:therefore\s+)?(?:ordered|adjudged|decreed)|the\s+court\s+(?:hereby\s+)?(?:orders|grants|denies|sustains|overrules|finds))"),
            re.compile(r"(?i)(signed\s+(?:this|on)|(?:judge|hon\.)\s+[A-Z])"),
            re.compile(r"(?i)(order\s+(?:granting|denying|on|regarding|sustaining|overruling)|(?:agreed|consent|protective)\s+order)"),
        ],
        "min_matches": 1,
    },
    "judgment": {
        "markers": [
            re.compile(r"(?i)(final\s+judgment|partial\s+judgment|default\s+judgment|summary\s+judgment|consent\s+judgment)"),
            re.compile(r"(?i)(judgment\s+(?:is\s+)?(?:rendered|entered|awarded)|(?:in\s+favor\s+of|against)\s+[A-Z])"),
            re.compile(r"(?i)(damages?\s+(?:in\s+the\s+(?:amount|sum)|(?:of|awarded))\s+\$|pre-?judgment\s+interest|post-?judgment\s+interest)"),
        ],
        "min_matches": 1,
    },
    "affidavit": {
        "markers": [
            re.compile(r"(?i)(affidavit\s+of|declaration\s+of|sworn\s+(?:statement|testimony)\s+of)"),
            re.compile(r"(?i)(subscribed\s+and\s+sworn|sworn\s+to\s+(?:and\s+subscribed\s+)?before|under\s+penalty\s+of\s+perjury|notary\s+public)"),
            re.compile(r"(?i)(my\s+name\s+is|i\s+am\s+over\s+the\s+age|personal\s+knowledge|competent\s+to\s+testify)"),
        ],
        "min_matches": 2,
    },
    "expert_report": {
        "markers": [
            re.compile(r"(?i)(expert\s+(?:report|opinion|analysis)|report\s+of\s+(?:\w+\s+)?expert)"),
            re.compile(r"(?i)((?:Ph\.?D|M\.?D|P\.?E|CPA|J\.?D|M\.?B\.?A)[,.\s]|board\s+certified|licensed\s+(?:professional|engineer))"),
            re.compile(r"(?i)(methodology|opinions?\s+(?:and\s+)?(?:conclusions?|findings?)|basis\s+for\s+(?:my\s+)?opinions?|reasonable\s+degree\s+of\s+(?:\w+\s+)?(?:certainty|probability))"),
        ],
        "min_matches": 2,
    },
    "disclosure": {
        "markers": [
            re.compile(r"(?i)((?:initial|supplemental|amended)\s+disclosure|rule\s+(?:194|26\(a\)))"),
            re.compile(r"(?i)(persons?\s+(?:with|having)\s+knowledge|testifying\s+expert|(?:documents?\s+)?(?:relevant\s+to|supporting))"),
            re.compile(r"(?i)(computation\s+of\s+(?:each\s+category\s+of\s+)?damages?|insurance\s+(?:agreement|coverage|policy))"),
        ],
        "min_matches": 1,
    },
    "discovery": {
        "markers": [
            re.compile(r"(?i)(interrogator|request\s+for\s+(production|admission)|subpoena\s+duces|request\s+for\s+disclosure)"),
            re.compile(r"(?i)(propound|serve|answer\s+(to|the)\s+(following|interrogator)|you\s+are\s+(?:hereby\s+)?(?:requested|commanded))"),
        ],
        "min_matches": 1,
    },
    "discovery_response": {
        "markers": [
            re.compile(r"(?i)((?:responses?|answers?|objections?)\s+(?:and\s+(?:objections?|responses?)\s+)?(?:of|to)\s+(?:plaintiff|defendant|[A-Z]))"),
            re.compile(r"(?i)((?:OBJECTION|Object(?:s|ing))[:.]\s|subject\s+to\s+(?:the\s+(?:foregoing|above)\s+)?(?:general\s+)?objection)"),
            re.compile(r"(?i)(without\s+waiving|responsive\s+documents?|see\s+(?:attached|documents?\s+produced))"),
        ],
        "min_matches": 2,
    },
    "subpoena": {
        "markers": [
            re.compile(r"(?i)(subpoena\s+(?:duces\s+tecum|ad\s+testificandum|for\s+(?:deposition|trial|hearing|production)))"),
            re.compile(r"(?i)(you\s+are\s+(?:hereby\s+)?commanded|commanded\s+to\s+(?:appear|produce|attend))"),
            re.compile(r"(?i)(failure\s+to\s+(?:comply|appear|obey)|contempt\s+of\s+court)"),
        ],
        "min_matches": 1,
    },
    "correspondence": {
        "markers": [
            re.compile(r"(?i)(dear\s+(mr|ms|mrs|counsel|judge)|re:|sincerely|regards|very\s+truly\s+yours)"),
            re.compile(r"(?i)(via\s+(email|facsimile|hand\s+delivery|certified\s+mail))"),
        ],
        "min_matches": 1,
    },
    "contract": {
        "markers": [
            re.compile(r"(?i)(agreement|contract|lease|deed|note|instrument|covenant)"),
            re.compile(r"(?i)(whereas|now\s+therefore|witnesseth|hereinafter|party\s+of\s+the\s+(first|second)\s+part)"),
            re.compile(r"(?i)(consideration|indemnif|warrant|represent)"),
        ],
        "min_matches": 2,
    },
    "deposition": {
        "markers": [
            re.compile(r"(?i)(deposition|oral\s+examination|sworn\s+testimony)"),
            re.compile(r"(?i)(q\.\s|a\.\s|by\s+(mr|ms|mrs)\.\s+\w+:)"),
            re.compile(r"(?i)(court\s+reporter|notary\s+public|certified\s+shorthand)"),
        ],
        "min_matches": 2,
    },
    "scheduling_order": {
        "markers": [
            re.compile(r"(?i)(scheduling\s+order|docket\s+control\s+order|case\s+management\s+order)"),
            re.compile(r"(?i)(discovery\s+(?:cut[\- ]?off|deadline)|trial\s+(?:date|setting)|mediation)"),
        ],
        "min_matches": 1,
    },
    "financial_statement": {
        "markers": [
            re.compile(r"(?i)(balance\s+sheet|income\s+statement|profit\s+and\s+loss|trial\s+balance)"),
            re.compile(r"(?i)(total\s+(assets|liabilities|equity|revenue)|net\s+(income|loss))"),
            re.compile(r"(?i)(accounts\s+(receivable|payable)|retained\s+earnings)"),
        ],
        "min_matches": 1,
    },
    "invoice": {
        "markers": [
            re.compile(r"(?i)(invoice|bill\s+to|amount\s+due|payment\s+terms|remit\s+to)"),
            re.compile(r"(?i)(invoice\s+(number|#|no)|total\s+due|balance\s+due)"),
        ],
        "min_matches": 1,
    },
    "real_estate": {
        "markers": [
            re.compile(r"(?i)(warranty\s+deed|deed\s+of\s+trust|promissory\s+note|closing\s+statement)"),
            re.compile(r"(?i)(title\s+(commitment|policy|insurance)|survey|legal\s+description)"),
            re.compile(r"(?i)(grantor|grantee|borrower|lender|beneficiary|trustee)"),
        ],
        "min_matches": 2,
    },
    "corporate": {
        "markers": [
            re.compile(r"(?i)(articles\s+of\s+(incorporation|organization)|bylaws|operating\s+agreement)"),
            re.compile(r"(?i)(certificate\s+of\s+(formation|good\s+standing)|registered\s+agent)"),
            re.compile(r"(?i)(board\s+of\s+directors|shareholder|member|manager)"),
        ],
        "min_matches": 2,
    },
    "email": {
        "markers": [
            re.compile(r"(?i)(from:|to:|cc:|bcc:|subject:|sent:|date:)"),
            re.compile(r"(?i)(@\w+\.\w+)"),
        ],
        "min_matches": 2,
    },
}


def classify_text(text: str, file_path: str) -> tuple[str, float]:
    """Classify document text. Returns (doc_type_code, confidence).

    Priority types (scheduling_order, deposition) are checked first and win
    over generic types like pleading when both match, because these document
    types carry the most critical litigation data (deadlines, testimony).
    """
    if not text or len(text.strip()) < 20:
        return "unknown", 0.0

    # ── Priority types: check these FIRST ────────────────────────────
    # Scheduling orders are the backbone of every litigation matter.
    # They often match pleading markers too (cause no, plaintiff, order)
    # but must be classified as scheduling_order for deadline extraction.
    PRIORITY_TYPES = ["scheduling_order", "deposition", "court_order", "judgment", "affidavit", "expert_report", "disclosure", "subpoena"]

    text_head = text[:8000]  # Expanded from 5000 — scheduling order dates can be deep
    folder_lower = (file_path or "").lower()

    for ptype in PRIORITY_TYPES:
        config = DOC_TYPE_PATTERNS.get(ptype)
        if not config:
            continue
        match_count = 0
        for marker in config["markers"]:
            if marker.search(text_head):
                match_count += 1
        if match_count >= config["min_matches"]:
            # Content match — high confidence
            return ptype, round(min(1.0, match_count / len(config["markers"]) + 0.1), 2)

    # Folder/filename boost can also force priority type
    priority_folder_keywords = {
        "scheduling_order": ["scheduling order", "scheduling_order", "docket control",
                             "case management order"],
        "deposition": ["deposition", "depo transcript"],
        "court_order": ["order", "ruling"],
        "judgment": ["judgment", "final judgment"],
        "affidavit": ["affidavit", "declaration", "sworn"],
        "expert_report": ["expert report", "expert opinion"],
        "disclosure": ["disclosure", "rule 194", "rule 26"],
        "subpoena": ["subpoena"],
    }
    for ptype, keywords in priority_folder_keywords.items():
        for kw in keywords:
            if kw in folder_lower:
                # Filename says scheduling order — trust it even if content
                # markers are borderline (e.g. proposed scheduling order)
                config = DOC_TYPE_PATTERNS.get(ptype, {})
                match_count = 0
                for marker in config.get("markers", []):
                    if marker.search(text_head):
                        match_count += 1
                if match_count >= 1:  # At least one content marker + folder match
                    return ptype, 0.90
                # Folder match alone with no content — still classify but lower confidence
                return ptype, 0.60

    # ── Standard classification for everything else ──────────────────
    scores = {}
    for doc_type, config in DOC_TYPE_PATTERNS.items():
        if doc_type in PRIORITY_TYPES:
            continue  # Already checked above
        match_count = 0
        for marker in config["markers"]:
            if marker.search(text_head):
                match_count += 1
        if match_count >= config["min_matches"]:
            scores[doc_type] = match_count / len(config["markers"])

    # Folder context boost (non-priority types only)
    folder_boosts = {
        "discovery": ["discovery", "production", "rfp", "rog", "rfa", "interrogator"],
        "discovery_response": ["response", "answers"],
        "correspondence": ["correspondence", "letters", "emails"],
        "pleading": ["pleading", "filing"],
        "motion": ["motion", "brief"],
        "brief": ["brief", "response", "reply", "memorandum"],
        "financial_statement": ["financial", "accounting", "bank"],
        "contract": ["contract", "agreement", "lease", "closing", "settlement"],
        "real_estate": ["real estate", "closing", "title"],
        "corporate": ["corporate", "formation", "entity"],
        "expert_report": ["expert", "report"],
    }
    for doc_type, keywords in folder_boosts.items():
        for kw in keywords:
            if kw in folder_lower:
                if doc_type not in scores:
                    scores[doc_type] = 0.3
                else:
                    scores[doc_type] = min(1.0, scores[doc_type] + 0.2)
                break

    if not scores:
        return "unknown", 0.3

    best = max(scores, key=scores.get)
    return best, round(min(1.0, scores[best]), 2)


def main():
    parser = argparse.ArgumentParser(description="Run extraction templates on existing documents")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--type", default=None, help="Only process this document type")
    parser.add_argument("--re-extract", action="store_true",
                        help="Re-extract even if already extracted")
    parser.add_argument("--path-like", default=None,
                        help="SQL LIKE pattern to scope by file_path "
                             "(e.g. '%%/matters/Weir/Victory/%%')")
    args = parser.parse_args()

    tid = args.tenant.strip()

    log.info("=== Extraction Template Engine Run ===")
    log.info("Tenant: %s", tid)

    conn = get_db_conn()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Create extraction_run
    run_id = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO extraction_runs
        (id, tenant_id, run_type, source_type, status, started_at)
        VALUES (%s, %s, 'template_extraction', 'legacy_inventory', 'processing', NOW())
    """, (run_id, tid))

    # Find documents with text
    where = ["TRIM(tenant_id) = %s", "content_text IS NOT NULL", "length(content_text) > 50"]
    params = [tid]

    if args.path_like:
        where.append("file_path LIKE %s")
        params.append(args.path_like)

    if not args.re_extract:
        # Skip docs that already have extraction data
        where.append("""
            id NOT IN (
                SELECT DISTINCT dms_document_id FROM document_sections
                WHERE TRIM(tenant_id) = %s AND superseded_by_run_id IS NULL
                  AND dms_document_id IS NOT NULL
            )
        """)
        params.append(tid)

    sql = f"SELECT id, file_path, content_text FROM dms_documents WHERE {' AND '.join(where)} ORDER BY file_path"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"

    cur.execute(sql, params)
    docs = cur.fetchall()
    log.info("Found %d documents to process", len(docs))

    cur.execute("UPDATE extraction_runs SET document_count = %s WHERE id = %s",
                (len(docs), run_id))

    # Import the engine
    from jobs.extraction_template_engine import run_extraction_template

    # Track stats
    stats = {
        "processed": 0, "sections": 0, "parties": 0,
        "defined_terms": 0, "deadlines": 0, "causes_of_action": 0,
        "failed": 0, "skipped_type": 0,
        "by_type": {},
    }

    for i, doc in enumerate(docs):
        doc_id = str(doc["id"])
        fpath = doc["file_path"] or ""
        fname = Path(fpath).name if fpath else "?"
        text = doc["content_text"] or ""

        # Classify
        doc_type, confidence = classify_text(text, fpath)

        if args.type and doc_type != args.type:
            stats["skipped_type"] += 1
            continue

        stats["by_type"][doc_type] = stats["by_type"].get(doc_type, 0) + 1

        # Look up matter_id from file_inventory if available
        matter_id = None
        try:
            cur.execute("""
                SELECT proposed_matter_id FROM file_inventory
                WHERE full_path = %s AND tenant_id = %s AND proposed_matter_id IS NOT NULL
                LIMIT 1
            """, (fpath, tid))
            row = cur.fetchone()
            if row:
                matter_id = str(row["proposed_matter_id"])
        except Exception:
            pass

        # Run extraction
        try:
            result = run_extraction_template(
                tenant_id=tid,
                document_id=doc_id,
                run_id=run_id,
                content_text=text,
                document_type_code=doc_type,
                source_table="dms",
                matter_id=matter_id,
                file_path=fpath,
            )

            if result.get("error"):
                log.warning("  [%d/%d] FAIL %s (%s): %s",
                            i+1, len(docs), fname[:50], doc_type, result["error"][:80])
                stats["failed"] += 1
            else:
                sec = result.get("sections", 0)
                par = result.get("parties", 0)
                trm = result.get("defined_terms", 0)
                ddl = result.get("deadlines", 0)
                coa = result.get("causes_of_action", 0)
                method = result.get("extraction_method", "?")

                stats["processed"] += 1
                stats["sections"] += sec
                stats["parties"] += par
                stats["defined_terms"] += trm
                stats["deadlines"] += ddl
                stats["causes_of_action"] += coa

                log.info("  [%d/%d] %s (%s) → sec=%d par=%d trm=%d ddl=%d coa=%d [%s]",
                         i+1, len(docs), fname[:45], doc_type,
                         sec, par, trm, ddl, coa, method)

        except Exception as exc:
            log.error("  [%d/%d] ERROR %s: %s", i+1, len(docs), fname[:50], exc)
            stats["failed"] += 1

        # Update extraction_run progress
        if (i + 1) % 25 == 0:
            cur.execute("""
                UPDATE extraction_runs
                SET documents_processed = %s, documents_failed = %s
                WHERE id = %s
            """, (stats["processed"], stats["failed"], run_id))

    # Mark run complete
    cur.execute("""
        UPDATE extraction_runs
        SET status = 'completed', completed_at = NOW(),
            documents_processed = %s, documents_failed = %s
        WHERE id = %s
    """, (stats["processed"], stats["failed"], run_id))

    cur.close()
    conn.close()

    # Summary
    log.info("")
    log.info("=== Extraction Summary ===")
    log.info("  Run ID:         %s", run_id)
    log.info("  Documents:      %d processed, %d failed, %d skipped", 
             stats["processed"], stats["failed"], stats["skipped_type"])
    log.info("  Sections:       %d", stats["sections"])
    log.info("  Parties:        %d", stats["parties"])
    log.info("  Defined Terms:  %d", stats["defined_terms"])
    log.info("  Deadlines:      %d", stats["deadlines"])
    log.info("  Causes/Action:  %d", stats["causes_of_action"])
    log.info("  By type:")
    for dt, cnt in sorted(stats["by_type"].items(), key=lambda x: -x[1]):
        log.info("    %-25s %d docs", dt, cnt)
    log.info("=== Done ===")


if __name__ == "__main__":
    main()
