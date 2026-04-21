"""
jobs/issue_map_job.py

RQ job: run_issue_map_refresh
Queue: ediscovery
Timeout: 600 seconds

Pulls all trigger-eligible documents for a matter, calls AIService to extract
the structured issue map, diffs against the prior version, and writes an
immutable record to issue_map_versions.

Trigger-eligible doc_types: pleading | motion | order | expert_report
Transcripts do NOT trigger issue map refresh (they trigger drift + entity extract).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import psycopg2
import psycopg2.extras
import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Document types that trigger issue map refresh (FIG. 2, trigger detection)
# ---------------------------------------------------------------------------
ISSUE_MAP_TRIGGER_TYPES = {"pleading", "motion", "order", "expert_report"}

# ---------------------------------------------------------------------------
# AI extraction prompt — structured issue map per patent spec S2-001
# ---------------------------------------------------------------------------
_ISSUE_MAP_PROMPT = """You are a legal analyst extracting the structured issue map from litigation documents.

Analyze the provided document text and extract the following structured issue map. Return ONLY valid JSON — no preamble, no markdown, no explanation.

Required JSON structure:
{
  "claims": [
    {
      "claim_name": "string — e.g. Breach of Contract",
      "count_designation": "string — e.g. Count I",
      "elements": [
        {
          "element_name": "string",
          "facts_required": "string — what facts establish this element",
          "documents_needed": ["string"],
          "key_players": ["string"]
        }
      ]
    }
  ],
  "defenses": [
    {
      "defense_name": "string",
      "elements": [
        {
          "element_name": "string",
          "facts_required": "string",
          "documents_needed": ["string"],
          "key_players": ["string"]
        }
      ]
    }
  ],
  "key_players": [
    {
      "name": "string",
      "role": "string",
      "organization": "string"
    }
  ],
  "key_entities": ["string"],
  "relevant_time_period": {
    "start_date": "YYYY-MM-DD or null",
    "end_date": "YYYY-MM-DD or null",
    "description": "string"
  },
  "key_events": [
    {
      "date": "YYYY-MM-DD or null",
      "description": "string"
    }
  ]
}

Document text:
{document_text}"""


# ---------------------------------------------------------------------------
# DB helpers — direct psycopg2 on port 5432 (never PgBouncer for writes)
# ---------------------------------------------------------------------------

def _get_direct_conn():
    """Return a psycopg2 connection to PostgreSQL on port 5432."""
    raw_url = os.environ["DATABASE_URL"]

    # Strip async driver prefix if present
    raw_url = raw_url.replace("postgresql+asyncpg://", "postgresql://")

    # DB password contains '@' — use rfind to split host from credentials
    at_pos = raw_url.rfind("@")
    credentials_part = raw_url[len("postgresql://"):at_pos]
    host_part = raw_url[at_pos + 1:]

    colon_pos = credentials_part.rfind(":")
    user = credentials_part[:colon_pos]
    password = credentials_part[colon_pos + 1:]

    # host_part may be "host:pgbouncer_port/dbname" — override port to 5432
    slash_pos = host_part.find("/")
    host_and_port = host_part[:slash_pos]
    dbname = host_part[slash_pos + 1:]

    colon_in_host = host_and_port.find(":")
    host = host_and_port[:colon_in_host] if colon_in_host != -1 else host_and_port

    conn = psycopg2.connect(
        host=host,
        port=5432,
        dbname=dbname,
        user=user,
        password=password,
    )
    conn.autocommit = True
    return conn


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------

def _extract_element_keys(issue_map: dict) -> set[str]:
    """Return a flat set of element keys for diffing."""
    keys: set[str] = set()
    for claim in issue_map.get("claims", []):
        for el in claim.get("elements", []):
            keys.add(f"claim::{claim.get('claim_name', '')}::{el.get('element_name', '')}")
    for defense in issue_map.get("defenses", []):
        for el in defense.get("elements", []):
            keys.add(f"defense::{defense.get('defense_name', '')}::{el.get('element_name', '')}")
    return keys


def _compute_diff(prior: dict, current: dict) -> tuple[dict, float]:
    """
    Compute structured diff between two issue maps.
    Returns (diff_record, magnitude_score).

    diff_record: {added:[], removed:[], modified:[], delta_conf:[]}
    magnitude: 0.0 (identical) → 1.0 (completely different)
    """
    if not prior:
        # First version — no diff
        return {"added": [], "removed": [], "modified": [], "delta_conf": []}, 0.0

    prior_keys = _extract_element_keys(prior)
    current_keys = _extract_element_keys(current)

    added = sorted(current_keys - prior_keys)
    removed = sorted(prior_keys - current_keys)

    # Key players diff
    prior_players = {p["name"] for p in prior.get("key_players", [])}
    current_players = {p["name"] for p in current.get("key_players", [])}
    added_players = sorted(current_players - prior_players)
    removed_players = sorted(prior_players - current_players)

    diff = {
        "added": added,
        "removed": removed,
        "modified": [],  # element content changes — placeholder for future deep diff
        "delta_conf": [],
        "added_players": added_players,
        "removed_players": removed_players,
    }

    # Magnitude: proportion of elements that changed relative to union
    all_keys = prior_keys | current_keys
    if not all_keys:
        magnitude = 0.0
    else:
        changed = len(added) + len(removed)
        magnitude = min(1.0, changed / len(all_keys))

    return diff, round(magnitude, 4)


# ---------------------------------------------------------------------------
# AIService call — uses ANTHROPIC_API_KEY directly (same pattern as KG job)
# ---------------------------------------------------------------------------

def _call_ai_extract(document_text: str) -> dict:
    """
    Call Anthropic API to extract structured issue map.
    Returns parsed dict. Falls back to empty skeleton on failure.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("issue_map_job: ANTHROPIC_API_KEY not set — returning empty issue map")
        return _empty_issue_map()

    prompt = _ISSUE_MAP_PROMPT.replace("{document_text}", document_text[:12000])

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90,
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"].strip()

        # Strip markdown fences if present
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]

        return json.loads(content)

    except Exception as exc:
        logger.error("issue_map_job: AI extraction failed: %s", exc)
        return _empty_issue_map()


def _empty_issue_map() -> dict:
    return {
        "claims": [],
        "defenses": [],
        "key_players": [],
        "key_entities": [],
        "relevant_time_period": {"start_date": None, "end_date": None, "description": ""},
        "key_events": [],
    }


# ---------------------------------------------------------------------------
# Main job entry point
# ---------------------------------------------------------------------------

def run_issue_map_refresh(
    tenant_id: str,
    matter_id: str,
    triggered_by: str = "document_added",
    source_document_ids: Optional[list] = None,
) -> dict:
    """
    RQ job entry point.

    1. Pull text of all trigger-eligible documents for the matter.
    2. Call AIService to extract structured issue map.
    3. Load prior version (if any).
    4. Compute diff and magnitude.
    5. Write new immutable version record.
    6. Fire drift detection hook if magnitude >= MIN_TRIGGER_MAGNITUDE.
    7. Log if magnitude thresholds crossed (>0.15 = high change).

    Returns summary dict for RQ result storage.
    """
    tenant_id = tenant_id.strip()
    source_document_ids = source_document_ids or []

    logger.info(
        "issue_map_job: starting refresh tenant=%s matter=%s triggered_by=%s",
        tenant_id,
        matter_id,
        triggered_by,
    )

    conn = _get_direct_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # ------------------------------------------------------------------
        # 1. Fetch trigger-eligible documents for this matter
        # ------------------------------------------------------------------
        cur.execute(
            """
            SELECT ed.id, ed.doc_type, ed.extracted_text, ed.file_name
            FROM ediscovery_documents ed
            JOIN ediscovery_collections c ON c.id = ed.collection_id
            WHERE ed.tenant_id = %s
              AND c.matter_id = %s::uuid
              AND c.tenant_id = %s
              AND ed.doc_type = ANY(%s)
              AND ed.extracted_text IS NOT NULL
              AND char_length(ed.extracted_text) > 50
            ORDER BY ed.created_at ASC
            """,
            (tenant_id, matter_id, tenant_id, list(ISSUE_MAP_TRIGGER_TYPES)),
        )
        docs = cur.fetchall()

        if not docs:
            logger.info(
                "issue_map_job: no trigger-eligible documents found for matter=%s — skipping",
                matter_id,
            )
            return {"status": "skipped", "reason": "no_eligible_documents", "matter_id": matter_id}

        # Concatenate document texts (truncated per document for prompt safety)
        combined_text_parts = []
        for doc in docs:
            snippet = (doc["extracted_text"] or "")[:4000]
            combined_text_parts.append(
                f"[Document: {doc['file_name']} | Type: {doc['doc_type']}]\n{snippet}"
            )
        combined_text = "\n\n---\n\n".join(combined_text_parts)

        # ------------------------------------------------------------------
        # 2. Extract issue map via AIService
        # ------------------------------------------------------------------
        current_issue_map = _call_ai_extract(combined_text)

        # ------------------------------------------------------------------
        # 3. Load prior version
        # ------------------------------------------------------------------
        cur.execute(
            """
            SELECT version_num, issue_map
            FROM issue_map_versions
            WHERE tenant_id = %s AND matter_id = %s
            ORDER BY version_num DESC
            LIMIT 1
            """,
            (tenant_id, matter_id),
        )
        prior_row = cur.fetchone()
        prior_version_number = prior_row["version_num"] if prior_row else 0
        prior_issue_map = prior_row["issue_map"] if prior_row else {}
        new_version_number = prior_version_number + 1

        # ------------------------------------------------------------------
        # 4. Compute diff and magnitude
        # ------------------------------------------------------------------
        diff_record, magnitude = _compute_diff(prior_issue_map, current_issue_map)

        # ------------------------------------------------------------------
        # 5. Write immutable version record (INSERT only — never UPDATE)
        # ------------------------------------------------------------------
        cur.execute(
            """
            INSERT INTO issue_map_versions
              (tenant_id, matter_id, version_num, trigger_type,
               source_doc_ids, issue_map, differential,
               magnitude, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                tenant_id,
                matter_id,
                new_version_number,
                triggered_by,
                json.dumps(source_document_ids),
                json.dumps(current_issue_map),
                json.dumps(diff_record),
                magnitude,
                "ai",
            ),
        )
        new_id = cur.fetchone()["id"]

        # ------------------------------------------------------------------
        # 6. Fire drift detection hook
        # Local import avoids circular dependency (drift_detection imports
        # nothing from issue_map_job, but keeping imports local is safer
        # for RQ worker process isolation).
        # ------------------------------------------------------------------
        try:
            from modules.ediscovery.drift_detection import maybe_trigger_drift_from_issue_map
            maybe_trigger_drift_from_issue_map(
                tenant_id=tenant_id,
                matter_id=matter_id,
                version_num=new_version_number,
                magnitude=magnitude,
            )
        except Exception as exc:
            # Drift hook failure must never abort the issue map job
            logger.warning(
                "issue_map_job: maybe_trigger_drift_from_issue_map failed (non-blocking): %s",
                exc,
            )

        # ------------------------------------------------------------------
        # 7. Magnitude threshold logging
        # ------------------------------------------------------------------
        if magnitude > 0.15:
            logger.warning(
                "issue_map_job: HIGH magnitude drift %.4f for matter=%s version=%d — "
                "new review pass should be queued",
                magnitude,
                matter_id,
                new_version_number,
            )
        elif magnitude > 0.05:
            logger.info(
                "issue_map_job: moderate magnitude drift %.4f for matter=%s version=%d",
                magnitude,
                matter_id,
                new_version_number,
            )

        logger.info(
            "issue_map_job: wrote version %d (id=%s) magnitude=%.4f for matter=%s",
            new_version_number,
            new_id,
            magnitude,
            matter_id,
        )

        return {
            "status": "ok",
            "matter_id": matter_id,
            "version_id": str(new_id),
            "version_number": new_version_number,
            "diff_magnitude": magnitude,
            "documents_processed": len(docs),
        }

    except Exception as exc:
        logger.error("issue_map_job: unhandled error for matter=%s: %s", matter_id, exc, exc_info=True)
        raise
    finally:
        conn.close()
