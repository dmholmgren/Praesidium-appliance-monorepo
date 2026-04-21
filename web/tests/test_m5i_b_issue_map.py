"""
M5i-B — Living Issue Map Engine Tests
Tests the differential engine, drift detection, magnitude scoring,
and job importability. DB round-trips via psycopg2.
Expected: 16 passed
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import MagicMock, patch

import psycopg2
import psycopg2.extras
import pytest


# ================================================================== #
# DB helper                                                           #
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


# ================================================================== #
# Import tests                                                        #
# ================================================================== #

def test_job_importable():
    from jobs import issue_map_engine
    assert hasattr(issue_map_engine, "run")


def test_compute_differential_importable():
    from jobs.issue_map_engine import _compute_differential
    assert callable(_compute_differential)


def test_detect_drift_importable():
    from jobs.issue_map_engine import _detect_drift
    assert callable(_detect_drift)


def test_thresholds_correct():
    from jobs.issue_map_engine import HIGH_THRESHOLD, LOW_THRESHOLD
    assert HIGH_THRESHOLD == 0.70
    assert LOW_THRESHOLD  == 0.35


# ================================================================== #
# Differential engine unit tests                                      #
# ================================================================== #

def test_differential_identical_maps():
    """Identical maps → magnitude 0.0."""
    from jobs.issue_map_engine import _compute_differential
    im = {
        "claims": [{"claim_name": "Breach of Contract", "elements": []}],
        "defenses": [],
        "key_players": [{"name": "Alice", "role": "Plaintiff"}],
        "key_events": [{"date": "2024-01-01", "description": "Contract signed"}],
    }
    diff, mag = _compute_differential(im, im)
    assert mag == 0.0


def test_differential_empty_prior():
    """No prior version → initial_version flag, magnitude 0.0."""
    from jobs.issue_map_engine import _compute_differential
    diff, mag = _compute_differential({}, {"claims": [], "defenses": []})
    assert diff.get("initial_version") is True
    assert mag == 0.0


def test_differential_claim_added():
    """Adding a claim raises magnitude above 0."""
    from jobs.issue_map_engine import _compute_differential
    prior = {"claims": [], "defenses": [], "key_players": [], "key_events": []}
    current = {
        "claims": [{"claim_name": "Fraud", "elements": []}],
        "defenses": [],
        "key_players": [],
        "key_events": [],
    }
    diff, mag = _compute_differential(prior, current)
    assert "Fraud" in diff["claims_added"]
    assert mag > 0.0


def test_differential_claim_removed():
    """Removing a claim is detected and raises magnitude."""
    from jobs.issue_map_engine import _compute_differential
    prior = {
        "claims": [
            {"claim_name": "Negligence", "elements": []},
            {"claim_name": "Fraud", "elements": []},
        ],
        "defenses": [], "key_players": [], "key_events": [],
    }
    current = {
        "claims": [{"claim_name": "Negligence", "elements": []}],
        "defenses": [], "key_players": [], "key_events": [],
    }
    diff, mag = _compute_differential(prior, current)
    assert "Fraud" in diff["claims_removed"]
    assert mag > 0.0


def test_differential_magnitude_capped_at_1():
    """Magnitude never exceeds 1.0."""
    from jobs.issue_map_engine import _compute_differential
    prior = {
        "claims": [{"claim_name": f"Claim {i}", "elements": []} for i in range(10)],
        "defenses": [{"defense_name": f"Defense {i}", "elements": []} for i in range(5)],
        "key_players": [{"name": f"Person {i}", "role": "Party"} for i in range(8)],
        "key_events": [{"date": None, "description": f"Event {i}"} for i in range(5)],
    }
    current = {
        "claims": [{"claim_name": f"NewClaim {i}", "elements": []} for i in range(10)],
        "defenses": [{"defense_name": f"NewDefense {i}", "elements": []} for i in range(5)],
        "key_players": [{"name": f"NewPerson {i}", "role": "Party"} for i in range(8)],
        "key_events": [{"date": None, "description": f"NewEvent {i}"} for i in range(5)],
    }
    diff, mag = _compute_differential(prior, current)
    assert mag <= 1.0


def test_differential_player_changes_detected():
    """Key player additions and removals are tracked."""
    from jobs.issue_map_engine import _compute_differential
    prior = {
        "claims": [], "defenses": [],
        "key_players": [
            {"name": "Alice", "role": "Plaintiff"},
            {"name": "Bob", "role": "Defendant"},
        ],
        "key_events": [],
    }
    current = {
        "claims": [], "defenses": [],
        "key_players": [
            {"name": "Alice", "role": "Plaintiff"},
            {"name": "Carol", "role": "Expert"},  # Bob removed, Carol added
        ],
        "key_events": [],
    }
    diff, mag = _compute_differential(prior, current)
    assert "Carol" in diff["players_added"]
    assert "Bob"   in diff["players_removed"]
    assert mag > 0.0


# ================================================================== #
# Rule-based drift unit tests                                         #
# ================================================================== #

def test_rule_based_drift_claim_added():
    from jobs.issue_map_engine import _rule_based_drift
    diff = {
        "claims_added": ["Fraud"],
        "claims_removed": [],
        "claims_modified": [],
        "defenses_added": [],
        "defenses_removed": [],
        "players_added": [],
        "players_removed": [],
        "events_added": [],
        "events_removed": [],
    }
    events = _rule_based_drift(diff, 1, 2, None)
    assert len(events) == 1
    assert events[0]["dimension"] == "theory_drift"
    assert events[0]["drift_type"] == "addition"
    assert "Fraud" in events[0]["description"]


def test_rule_based_drift_empty_diff():
    from jobs.issue_map_engine import _rule_based_drift
    diff = {k: [] for k in [
        "claims_added", "claims_removed", "claims_modified",
        "defenses_added", "defenses_removed",
        "players_added", "players_removed",
        "events_added", "events_removed",
    ]}
    events = _rule_based_drift(diff, 1, 2, None)
    assert events == []


# ================================================================== #
# DB round-trip tests                                                 #
# ================================================================== #

def test_issue_map_version_write_and_read():
    """Simulate a job writing a version record and reading it back."""
    matter_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    tenant_id = "hjmm-prod           "  # CHAR(36) with trailing spaces

    issue_map = {
        "claims": [{"claim_name": "Breach of Contract", "elements": []}],
        "defenses": [],
        "key_players": [{"name": "Enron Corp", "role": "Defendant"}],
        "key_entities": ["Enron"],
        "relevant_time_period": {"start": "2001-01-01", "end": "2002-12-31", "description": ""},
        "key_events": [],
    }

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO issue_map_versions
                    (id, matter_id, tenant_id, version_num, trigger_type,
                     source_doc_ids, issue_map, differential, magnitude, created_by)
                VALUES (%s, %s, %s, 1, 'manual', %s, %s, %s, %s, NULL)
            """, (
                version_id, matter_id, tenant_id,
                [],
                json.dumps(issue_map),
                json.dumps({"initial_version": True}),
                0.0,
            ))
            conn.commit()

            cur.execute(
                "SELECT version_num, magnitude, issue_map FROM issue_map_versions WHERE id = %s",
                (version_id,)
            )
            row = cur.fetchone()

        assert row["version_num"] == 1
        assert row["magnitude"] == 0.0
        stored_map = row["issue_map"]
        if isinstance(stored_map, str):
            stored_map = json.loads(stored_map)
        assert stored_map["claims"][0]["claim_name"] == "Breach of Contract"
    finally:
        conn.close()


def test_drift_event_write_and_read():
    """Simulate the job writing a drift event and reading it back."""
    matter_id = str(uuid.uuid4())
    event_id  = str(uuid.uuid4())
    tenant_id = "hjmm-prod           "

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO drift_events
                    (id, matter_id, tenant_id, version_before, version_after,
                     dimension, drift_type, description, magnitude, detection_source)
                VALUES (%s, %s, %s, 1, 2, 'theory_drift', 'addition',
                        'New fraud claim added', 0.65, 'rule_based')
            """, (event_id, matter_id, tenant_id))
            conn.commit()

            cur.execute(
                "SELECT dimension, drift_type, magnitude FROM drift_events WHERE id = %s",
                (event_id,)
            )
            row = cur.fetchone()

        assert row["dimension"] == "theory_drift"
        assert row["drift_type"] == "addition"
        assert abs(row["magnitude"] - 0.65) < 0.001
    finally:
        conn.close()


def test_run_with_mocked_ai():
    """
    Full job run with AI mocked. Verifies version is written
    and drift events generated for a matter with a prior version.
    """
    from jobs.issue_map_engine import run, _empty_issue_map

    matter_id = str(uuid.uuid4())
    tenant_id = "hjmm-prod           "

    # Seed a prior version so differential fires
    prior_map = {
        "claims": [{"claim_name": "Securities Fraud", "elements": []}],
        "defenses": [],
        "key_players": [{"name": "Andrew Fastow", "role": "CFO"}],
        "key_entities": ["Enron"],
        "relevant_time_period": {"start": None, "end": None, "description": ""},
        "key_events": [],
    }
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO issue_map_versions
                    (id, matter_id, tenant_id, version_num, trigger_type,
                     source_doc_ids, issue_map, differential, magnitude)
                VALUES (gen_random_uuid(), %s, %s, 1, 'manual',
                        %s, %s, %s, 0.0)
            """, (matter_id, tenant_id, [], json.dumps(prior_map), json.dumps({})))
            conn.commit()
    finally:
        conn.close()

    # New issue map that differs — adds a claim
    new_map = {
        "claims": [
            {"claim_name": "Securities Fraud", "elements": []},
            {"claim_name": "Wire Fraud", "elements": []},   # added
        ],
        "defenses": [],
        "key_players": [{"name": "Andrew Fastow", "role": "CFO"}],
        "key_entities": ["Enron"],
        "relevant_time_period": {"start": None, "end": None, "description": ""},
        "key_events": [],
    }

    with patch("jobs.issue_map_engine._generate_issue_map", return_value=new_map), \
         patch("jobs.issue_map_engine._get_ai_client") as mock_ai:
        # Mock drift AI to return empty (rule-based fallback will fire)
        mock_ai.side_effect = RuntimeError("no key in test")

        result = run(
            matter_id=matter_id,
            tenant_id=tenant_id,
            trigger_type="manual",
        )

    assert result["status"] == "complete"
    assert result["version_num"] == 2
    assert result["magnitude"] > 0.0
    assert result["drift_count"] >= 1  # rule-based drift: Wire Fraud added
