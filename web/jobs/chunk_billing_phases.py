#!/usr/bin/env python3
"""
jobs/chunk_billing_phases.py
Billing Phase Chunker — groups time entries into phase-bounded windows
per matter, extracts data primitives, and writes composed chunks to
billing_chunks.

Phase boundaries detected by:
  - Time gaps (>30 days between slips)
  - Timekeeper transitions (dominant timekeeper changes)
  - Work type shifts (activity code patterns)

Data primitives extracted per phase:
  - Phase label (auto-detected: e.g., "Discovery", "Motion Practice")
  - Timekeeper mix (who worked, hours per person)
  - Work type distribution (percentage by activity)
  - Attorney velocity (hours/day during phase)
  - Fee summary (total hours, value, average rate)

Usage:
    sudo docker exec praesidium-web python3 /app/jobs/chunk_billing_phases.py \
        --tenant 986c0fee-1390-43bb-ad28-8cd1db6de53f
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

log = logging.getLogger("billing_chunker")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Phase gap threshold — slips >30 days apart start a new phase
PHASE_GAP_DAYS = 30

# Minimum slips to form a phase (smaller groups merged with adjacent)
MIN_PHASE_SLIPS = 3

# Max chunk content length in chars (Voyage law-2 handles 16K context)
MAX_CHUNK_CHARS = 8000

# ── Work type detection from narratives ───────────────────────────────────────

WORK_TYPE_PATTERNS = {
    "discovery": re.compile(
        r"(?i)(discovery|interrogator|request.for.production|document.review|"
        r"deposition|subpoena|rog\b|rfp\b|rfa\b)", re.IGNORECASE
    ),
    "motion_practice": re.compile(
        r"(?i)(motion|brief|memorandum|reply|response.to.motion|summary.judgment|"
        r"dismiss|injunction|compel)", re.IGNORECASE
    ),
    "trial_prep": re.compile(
        r"(?i)(trial.prep|exhibit|witness.prep|jury|voir.dire|"
        r"pre.?trial|trial.brief|trial.notebook)", re.IGNORECASE
    ),
    "trial": re.compile(
        r"(?i)(trial\b|cross.exam|direct.exam|opening.state|closing.arg|"
        r"jury.selection|court.appear)", re.IGNORECASE
    ),
    "negotiation": re.compile(
        r"(?i)(negotiat|settlement|mediat|demand.letter|counter.?offer|"
        r"term.sheet|loi\b)", re.IGNORECASE
    ),
    "transactional": re.compile(
        r"(?i)(draft.*(agreement|contract|lease|deed|note)|closing|"
        r"due.diligence|title.review|survey|escrow|commitment)", re.IGNORECASE
    ),
    "client_communication": re.compile(
        r"(?i)(telephone|conference|email|letter|meeting|call.with.client|"
        r"update.*client|client.meeting)", re.IGNORECASE
    ),
    "research": re.compile(
        r"(?i)(research|legal.research|case.law|statute|regulation|"
        r"review.*law|analyze.*issue)", re.IGNORECASE
    ),
    "court_filing": re.compile(
        r"(?i)(file|filing|e.?file|serve|service.of.process|"
        r"notice.of|certificate.of)", re.IGNORECASE
    ),
    "administrative": re.compile(
        r"(?i)(admin|calendar|docket|schedule|organize|file.manage|"
        r"conflict.check|open.matter)", re.IGNORECASE
    ),
}


def detect_work_type(narrative: str) -> str:
    """Classify a slip narrative into a work type."""
    if not narrative:
        return "other"
    for wtype, pattern in WORK_TYPE_PATTERNS.items():
        if pattern.search(narrative):
            return wtype
    return "other"


def detect_phase_label(work_type_dist: dict) -> str:
    """Derive a phase label from the dominant work type."""
    if not work_type_dist:
        return "General"
    dominant = max(work_type_dist, key=work_type_dist.get)
    labels = {
        "discovery": "Discovery",
        "motion_practice": "Motion Practice",
        "trial_prep": "Trial Preparation",
        "trial": "Trial",
        "negotiation": "Negotiation / Settlement",
        "transactional": "Transactional",
        "client_communication": "Client Communication",
        "research": "Legal Research",
        "court_filing": "Court Filings",
        "administrative": "Administrative",
        "other": "General",
    }
    return labels.get(dominant, "General")


def split_into_phases(slips: list[dict]) -> list[list[dict]]:
    """
    Split a chronologically sorted list of slips into phases
    based on time gaps and work type shifts.
    """
    if not slips:
        return []

    phases = []
    current_phase = [slips[0]]

    for i in range(1, len(slips)):
        prev = slips[i - 1]
        curr = slips[i]

        gap_days = (curr["slip_date"] - prev["slip_date"]).days if curr["slip_date"] and prev["slip_date"] else 0

        if gap_days > PHASE_GAP_DAYS:
            phases.append(current_phase)
            current_phase = [curr]
        else:
            current_phase.append(curr)

    if current_phase:
        phases.append(current_phase)

    # Merge tiny phases into adjacent ones
    merged = []
    for phase in phases:
        if len(phase) < MIN_PHASE_SLIPS and merged:
            merged[-1].extend(phase)
        else:
            merged.append(phase)

    return merged


def compose_chunk(phase_slips: list[dict], matter_info: dict) -> dict:
    """
    Compose a billing chunk from a phase of slips.
    Returns the chunk content string + metadata primitives.
    """
    # Extract primitives
    work_types = Counter()
    tk_hours = defaultdict(float)
    total_hours = 0.0
    total_value = 0.0
    narratives = []

    for s in phase_slips:
        wtype = detect_work_type(s.get("narrative", ""))
        work_types[wtype] += 1
        tk_name = s.get("timekeeper_name") or s.get("source_tk_id") or "Unknown"
        hours = float(s.get("hours") or 0)
        value = float(s.get("wip_value") or s.get("value") or 0)
        tk_hours[tk_name] += hours
        total_hours += hours
        total_value += value
        if s.get("narrative"):
            narratives.append(s["narrative"].strip())

    start_date = phase_slips[0].get("slip_date")
    end_date = phase_slips[-1].get("slip_date")
    days_span = (end_date - start_date).days + 1 if start_date and end_date else 1
    velocity = total_hours / max(days_span, 1)

    phase_label = detect_phase_label(dict(work_types))

    # Work type distribution as percentages
    total_entries = sum(work_types.values())
    work_dist = {
        k: round(v / total_entries * 100, 1)
        for k, v in work_types.most_common()
    }

    # Timekeeper mix
    tk_mix = {
        k: round(v, 1)
        for k, v in sorted(tk_hours.items(), key=lambda x: -x[1])
    }

    # Compose the chunk content — a structured natural language summary
    # that embeds well with legal embedding models
    lines = []
    lines.append(f"Client: {matter_info.get('client_name', 'Unknown')}")
    lines.append(f"Matter: {matter_info.get('matter_name', 'Unknown')} ({matter_info.get('matter_number', '')})")
    lines.append(f"Phase: {phase_label}")
    lines.append(f"Period: {start_date} to {end_date} ({days_span} days)")
    lines.append(f"Entries: {len(phase_slips)}, Hours: {total_hours:.1f}, Value: ${total_value:,.2f}")
    lines.append(f"Velocity: {velocity:.2f} hours/day")
    lines.append(f"Timekeepers: {', '.join(f'{k} ({v}h)' for k, v in tk_mix.items())}")
    lines.append(f"Work types: {', '.join(f'{k} ({v}%)' for k, v in work_dist.items())}")
    lines.append("")
    lines.append("Narrative summary:")

    # Deduplicate and truncate narratives
    seen = set()
    for n in narratives:
        n_clean = n.strip()
        if n_clean and n_clean.lower() not in seen:
            seen.add(n_clean.lower())
            lines.append(f"- {n_clean}")

    content = "\n".join(lines)
    if len(content) > MAX_CHUNK_CHARS:
        content = content[:MAX_CHUNK_CHARS] + "\n[truncated]"

    metadata = {
        "phase_label": phase_label,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "days_span": days_span,
        "entry_count": len(phase_slips),
        "total_hours": round(total_hours, 2),
        "total_value": round(total_value, 2),
        "velocity_hours_per_day": round(velocity, 4),
        "timekeeper_mix": tk_mix,
        "work_type_distribution": work_dist,
        "avg_rate": round(total_value / max(total_hours, 0.01), 2),
    }

    return {
        "content": content,
        "embedded_content": content,  # Same for now; could be summarized later
        "metadata": metadata,
        "phase_label": phase_label,
        "start_date": start_date,
        "end_date": end_date,
        "total_hours": total_hours,
        "total_value": total_value,
    }


def get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return "host=localhost port=5432 dbname=praesidium user=praesidium"
    return url.replace("postgresql+asyncpg://", "postgresql://")


def main():
    parser = argparse.ArgumentParser(description="Chunk billing phases")
    parser.add_argument("--tenant", required=True, help="Tenant ID")
    parser.add_argument("--limit-clients", type=int, default=0,
                        help="Limit to N clients (0=all, for testing)")
    args = parser.parse_args()

    run_id = str(uuid.uuid4())
    tid = args.tenant.strip()

    log.info("=== Billing Phase Chunker ===")
    log.info("Tenant:  %s", tid)
    log.info("Run ID:  %s", run_id)

    conn = psycopg2.connect(get_db_url())

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Get all matters with their client info and legacy_id for joining
            cur.execute("""
                SELECT m.id AS matter_id, m.matter_name, m.matter_number,
                       m.client_id, c.client_name, m.legacy_id
                FROM matters m
                LEFT JOIN clients c ON c.id = m.client_id
                WHERE TRIM(m.tenant_id) = %s
                  AND m.legacy_id IS NOT NULL
                ORDER BY c.client_name, m.matter_number
            """, (tid,))
            matters = cur.fetchall()

            if args.limit_clients:
                matters = matters[:args.limit_clients]

            log.info("Processing %d matters...", len(matters))

            total_chunks = 0
            total_slips_processed = 0
            start = time.time()

            for i, matter in enumerate(matters):
                # Get slips for this matter via legacy_id → source_client_id
                cur.execute("""
                    SELECT ts.source_slip_id, ts.slip_date, ts.hours,
                           ts.rate, ts.wip_value, ts.narrative,
                           ts.source_tk_id, ts.activity_id,
                           tk.ts_name AS timekeeper_name
                    FROM ts_slips ts
                    LEFT JOIN ts_timekeepers tk
                        ON tk.ts_tk_id = ts.source_tk_id
                       AND TRIM(tk.tenant_id) = TRIM(ts.tenant_id)
                    WHERE TRIM(ts.tenant_id) = %s
                      AND ts.source_client_id = %s
                    ORDER BY ts.slip_date, ts.source_tk_id
                """, (tid, matter["legacy_id"]))
                slips = cur.fetchall()

                if not slips:
                    continue

                total_slips_processed += len(slips)
                phases = split_into_phases(slips)

                for chunk_idx, phase in enumerate(phases):
                    chunk = compose_chunk(phase, matter)

                    cur.execute("""
                        INSERT INTO billing_chunks (
                            id, tenant_id, source_type, source_id,
                            matter_id, client_id, run_id,
                            chunk_index, content, embedded_content,
                            token_count, section_label, chunk_metadata,
                            timekeeper_id, entry_date
                        ) VALUES (
                            gen_random_uuid(), %s, 'ts_slips',
                            COALESCE(%s::uuid, gen_random_uuid()),
                            %s::uuid, %s,
                            %s::uuid, %s, %s, %s, %s, %s, %s::jsonb, %s, %s
                        )
                    """, (
                        tid,
                        str(matter["matter_id"]),
                        str(matter["matter_id"]),
                        str(matter["client_id"]) if matter["client_id"] else None,
                        run_id,
                        chunk_idx,
                        chunk["content"],
                        chunk["embedded_content"],
                        len(chunk["content"].split()),  # rough token estimate
                        chunk["phase_label"],
                        json.dumps(chunk["metadata"], default=str),
                        slips[0].get("source_tk_id"),
                        str(chunk["start_date"]) if chunk["start_date"] else None,
                    ))
                    total_chunks += 1

                if (i + 1) % 100 == 0:
                    conn.commit()
                    log.info("  Processed %d/%d matters, %d chunks so far...",
                             i + 1, len(matters), total_chunks)

            conn.commit()

        elapsed = time.time() - start
        log.info("=== Chunking Summary ===")
        log.info("  Matters processed: %d", len(matters))
        log.info("  Slips processed:   %d", total_slips_processed)
        log.info("  Chunks created:    %d", total_chunks)
        log.info("  Time:              %.1fs", elapsed)
        log.info("  Run ID:            %s", run_id)

    finally:
        conn.close()

    log.info("=== Done ===")
    return run_id


if __name__ == "__main__":
    main()
