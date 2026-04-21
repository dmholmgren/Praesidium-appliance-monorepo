"""
jobs/drift_detection_job.py

RQ job: run_drift_detection
Queue: ediscovery
Timeout: 300 seconds

Analyzes the differential record from an issue map version and classifies
material changes as typed drift events. Writes immutable drift_events records.

Patent claim: S2-001, FIG. 2 — Drift Detection Subsystem [130]

Drift dimensions: theory | factual | custodian | damages
Drift types:      addition | removal | modification | escalation | de-escalation | reversal

Magnitude thresholds (from FIG. 2):
  > 0.6 → wiam_surfaced = True (auto-surface in WIAM)
  > 0.3 → log alert for case dashboard

Triggers:
  1. After issue map version write (magnitude > 0.05) — called from issue_map_job
  2. Transcript ingestion — transcripts trigger drift detection, not issue map refresh
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

# Magnitude thresholds (patent spec FIG. 2)
WIAM_THRESHOLD = 0.6      # auto-surface in WIAM
ALERT_THRESHOLD = 0.3     # surface alert in case dashboard
MIN_TRIGGER_MAGNITUDE = 0.05  # below this → skip drift analysis

VALID_DIMENSIONS = {"theory_drift", "factual_drift", "custodian_drift", "damages_drift"}
VALID_TYPES = {"addition", "removal", "modification", "escalation", "de-escalation", "reversal"}

# ---------------------------------------------------------------------------
# AI classification prompt
# ---------------------------------------------------------------------------

_DRIFT_PROMPT = """You are a litigation analyst detecting case theory drift from an issue map differential.

You are given:
1. A structured diff between two issue map versions (added elements, removed elements, added/removed players)
2. The magnitude score (0.0–1.0) already computed

Classify ALL material changes as drift events. Return ONLY a valid JSON array — no preamble, no markdown.

Each drift event object must have exactly these fields:
{{
  "drift_dimension": "theory" | "factual" | "custodian" | "damages",
  "drift_type": "addition" | "removal" | "modification" | "escalation" | "de-escalation" | "reversal",
  "description": "one sentence describing what changed and why it matters",
  "magnitude": 0.0 to 1.0
}}

Classification guidance:
- theory: changes to legal claims, defenses, or their elements
- factual: changes to key events, dates, or factual narrative
- custodian: changes to key players or organizational affiliations
- damages: changes to damages elements or damages-related claims

- addition: new element or player appeared
- removal: element or player was removed
- modification: content of existing element changed
- escalation: claim or element strengthened / expanded
- de-escalation: claim or element weakened / narrowed
- reversal: element completely changed direction or theory

If the diff is empty or magnitude is below 0.05, return an empty array: []

Differential record:
{diff_json}

Overall magnitude: {magnitude}"""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_direct_conn():
    raw_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    at_pos = raw_url.rfind("@")
    creds = raw_url[len("postgresql://"):at_pos]
    host_part = raw_url[at_pos + 1:]
    colon = creds.rfind(":")
    user = creds[:colon]
    password = creds[colon + 1:]
    slash = host_part.find("/")
    host_and_port = host_part[:slash]
    dbname = host_part[slash + 1:]
    colon_h = host_and_port.find(":")
    host = host_and_port[:colon_h] if colon_h != -1 else host_and_port
    conn = psycopg2.connect(host=host, port=5432, dbname=dbname, user=user, password=password)
    conn.autocommit = True
    return conn


# ---------------------------------------------------------------------------
# AI classification
# ---------------------------------------------------------------------------

def _call_ai_classify(diff: dict, magnitude: float) -> list[dict]:
    """
    Call Anthropic API to classify drift events from the differential record.
    Returns list of drift event dicts. Falls back to empty list on failure.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("drift_detection_job: ANTHROPIC_API_KEY not set — returning empty drift events")
        return []

    # Skip if diff is empty
    has_changes = (
        diff.get("added") or diff.get("removed") or
        diff.get("added_players") or diff.get("removed_players") or
        diff.get("modified")
    )
    if not has_changes or magnitude < MIN_TRIGGER_MAGNITUDE:
        logger.info("drift_detection_job: diff empty or magnitude below threshold — skipping AI call")
        return []

    prompt = _DRIFT_PROMPT.format(
        diff_json=json.dumps(diff, indent=2)[:4000],
        magnitude=magnitude,
    )

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
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"].strip()

        # Strip markdown fences
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]

        events = json.loads(content)
        if not isinstance(events, list):
            return []

        # Validate and filter
        validated = []
        for ev in events:
            dim = ev.get("drift_dimension", "")
            typ = ev.get("drift_type", "")
            if dim in VALID_DIMENSIONS and typ in VALID_TYPES:
                validated.append({
                    "drift_dimension": dim,
                    "drift_type": typ,
                    "description": str(ev.get("description", ""))[:1000],
                    "magnitude": min(1.0, max(0.0, float(ev.get("magnitude", magnitude)))),
                })
        return validated

    except Exception as exc:
        logger.error("drift_detection_job: AI classification failed: %s", exc)
        return []


def _rule_based_classify(diff: dict, magnitude: float) -> list[dict]:
    """
    Fallback: produce basic drift events from the diff without AI.
    Used when ANTHROPIC_API_KEY is absent or AI call fails.
    """
    if magnitude < MIN_TRIGGER_MAGNITUDE:
        return []

    events = []

    for item in diff.get("added", []):
        dim = "custodian_drift" if "player" in item.lower() else "theory_drift"
        events.append({
            "drift_dimension": dim,
            "drift_type": "addition",
            "description": f"New element added: {item}",
            "magnitude": magnitude,
        })

    for item in diff.get("removed", []):
        dim = "custodian_drift" if "player" in item.lower() else "theory_drift"
        events.append({
            "drift_dimension": dim,
            "drift_type": "removal",
            "description": f"Element removed: {item}",
            "magnitude": magnitude,
        })

    for item in diff.get("added_players", []):
        events.append({
            "drift_dimension": "custodian_drift",
            "drift_type": "addition",
            "description": f"New key player identified: {item}",
            "magnitude": magnitude * 0.5,
        })

    for item in diff.get("removed_players", []):
        events.append({
            "drift_dimension": "custodian_drift",
            "drift_type": "removal",
            "description": f"Key player removed: {item}",
            "magnitude": magnitude * 0.5,
        })

    return events


# ---------------------------------------------------------------------------
# Main job entry point
# ---------------------------------------------------------------------------

def run_drift_detection(
    tenant_id: str,
    matter_id: str,
    issue_map_version_num: int,
    source_document_id: Optional[str] = None,
    triggered_by: str = "system",
) -> dict:
    """
    RQ job entry point.

    1. Load the issue map version and its differential record.
    2. Skip if magnitude below threshold.
    3. Call AIService to classify drift events (fall back to rule-based).
    4. Write immutable drift_events records.
    5. Set wiam_surfaced=True for events with magnitude > 0.6.
    6. Log alert for events with magnitude > 0.3.

    Returns summary dict.
    """
    tenant_id = tenant_id.strip()

    logger.info(
        "drift_detection_job: starting tenant=%s matter=%s version=%d",
        tenant_id, matter_id, issue_map_version_num,
    )

    conn = _get_direct_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # ------------------------------------------------------------------
        # 1. Load the issue map version
        # ------------------------------------------------------------------
        cur.execute(
            """
            SELECT version_num, differential, magnitude
            FROM issue_map_versions
            WHERE tenant_id = %s AND matter_id = %s AND version_num = %s
            """,
            (tenant_id, matter_id, issue_map_version_num),
        )
        version_row = cur.fetchone()

        if not version_row:
            logger.info(
                "drift_detection_job: version %d not found for matter=%s — skipping",
                issue_map_version_num, matter_id,
            )
            return {"status": "skipped", "reason": "version_not_found", "matter_id": matter_id}

        diff = version_row["differential"] or {}
        magnitude = float(version_row["magnitude"] or 0.0)

        # ------------------------------------------------------------------
        # 2. Skip if magnitude below minimum threshold
        # ------------------------------------------------------------------
        if magnitude < MIN_TRIGGER_MAGNITUDE:
            logger.info(
                "drift_detection_job: magnitude %.4f below threshold %.2f for matter=%s — skipping",
                magnitude, MIN_TRIGGER_MAGNITUDE, matter_id,
            )
            return {
                "status": "skipped",
                "reason": "magnitude_below_threshold",
                "magnitude": magnitude,
                "matter_id": matter_id,
            }

        prior_version_num = issue_map_version_num - 1

        # ------------------------------------------------------------------
        # 3. Classify drift events
        # ------------------------------------------------------------------
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            drift_events = _call_ai_classify(diff, magnitude)
            detection_source = "ai"
        else:
            drift_events = _rule_based_classify(diff, magnitude)
            detection_source = "system"

        if not drift_events:
            logger.info(
                "drift_detection_job: no drift events classified for matter=%s version=%d",
                matter_id, issue_map_version_num,
            )
            return {
                "status": "ok",
                "matter_id": matter_id,
                "events_written": 0,
                "magnitude": magnitude,
            }

        # ------------------------------------------------------------------
        # 4. Write immutable drift_events records
        # ------------------------------------------------------------------
        events_written = 0
        wiam_count = 0

        for ev in drift_events:
            ev_magnitude = ev["magnitude"]
            wiam_surfaced = ev_magnitude > WIAM_THRESHOLD

            cur.execute(
                """
                INSERT INTO drift_events
                  (tenant_id, matter_id, version_before, version_after,
                   dimension, drift_type, description, source_doc_id,
                   magnitude, detection_source, wiam_surfaced)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    tenant_id,
                    matter_id,
                    prior_version_num,
                    issue_map_version_num,
                    ev["drift_dimension"],
                    ev["drift_type"],
                    ev["description"],
                    source_document_id,   # UUID or None
                    ev_magnitude,
                    detection_source,
                    wiam_surfaced,
                ),
            )
            events_written += 1
            if wiam_surfaced:
                wiam_count += 1

            # ------------------------------------------------------------------
            # 5. Alert logging by threshold
            # ------------------------------------------------------------------
            if ev_magnitude > WIAM_THRESHOLD:
                logger.warning(
                    "drift_detection_job: HIGH magnitude drift (%.4f) — %s/%s — "
                    "wiam_surfaced=True — matter=%s",
                    ev_magnitude, ev["drift_dimension"], ev["drift_type"], matter_id,
                )
            elif ev_magnitude > ALERT_THRESHOLD:
                logger.info(
                    "drift_detection_job: moderate drift (%.4f) — %s/%s — matter=%s",
                    ev_magnitude, ev["drift_dimension"], ev["drift_type"], matter_id,
                )

        logger.info(
            "drift_detection_job: wrote %d drift events (%d WIAM-surfaced) for matter=%s version=%d",
            events_written, wiam_count, matter_id, issue_map_version_num,
        )

        return {
            "status": "ok",
            "matter_id": matter_id,
            "version_num": issue_map_version_num,
            "events_written": events_written,
            "wiam_surfaced_count": wiam_count,
            "magnitude": magnitude,
            "detection_source": detection_source,
        }

    except Exception as exc:
        logger.error(
            "drift_detection_job: unhandled error for matter=%s: %s", matter_id, exc, exc_info=True
        )
        raise
    finally:
        conn.close()
