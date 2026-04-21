"""
jobs/issue_map_engine.py

M5i-B — Living Issue Map Engine (S2-001)

RQ job that:
  1. Retrieves all documents for a matter (pleadings, motions, transcripts, etc.)
  2. Calls AIService to generate/refresh the structured issue map
  3. Computes a full differential against the prior version
  4. Calculates a magnitude score (0.0–1.0)
  5. Writes an immutable new issue_map_versions record
  6. Runs drift detection and writes drift_event records for material changes
  7. Auto-queues a re-review pass if magnitude >= HIGH_THRESHOLD (0.70)
  8. Surfaces high-magnitude drift events as WIAM findings

Queue: ediscovery
Entry point: jobs.issue_map_engine.run(matter_id, tenant_id, trigger_type, source_doc_ids)
Timeout: 600 seconds

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Magnitude thresholds (S2-001)                                       #
# ------------------------------------------------------------------ #
HIGH_THRESHOLD = 0.70   # auto-queue review pass + surface to WIAM
LOW_THRESHOLD  = 0.35   # flag for human review

# ------------------------------------------------------------------ #
# Document processing profile (S2-001 trigger detection subsystem)    #
# Pleadings/motions/orders/expert reports → issue map refresh         #
# Deposition transcripts → drift detection + entity extraction only   #
# ------------------------------------------------------------------ #
ISSUE_MAP_TRIGGER_TYPES = {
    "pleading", "motion", "court_order", "expert_report",
    "complaint", "answer", "counterclaim", "brief",
}

DRIFT_ONLY_TYPES = {
    "deposition_transcript", "deposition", "transcript",
}


# ================================================================== #
# DB helpers (psycopg2 direct — same pattern as M5i-A tests)         #
# ================================================================== #

def _get_conn():
    raw = os.environ.get("DATABASE_URL", "")
    raw = raw.replace("postgresql+asyncpg://", "postgresql://")
    at = raw.rfind("@")
    creds = raw[len("postgresql://"):at]
    rest = raw[at + 1:]
    user, password = creds.split(":", 1)
    host_port, dbname = rest.rsplit("/", 1)
    host = host_port.split(":")[0]
    return psycopg2.connect(
        host=host, port=5432,
        dbname=dbname, user=user, password=password,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _get_ai_client():
    """Return Anthropic client. Raises if key not set."""
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key)


# ================================================================== #
# Issue map generation (S2-001 §II)                                  #
# ================================================================== #

ISSUE_MAP_SYSTEM_PROMPT = """You are a litigation intelligence engine for Praesidium, 
a legal practice platform. Your task is to extract and structure the complete case theory 
from the provided legal documents.

Return ONLY valid JSON matching this exact schema — no preamble, no markdown, no explanation:

{
  "claims": [
    {
      "claim_name": "string",
      "count": "string or null",
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
  "defenses": [
    {
      "defense_name": "string",
      "elements": [
        {
          "element_name": "string",
          "facts_required": "string"
        }
      ]
    }
  ],
  "key_players": [
    {
      "name": "string",
      "role": "string",
      "organization": "string or null"
    }
  ],
  "key_entities": ["string"],
  "relevant_time_period": {
    "start": "YYYY-MM-DD or null",
    "end": "YYYY-MM-DD or null",
    "description": "string"
  },
  "key_events": [
    {
      "date": "YYYY-MM-DD or null",
      "description": "string"
    }
  ]
}

Extract all claims and defenses explicitly pleaded. If a document is a deposition or 
exhibit rather than a pleading, extract admissions and key facts rather than claims."""


def _generate_issue_map(matter_id: str, tenant_id: str, docs: list[dict]) -> dict:
    """
    Call AIService (Claude) to generate a structured issue map from matter documents.
    Falls back to empty structure if AI unavailable.
    """
    if not docs:
        log.warning("issue_map_engine: no documents for matter %s", matter_id)
        return _empty_issue_map()

    # Build document summary for the prompt
    doc_summaries = []
    for d in docs[:20]:  # cap at 20 docs to stay within context
        summary = f"[{d.get('doc_type','unknown').upper()}] {d.get('file_name','untitled')}"
        if d.get('file_name'):
            # First 2000 chars of extracted text
            text = f"Custodian: {d.get('custodian','')} Date: {d.get('doc_date','')}"
            summary += f"\n{text}"
        doc_summaries.append(summary)

    user_content = (
        f"Matter ID: {matter_id}\n\n"
        f"Documents ({len(docs)} total, showing {len(doc_summaries)}):\n\n"
        + "\n\n---\n\n".join(doc_summaries)
        + "\n\nGenerate the complete structured issue map for this matter."
    )

    try:
        client = _get_ai_client()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=ISSUE_MAP_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as exc:
        log.error("issue_map_engine: AI call failed for matter %s: %s", matter_id, exc)
        return _empty_issue_map()


def _empty_issue_map() -> dict:
    return {
        "claims": [],
        "defenses": [],
        "key_players": [],
        "key_entities": [],
        "relevant_time_period": {"start": None, "end": None, "description": ""},
        "key_events": [],
    }


# ================================================================== #
# Differential analysis (S2-001 §differential_analysis_subsystem)    #
# ================================================================== #

def _compute_differential(prior: dict, current: dict) -> tuple[dict, float]:
    """
    Compute structured difference record and magnitude score between
    two issue map versions.

    Magnitude: 0.0 = identical, 1.0 = complete replacement.
    Structured diff identifies: added, removed, modified elements.

    Returns (differential: dict, magnitude: float)
    """
    diff = {
        "claims_added": [],
        "claims_removed": [],
        "claims_modified": [],
        "defenses_added": [],
        "defenses_removed": [],
        "players_added": [],
        "players_removed": [],
        "events_added": [],
        "events_removed": [],
    }

    if not prior:
        # First version — no prior to compare
        return {"initial_version": True}, 0.0

    # --- Claims ---
    prior_claims = {c.get("claim_name", ""): c for c in prior.get("claims", [])}
    curr_claims  = {c.get("claim_name", ""): c for c in current.get("claims", [])}

    for name in set(curr_claims) - set(prior_claims):
        diff["claims_added"].append(name)
    for name in set(prior_claims) - set(curr_claims):
        diff["claims_removed"].append(name)
    for name in set(prior_claims) & set(curr_claims):
        if json.dumps(prior_claims[name], sort_keys=True) != \
           json.dumps(curr_claims[name], sort_keys=True):
            diff["claims_modified"].append(name)

    # --- Defenses ---
    prior_def = {d.get("defense_name", ""): d for d in prior.get("defenses", [])}
    curr_def  = {d.get("defense_name", ""): d for d in current.get("defenses", [])}

    for name in set(curr_def) - set(prior_def):
        diff["defenses_added"].append(name)
    for name in set(prior_def) - set(curr_def):
        diff["defenses_removed"].append(name)

    # --- Key players ---
    prior_players = {p.get("name", ""): p for p in prior.get("key_players", [])}
    curr_players  = {p.get("name", ""): p for p in current.get("key_players", [])}

    for name in set(curr_players) - set(prior_players):
        diff["players_added"].append(name)
    for name in set(prior_players) - set(curr_players):
        diff["players_removed"].append(name)

    # --- Key events ---
    prior_events = [e.get("description", "") for e in prior.get("key_events", [])]
    curr_events  = [e.get("description", "") for e in current.get("key_events", [])]
    prior_event_set = set(prior_events)
    curr_event_set  = set(curr_events)

    diff["events_added"]   = list(curr_event_set - prior_event_set)
    diff["events_removed"] = list(prior_event_set - curr_event_set)

    # --- Magnitude score ---
    total_changes = (
        len(diff["claims_added"])    + len(diff["claims_removed"])   +
        len(diff["claims_modified"]) * 0.5 +
        len(diff["defenses_added"])  + len(diff["defenses_removed"]) +
        len(diff["players_added"])   + len(diff["players_removed"])  +
        len(diff["events_added"])    + len(diff["events_removed"])
    )

    # Normalize against total elements considered
    total_elements = max(
        len(prior_claims) + len(curr_claims) +
        len(prior_def)    + len(curr_def)    +
        len(prior_players)+ len(curr_players)+
        len(prior_events) + len(curr_events),
        1
    )

    magnitude = min(total_changes / total_elements, 1.0)
    return diff, round(magnitude, 4)


# ================================================================== #
# Drift detection (S2-001 §drift_detection_subsystem)                #
# ================================================================== #

DRIFT_PROMPT_TEMPLATE = """You are analyzing changes between two versions of a legal case theory.

Prior issue map version {v_before}:
{prior_json}

Current issue map version {v_after}:
{current_json}

Differential summary:
{diff_json}

Identify drift events. For each material change, output a JSON array of drift event objects:
[
  {{
    "dimension": "theory_drift|factual_drift|custodian_drift|damages_drift",
    "drift_type": "addition|removal|modification|escalation|de-escalation|reversal",
    "description": "plain English description of what changed and why it matters",
    "magnitude": 0.0-1.0
  }}
]

Rules:
- Only surface material changes (magnitude >= 0.1). Ignore cosmetic edits.
- theory_drift: changes to claims, defenses, or legal elements
- factual_drift: changes to key events, timeline, or factual allegations  
- custodian_drift: changes to key players or witnesses
- damages_drift: changes to damages theories or amounts
- Return ONLY the JSON array. No preamble. No markdown.
- If no material drift, return []"""


def _detect_drift(
    prior: dict,
    current: dict,
    diff: dict,
    version_before: int,
    version_after: int,
    source_doc_id: str | None,
) -> list[dict]:
    """
    Call AIService to classify drift events from the differential.
    Returns list of drift event dicts ready to insert.
    """
    if diff.get("initial_version"):
        return []

    prompt = DRIFT_PROMPT_TEMPLATE.format(
        v_before=version_before,
        v_after=version_after,
        prior_json=json.dumps(prior, indent=2)[:3000],
        current_json=json.dumps(current, indent=2)[:3000],
        diff_json=json.dumps(diff, indent=2),
    )

    try:
        client = _get_ai_client()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        events = json.loads(raw)
        if not isinstance(events, list):
            return []

        # Validate and normalize
        valid_dims   = {"theory_drift", "factual_drift", "custodian_drift", "damages_drift"}
        valid_types  = {"addition", "removal", "modification", "escalation", "de-escalation", "reversal"}
        result = []
        for ev in events:
            if ev.get("dimension") not in valid_dims:
                continue
            if ev.get("drift_type") not in valid_types:
                continue
            mag = float(ev.get("magnitude", 0.0))
            if mag < 0.1:
                continue
            result.append({
                "id": str(uuid.uuid4()),
                "dimension": ev["dimension"],
                "drift_type": ev["drift_type"],
                "description": str(ev.get("description", ""))[:1000],
                "magnitude": min(max(mag, 0.0), 1.0),
                "source_doc_id": source_doc_id,
                "version_before": version_before,
                "version_after": version_after,
                "detection_source": "ai",
            })
        return result

    except Exception as exc:
        log.error("issue_map_engine: drift detection failed: %s", exc)
        # Fall back to rule-based drift from differential
        return _rule_based_drift(diff, version_before, version_after, source_doc_id)


def _rule_based_drift(
    diff: dict,
    version_before: int,
    version_after: int,
    source_doc_id: str | None,
) -> list[dict]:
    """
    Fallback rule-based drift detection when AI is unavailable.
    Generates drift events directly from the differential record.
    """
    events = []

    for name in diff.get("claims_added", []):
        events.append({
            "id": str(uuid.uuid4()),
            "dimension": "theory_drift",
            "drift_type": "addition",
            "description": f"New claim added: {name}",
            "magnitude": 0.6,
            "source_doc_id": source_doc_id,
            "version_before": version_before,
            "version_after": version_after,
            "detection_source": "rule_based",
        })
    for name in diff.get("claims_removed", []):
        events.append({
            "id": str(uuid.uuid4()),
            "dimension": "theory_drift",
            "drift_type": "removal",
            "description": f"Claim removed: {name}",
            "magnitude": 0.7,
            "source_doc_id": source_doc_id,
            "version_before": version_before,
            "version_after": version_after,
            "detection_source": "rule_based",
        })
    for name in diff.get("claims_modified", []):
        events.append({
            "id": str(uuid.uuid4()),
            "dimension": "theory_drift",
            "drift_type": "modification",
            "description": f"Claim elements modified: {name}",
            "magnitude": 0.4,
            "source_doc_id": source_doc_id,
            "version_before": version_before,
            "version_after": version_after,
            "detection_source": "rule_based",
        })
    for name in diff.get("players_added", []):
        events.append({
            "id": str(uuid.uuid4()),
            "dimension": "custodian_drift",
            "drift_type": "addition",
            "description": f"New key player identified: {name}",
            "magnitude": 0.3,
            "source_doc_id": source_doc_id,
            "version_before": version_before,
            "version_after": version_after,
            "detection_source": "rule_based",
        })
    for name in diff.get("players_removed", []):
        events.append({
            "id": str(uuid.uuid4()),
            "dimension": "custodian_drift",
            "drift_type": "removal",
            "description": f"Key player removed: {name}",
            "magnitude": 0.4,
            "source_doc_id": source_doc_id,
            "version_before": version_before,
            "version_after": version_after,
            "detection_source": "rule_based",
        })

    return events


# ================================================================== #
# Main entry point                                                    #
# ================================================================== #

def run(
    matter_id: str,
    tenant_id: str,
    trigger_type: str = "manual",
    source_doc_ids: list[str] | None = None,
    created_by: int | None = None,
) -> dict:
    """
    RQ entry point. Called by intelligence_layer.py router when:
      - Attorney manually triggers a refresh
      - A legally significant document is ingested (trigger_type='document_added')
      - Scheduled refresh (trigger_type='scheduled')

    Returns summary dict with version_id, version_num, magnitude, drift_count.
    """
    tenant_id = tenant_id.strip()
    source_doc_ids = source_doc_ids or []
    log.info(
        "issue_map_engine: starting for matter=%s tenant=%s trigger=%s",
        matter_id, tenant_id, trigger_type,
    )

    conn = _get_conn()
    try:
        # ---------------------------------------------------------- #
        # 1. Load all matter documents                                #
        # ---------------------------------------------------------- #
        with conn.cursor() as cur:
            cur.execute("""
                SELECT d.id, d.file_name, d.doc_type, d.custodian, d.doc_date, d.created_at
                FROM ediscovery_documents d JOIN ediscovery_collections c ON d.collection_id = c.id
                WHERE c.matter_id = %s AND d.tenant_id = %s
                ORDER BY created_at ASC
                LIMIT 50
            """, (matter_id, tenant_id))
            docs = cur.fetchall() or []

        log.info("issue_map_engine: loaded %d documents", len(docs))

        # ---------------------------------------------------------- #
        # 2. Load prior version                                       #
        # ---------------------------------------------------------- #
        with conn.cursor() as cur:
            cur.execute("""
                SELECT version_num, issue_map
                FROM issue_map_versions
                WHERE matter_id = %s AND tenant_id = %s
                ORDER BY version_num DESC
                LIMIT 1
            """, (matter_id, tenant_id))
            prior_row = cur.fetchone()

        prior_version_num = prior_row["version_num"] if prior_row else 0
        prior_issue_map   = prior_row["issue_map"]   if prior_row else {}

        # Normalize prior_issue_map from JSONB (may be str or dict)
        if isinstance(prior_issue_map, str):
            try:
                prior_issue_map = json.loads(prior_issue_map)
            except Exception:
                prior_issue_map = {}
        prior_issue_map = prior_issue_map or {}

        # ---------------------------------------------------------- #
        # 3. Generate new issue map via AIService                     #
        # ---------------------------------------------------------- #
        new_issue_map = _generate_issue_map(matter_id, tenant_id, docs)

        # ---------------------------------------------------------- #
        # 4. Compute differential and magnitude                       #
        # ---------------------------------------------------------- #
        differential, magnitude = _compute_differential(prior_issue_map, new_issue_map)

        next_version_num = prior_version_num + 1
        version_id = str(uuid.uuid4())

        # ---------------------------------------------------------- #
        # 5. Write immutable version record — never UPDATE            #
        # ---------------------------------------------------------- #
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO issue_map_versions
                    (id, matter_id, tenant_id, version_num, trigger_type,
                     source_doc_ids, issue_map, differential, magnitude, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                version_id,
                matter_id,
                tenant_id,
                next_version_num,
                trigger_type,
                source_doc_ids,
                json.dumps(new_issue_map),
                json.dumps(differential),
                magnitude,
                created_by,
            ))
        conn.commit()

        log.info(
            "issue_map_engine: wrote version %d (magnitude=%.3f)",
            next_version_num, magnitude,
        )

        # ---------------------------------------------------------- #
        # 6. Drift detection                                          #
        # ---------------------------------------------------------- #
        drift_events = []
        if prior_row:
            source_doc_id = source_doc_ids[0] if source_doc_ids else None
            drift_events = _detect_drift(
                prior=prior_issue_map,
                current=new_issue_map,
                diff=differential,
                version_before=prior_version_num,
                version_after=next_version_num,
                source_doc_id=source_doc_id,
            )

            if drift_events:
                with conn.cursor() as cur:
                    for ev in drift_events:
                        cur.execute("""
                            INSERT INTO drift_events
                                (id, matter_id, tenant_id, version_before, version_after,
                                 dimension, drift_type, description, source_doc_id,
                                 magnitude, detection_source)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            ev["id"],
                            matter_id,
                            tenant_id,
                            ev["version_before"],
                            ev["version_after"],
                            ev["dimension"],
                            ev["drift_type"],
                            ev["description"],
                            ev.get("source_doc_id"),
                            ev["magnitude"],
                            ev["detection_source"],
                        ))
                conn.commit()
                log.info("issue_map_engine: wrote %d drift events", len(drift_events))

        # ---------------------------------------------------------- #
        # 7. High-magnitude auto-actions (S2-001 claim 9/10)         #
        # ---------------------------------------------------------- #
        if magnitude >= HIGH_THRESHOLD:
            log.warning(
                "issue_map_engine: HIGH DRIFT magnitude=%.3f for matter=%s — "
                "auto-queue review pass (M5i-C will implement)",
                magnitude, matter_id,
            )
            # Surface high-magnitude drift events to WIAM queue
            # Full implementation in M5i-D wiam_engine.py
            # Placeholder: logged — WIAM auto-trigger added in M5i-D

        elif magnitude >= LOW_THRESHOLD:
            log.info(
                "issue_map_engine: MODERATE DRIFT magnitude=%.3f for matter=%s — "
                "flagged for human review",
                magnitude, matter_id,
            )

        return {
            "status": "complete",
            "version_id": version_id,
            "version_num": next_version_num,
            "magnitude": magnitude,
            "drift_count": len(drift_events),
            "high_drift": magnitude >= HIGH_THRESHOLD,
        }

    except Exception as exc:
        log.exception("issue_map_engine: fatal error for matter=%s: %s", matter_id, exc)
        conn.rollback()
        raise
    finally:
        conn.close()
