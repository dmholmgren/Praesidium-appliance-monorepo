#!/usr/bin/env python3
"""
jobs/ingest_sample.py
Sample Ingestion Job — picks a stratified random sample from file_inventory,
extracts text, classifies document type, writes to dms_documents,
and creates parsing_strategy_rules entries for each document pattern encountered.

The learning loop: each new structural pattern (e.g., "PDF with Bates prefix",
"DOCX with court caption", "financial statement with header row") gets a rule
in parsing_strategy_rules. Future ingestion checks rules before escalating
to AI — if a rule exists with success_rate > threshold, apply it directly.

Usage:
    sudo docker exec praesidium-web python3 /app/jobs/ingest_sample.py \
        --tenant 986c0fee-1390-43bb-ad28-8cd1db6de53f \
        --sample-size 100

    # Smaller test:
    --sample-size 20

    # Only specific extensions:
    --extensions pdf,docx
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

log = logging.getLogger("ingest_sample")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# ── Document type classification patterns ─────────────────────────────────────

DOC_TYPE_PATTERNS = {
    "pleading": {
        "markers": [
            re.compile(r"(?i)(cause\s+no|case\s+no|civil\s+action|in\s+the\s+(district|county|circuit)\s+court)"),
            re.compile(r"(?i)(plaintiff|defendant|petitioner|respondent)"),
            re.compile(r"(?i)(motion|brief|complaint|answer|petition|order|judgment)"),
        ],
        "min_matches": 2,
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
    "discovery": {
        "markers": [
            re.compile(r"(?i)(interrogator|request\s+for\s+(production|admission)|subpoena\s+duces)"),
            re.compile(r"(?i)(propound|serve|answer\s+(to|the)\s+(following|interrogator))"),
        ],
        "min_matches": 1,
    },
    "deposition": {
        "markers": [
            re.compile(r"(?i)(deposition|oral\s+examination|sworn\s+testimony)"),
            re.compile(r"(?i)(q\.\s|a\.\s|by\s+(mr|ms|mrs)\.\s+\w+:)"),
            re.compile(r"(?i)(court\s+reporter|notary\s+public|certified\s+shorthand)"),
        ],
        "min_matches": 2,
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
    "email_printed": {
        "markers": [
            re.compile(r"(?i)(from:|to:|cc:|bcc:|subject:|sent:|date:)"),
            re.compile(r"(?i)(@\w+\.\w+)"),
        ],
        "min_matches": 2,
    },
}

# Section delimiter patterns for structural analysis
SECTION_PATTERNS = {
    "numbered_sections": re.compile(r"^\s*(\d+\.|\([a-z]\)|\([0-9]+\))\s+", re.MULTILINE),
    "article_sections": re.compile(r"(?i)^(article|section)\s+[IVXLCDM\d]+", re.MULTILINE),
    "exhibit_markers": re.compile(r"(?i)^exhibit\s+[A-Z0-9]", re.MULTILINE),
    "bates_numbers": re.compile(r"[A-Z]{2,10}[-_]?\d{4,8}"),
    "page_numbers": re.compile(r"(?i)page\s+\d+\s+of\s+\d+"),
    "signature_blocks": re.compile(r"(?i)(respectfully\s+submitted|by:\s*/s/|_{20,})"),
    "caption_block": re.compile(r"(?i)(cause\s+no|case\s+no|no\.\s+\d).*\n.*v[s]?\.\s*\n"),
    "closing_formulas": re.compile(r"(?i)(wherefore|prayer\s+for\s+relief|respectfully\s+(request|submit))"),
}


def classify_document(text: str, filename: str, folder_path: str) -> dict:
    """
    Classify a document based on text content, filename, and folder context.
    Returns {doc_type, confidence, structural_markers, section_delimiters}.
    """
    if not text or len(text.strip()) < 20:
        return {
            "doc_type": "unreadable",
            "confidence": 0.0,
            "structural_markers": {},
            "section_delimiters": {},
        }

    # Score each document type
    scores = {}
    for doc_type, config in DOC_TYPE_PATTERNS.items():
        match_count = 0
        matched_markers = []
        for marker in config["markers"]:
            if marker.search(text[:5000]):  # Check first 5K chars
                match_count += 1
                matched_markers.append(marker.pattern[:50])
        if match_count >= config["min_matches"]:
            scores[doc_type] = {
                "score": match_count / len(config["markers"]),
                "matched": matched_markers,
            }

    # Folder path context boost
    folder_lower = folder_path.lower()
    folder_boosts = {
        "deposition": ["deposition", "depo", "transcript"],
        "discovery": ["discovery", "production", "rfp", "rog"],
        "correspondence": ["correspondence", "letters", "emails"],
        "pleading": ["pleading", "motion", "brief", "filing"],
        "financial_statement": ["financial", "accounting", "bank"],
        "contract": ["contract", "agreement", "lease", "closing"],
        "real_estate": ["real estate", "closing", "title"],
        "corporate": ["corporate", "formation", "entity"],
    }
    for doc_type, keywords in folder_boosts.items():
        for kw in keywords:
            if kw in folder_lower:
                if doc_type not in scores:
                    scores[doc_type] = {"score": 0.3, "matched": [f"folder:{kw}"]}
                else:
                    scores[doc_type]["score"] = min(1.0, scores[doc_type]["score"] + 0.2)
                break

    # Pick best
    if not scores:
        best_type = "general"
        confidence = 0.3
        matched = []
    else:
        best_type = max(scores, key=lambda k: scores[k]["score"])
        confidence = min(1.0, scores[best_type]["score"])
        matched = scores[best_type]["matched"]

    # Detect structural patterns
    structural = {}
    for name, pattern in SECTION_PATTERNS.items():
        matches = pattern.findall(text[:10000])
        if matches:
            structural[name] = len(matches)

    # Detect section delimiters
    section_delims = {}
    if structural.get("numbered_sections", 0) > 3:
        section_delims["type"] = "numbered"
    elif structural.get("article_sections", 0) > 1:
        section_delims["type"] = "article"
    elif structural.get("exhibit_markers", 0) > 0:
        section_delims["type"] = "exhibit"

    return {
        "doc_type": best_type,
        "confidence": round(confidence, 2),
        "structural_markers": structural,
        "section_delimiters": section_delims,
        "all_scores": {k: round(v["score"], 2) for k, v in scores.items()},
    }


def get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return "host=localhost port=5432 dbname=praesidium user=praesidium"
    return url.replace("postgresql+asyncpg://", "postgresql://")


def main():
    parser = argparse.ArgumentParser(description="Ingest sample documents")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--extensions", default="pdf,docx,doc,txt,rtf,xlsx",
                        help="Comma-separated extensions (no dots)")
    args = parser.parse_args()

    tid = args.tenant.strip()
    sample_size = args.sample_size
    exts = [f".{e.strip().lstrip('.')}" for e in args.extensions.split(",")]

    log.info("=== Sample Ingestion Job ===")
    log.info("Tenant:      %s", tid)
    log.info("Sample size: %d", sample_size)
    log.info("Extensions:  %s", exts)

    conn = psycopg2.connect(get_db_url())

    try:
        # ── 1. Pick stratified sample from file_inventory ─────────────────
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ext_placeholders = ",".join(["%s"] * len(exts))
            cur.execute(f"""
                SELECT id, full_path, name, extension, size_bytes,
                       root_folder, proposed_client_name, proposed_client_id,
                       proposed_matter_id, match_method, match_confidence,
                       is_active_matter, depth
                FROM file_inventory
                WHERE tenant_id = %s
                  AND entry_type = 'file'
                  AND classification = 'document'
                  AND match_method IS NOT NULL
                  AND size_bytes BETWEEN 5000 AND 10000000
                  AND extension IN ({ext_placeholders})
                ORDER BY random()
                LIMIT %s
            """, [tid] + exts + [sample_size])
            sample = cur.fetchall()

        log.info("Selected %d files for ingestion", len(sample))

        # ── 2. Process each file ──────────────────────────────────────────
        from modules.ediscovery.services.text_extraction import extract_text

        stats = {
            "processed": 0, "extracted": 0, "failed": 0,
            "doc_types": {}, "rules_created": 0,
        }
        strategy_cache = {}  # doc_type → rule_id (avoid duplicate rules)

        for i, file_row in enumerate(sample):
            fpath = file_row["full_path"]
            fname = file_row["name"]
            ext = (file_row["extension"] or "").lower().lstrip(".")

            # Map extension to doc_type for text_extraction
            ext_to_doctype = {
                "pdf": "pdf", "docx": "word", "doc": "word",
                "txt": "text", "rtf": "text",
                "xlsx": "spreadsheet", "xls": "spreadsheet",
                "htm": "html", "html": "html",
                "msg": "email", "eml": "email",
            }
            extract_type = ext_to_doctype.get(ext, "text")

            try:
                if not os.path.exists(fpath):
                    log.warning("  [%d] File not found: %s", i, fpath)
                    stats["failed"] += 1
                    continue

                # Extract text
                text, page_count = extract_text(fpath, extract_type)

                if not text or len(text.strip()) < 20:
                    log.debug("  [%d] No text extracted: %s", i, fname)
                    stats["failed"] += 1
                    continue

                stats["extracted"] += 1

                # Classify
                classification = classify_document(
                    text, fname, file_row["full_path"]
                )
                doc_type = classification["doc_type"]
                stats["doc_types"][doc_type] = stats["doc_types"].get(doc_type, 0) + 1

                # Compute file hash
                file_hash = hashlib.sha256(
                    open(fpath, "rb").read(65536)  # First 64KB for speed
                ).hexdigest()

                # ── Write to dms_documents ────────────────────────────────
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO dms_documents (
                            tenant_id, file_path, folder_root,
                            file_hash, file_size_bytes, modified_at,
                            content_text, ocr_status, extraction_status,
                            source
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT DO NOTHING
                    """, (
                        tid,
                        fpath,
                        file_row["root_folder"],
                        file_hash,
                        file_row["size_bytes"],
                        datetime.now(timezone.utc),
                        text[:50000],  # Cap at 50K chars
                        "not_applicable" if extract_type != "pdf" else (
                            "ocr_complete" if page_count and page_count > 0 else "not_applicable"
                        ),
                        "complete",
                        "legacy_inventory",
                    ))

                # ── Create/update parsing_strategy_rule ───────────────────
                if doc_type not in strategy_cache:
                    rule_id = str(uuid.uuid4())
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO parsing_strategy_rules (
                                id, source_domain, document_type,
                                confidence_threshold,
                                structural_markers, section_delimiters,
                                extraction_template,
                                learned_from, learned_at,
                                times_applied, times_succeeded,
                                success_rate, notes
                            ) VALUES (
                                %s::uuid, %s, %s, %s,
                                %s::jsonb, %s::jsonb, %s::jsonb,
                                %s, now(), 1, 1, 1.0, %s
                            )
                            ON CONFLICT DO NOTHING
                        """, (
                            rule_id,
                            "legacy_filesystem",
                            doc_type,
                            classification["confidence"],
                            json.dumps(classification["structural_markers"]),
                            json.dumps(classification.get("section_delimiters", {})),
                            json.dumps({
                                "extract_type": extract_type,
                                "text_cap": 50000,
                                "classify_patterns": list(classification.get("all_scores", {}).keys()),
                            }),
                            fname,
                            f"Auto-learned from sample ingestion. "
                            f"Structural: {json.dumps(classification['structural_markers'])}",
                        ))
                    strategy_cache[doc_type] = rule_id
                    stats["rules_created"] += 1
                    log.info("  [%d] NEW RULE: %s (from %s, conf=%.2f)",
                             i, doc_type, fname, classification["confidence"])
                else:
                    # Increment times_applied on existing rule
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE parsing_strategy_rules
                            SET times_applied = times_applied + 1,
                                times_succeeded = times_succeeded + 1,
                                last_applied_at = now()
                            WHERE id = %s::uuid
                        """, (strategy_cache[doc_type],))

                stats["processed"] += 1

                if (i + 1) % 10 == 0:
                    conn.commit()
                    log.info("  Processed %d/%d (extracted=%d, failed=%d, rules=%d)",
                             i + 1, len(sample), stats["extracted"],
                             stats["failed"], stats["rules_created"])

            except Exception as e:
                log.error("  [%d] Error processing %s: %s", i, fname, e)
                stats["failed"] += 1
                conn.rollback()
                continue

        conn.commit()

        # ── 3. Summary ────────────────────────────────────────────────────
        log.info("=== Ingestion Summary ===")
        log.info("  Files processed:  %d", stats["processed"])
        log.info("  Text extracted:   %d", stats["extracted"])
        log.info("  Failed:           %d", stats["failed"])
        log.info("  Rules created:    %d", stats["rules_created"])
        log.info("  Document types discovered:")
        for dt, cnt in sorted(stats["doc_types"].items(), key=lambda x: -x[1]):
            log.info("    %-25s %d files", dt, cnt)

        # Print rules summary
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT document_type, confidence_threshold,
                       times_applied, times_succeeded, success_rate,
                       structural_markers::text, learned_from
                FROM parsing_strategy_rules
                WHERE source_domain = 'legacy_filesystem'
                ORDER BY times_applied DESC
            """)
            rules = cur.fetchall()
            log.info("  Parsing strategy rules:")
            for r in rules:
                log.info("    %-25s conf=%.2f applied=%d from=%s",
                         r["document_type"], float(r["confidence_threshold"]),
                         r["times_applied"], r["learned_from"][:40])

    finally:
        conn.close()

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
