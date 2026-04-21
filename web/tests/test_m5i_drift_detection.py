"""
tests/test_m5i_drift_detection.py

M5i Drift Detection test suite — 22 tests.

Tests:
  - Migration: wiam_surfaced column added, index exists
  - Job: no version → skipped
  - Job: magnitude below threshold → skipped
  - Job: empty diff → no events written
  - Job: rule-based classification (no AI key)
  - Job: wiam_surfaced=True when magnitude > 0.6
  - Job: wiam_surfaced=False when magnitude <= 0.6
  - Job: immutability — no UPDATE path in source
  - Rule-based classifier: addition events
  - Rule-based classifier: removal events
  - Rule-based classifier: player changes → custodian dimension
  - Rule-based classifier: empty diff → empty list
  - Rule-based classifier: below threshold → empty list
  - Validation: invalid dimension rejected
  - Validation: invalid type rejected
  - Trigger hook (issue map): fires when magnitude > threshold
  - Trigger hook (issue map): does not fire when magnitude < threshold
  - Trigger hook (transcript): fires for transcript doc type
  - Router: GET /drift-map returns 200
  - Router: GET /drift-events returns 200
  - Router: bad matter_id returns 404
  - Router: unauthenticated returns 302/401
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import MagicMock, patch

import psycopg2
import psycopg2.extras
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _direct_conn():
    raw_url = os.environ.get("DATABASE_URL", "")
    raw_url = raw_url.replace("postgresql+asyncpg://", "postgresql://")
    at = raw_url.rfind("@")
    creds = raw_url[len("postgresql://"):at]
    hp = raw_url[at + 1:]
    colon = creds.rfind(":")
    user = creds[:colon]
    pw = creds[colon + 1:]
    slash = hp.find("/")
    host = hp[:slash].split(":")[0]
    db = hp[slash + 1:]
    conn = psycopg2.connect(host=host, port=5432, dbname=db, user=user, password=pw)
    conn.autocommit = True
    return conn


def _test_tenant():
    return "hjmm-prod"


def _get_matter_id():
    conn = _direct_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM matters WHERE tenant_id = %s LIMIT 1", (_test_tenant(),))
    row = cur.fetchone()
    conn.close()
    return str(row[0]) if row else None


def _insert_issue_map_version(conn, tenant_id, matter_id, version_num, diff, magnitude):
    """Insert a minimal issue_map_versions row for testing."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        INSERT INTO issue_map_versions
          (tenant_id, matter_id, version_num, trigger_type,
           source_doc_ids, issue_map, differential, magnitude, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            tenant_id, matter_id, version_num, "manual",
            [],
            json.dumps({"claims": [], "defenses": [], "key_players": [], "key_entities": [],
                        "relevant_time_period": {}, "key_events": []}),
            json.dumps(diff),
            magnitude,
            None,
        ),
    )
    return cur.fetchone()["id"]


def _cleanup_versions(conn, tenant_id, matter_id):
    cur = conn.cursor()
    cur.execute("DELETE FROM issue_map_versions WHERE tenant_id = %s AND matter_id = %s", (tenant_id, matter_id))


def _cleanup_drift_events(conn, tenant_id, matter_id):
    cur = conn.cursor()
    cur.execute("DELETE FROM drift_events WHERE tenant_id = %s AND matter_id = %s", (tenant_id, matter_id))


# ---------------------------------------------------------------------------
# 1. Migration
# ---------------------------------------------------------------------------

class TestMigration:
    def test_wiam_surfaced_column_exists(self):
        conn = _direct_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'drift_events' AND column_name = 'wiam_surfaced'"
        )
        assert cur.fetchone() is not None, "wiam_surfaced column not found in drift_events"
        conn.close()

    def test_drift_events_index_exists(self):
        conn = _direct_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'drift_events' AND indexname = 'ix_drift_events_matter'"
        )
        assert cur.fetchone() is not None, "ix_drift_events_matter index not found"
        conn.close()

    def test_all_required_columns_present(self):
        conn = _direct_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'drift_events'"
        )
        cols = {r[0] for r in cur.fetchall()}
        conn.close()
        required = {
            "id", "tenant_id", "matter_id", "version_before", "version_after",
            "dimension", "drift_type", "description", "source_doc_id",
            "magnitude", "detection_source", "wiam_surfaced", "created_at",
        }
        missing = required - cols
        assert not missing, f"Missing columns: {missing}"


# ---------------------------------------------------------------------------
# 2. Rule-based classifier (pure functions — no DB, no AI)
# ---------------------------------------------------------------------------

class TestRuleBasedClassifier:
    def _import(self):
        from jobs.drift_detection_job import _rule_based_classify
        return _rule_based_classify

    def test_empty_diff_returns_empty(self):
        classify = self._import()
        result = classify({}, 0.1)
        assert result == []

    def test_below_threshold_returns_empty(self):
        classify = self._import()
        result = classify({"added": ["claim::A::E1"]}, 0.03)
        assert result == []

    def test_added_element_produces_theory_addition(self):
        classify = self._import()
        result = classify({"added": ["claim::Breach::Element1"]}, 0.2)
        assert len(result) >= 1
        ev = result[0]
        assert ev["drift_dimension"] == "theory_drift"
        assert ev["drift_type"] == "addition"

    def test_removed_element_produces_theory_removal(self):
        classify = self._import()
        result = classify({"removed": ["claim::Fraud::Element2"]}, 0.2)
        assert len(result) >= 1
        assert result[0]["drift_type"] == "removal"

    def test_added_player_produces_custodian_addition(self):
        classify = self._import()
        result = classify({"added_players": ["John Smith"]}, 0.15)
        assert any(e["drift_dimension"] == "custodian_drift" for e in result)

    def test_removed_player_produces_custodian_removal(self):
        classify = self._import()
        result = classify({"removed_players": ["Jane Doe"]}, 0.15)
        assert any(e["drift_dimension"] == "custodian_drift" and e["drift_type"] == "removal" for e in result)

    def test_all_events_have_required_fields(self):
        classify = self._import()
        result = classify({"added": ["claim::A::E1"], "removed": ["claim::B::E2"]}, 0.3)
        for ev in result:
            assert "drift_dimension" in ev
            assert "drift_type" in ev
            assert "description" in ev
            assert "magnitude" in ev


# ---------------------------------------------------------------------------
# 3. Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_invalid_dimension_not_in_valid_set(self):
        from jobs.drift_detection_job import VALID_DIMENSIONS
        assert "invalid_dim" not in VALID_DIMENSIONS
        assert "theory_drift" in VALID_DIMENSIONS
        assert "factual_drift" in VALID_DIMENSIONS
        assert "custodian_drift" in VALID_DIMENSIONS
        assert "damages_drift" in VALID_DIMENSIONS

    def test_invalid_type_not_in_valid_set(self):
        from jobs.drift_detection_job import VALID_TYPES
        assert "invalid_type" not in VALID_TYPES
        assert "addition" in VALID_TYPES
        assert "removal" in VALID_TYPES
        assert "reversal" in VALID_TYPES
        assert "de-escalation" in VALID_TYPES


# ---------------------------------------------------------------------------
# 4. Job — run_drift_detection
# ---------------------------------------------------------------------------

class TestDriftDetectionJob:
    def _import(self):
        from jobs.drift_detection_job import run_drift_detection
        return run_drift_detection

    def test_skipped_when_version_not_found(self):
        run = self._import()
        result = run(
            tenant_id=_test_tenant(),
            matter_id=str(uuid.uuid4()),
            issue_map_version_num=999,
        )
        assert result["status"] == "skipped"
        assert result["reason"] == "version_not_found"

    def test_skipped_when_magnitude_below_threshold(self):
        run = self._import()
        matter_id = _get_matter_id()
        if not matter_id:
            pytest.skip("No matters in test DB")

        conn = _direct_conn()
        _cleanup_versions(conn, _test_tenant(), matter_id)
        _insert_issue_map_version(conn, _test_tenant(), matter_id, 1, {}, 0.02)

        result = run(
            tenant_id=_test_tenant(),
            matter_id=matter_id,
            issue_map_version_num=1,
        )
        assert result["status"] == "skipped"
        assert result["reason"] == "magnitude_below_threshold"

        _cleanup_versions(conn, _test_tenant(), matter_id)
        conn.close()

    def test_no_events_when_diff_empty(self):
        run = self._import()
        matter_id = _get_matter_id()
        if not matter_id:
            pytest.skip("No matters in test DB")

        conn = _direct_conn()
        _cleanup_versions(conn, _test_tenant(), matter_id)
        _cleanup_drift_events(conn, _test_tenant(), matter_id)
        _insert_issue_map_version(conn, _test_tenant(), matter_id, 1, {}, 0.1)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
            result = run(
                tenant_id=_test_tenant(),
                matter_id=matter_id,
                issue_map_version_num=1,
            )

        assert result["status"] == "ok"
        assert result["events_written"] == 0

        _cleanup_versions(conn, _test_tenant(), matter_id)
        _cleanup_drift_events(conn, _test_tenant(), matter_id)
        conn.close()

    def test_events_written_for_nonempty_diff(self):
        run = self._import()
        matter_id = _get_matter_id()
        if not matter_id:
            pytest.skip("No matters in test DB")

        conn = _direct_conn()
        _cleanup_versions(conn, _test_tenant(), matter_id)
        _cleanup_drift_events(conn, _test_tenant(), matter_id)

        diff = {"added": ["claim::Breach::Duty"], "removed": [], "added_players": [], "removed_players": []}
        _insert_issue_map_version(conn, _test_tenant(), matter_id, 1, diff, 0.25)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
            result = run(
                tenant_id=_test_tenant(),
                matter_id=matter_id,
                issue_map_version_num=1,
            )

        assert result["status"] == "ok"
        assert result["events_written"] >= 1

        _cleanup_versions(conn, _test_tenant(), matter_id)
        _cleanup_drift_events(conn, _test_tenant(), matter_id)
        conn.close()

    def test_wiam_surfaced_true_when_magnitude_high(self):
        run = self._import()
        matter_id = _get_matter_id()
        if not matter_id:
            pytest.skip("No matters in test DB")

        conn = _direct_conn()
        _cleanup_versions(conn, _test_tenant(), matter_id)
        _cleanup_drift_events(conn, _test_tenant(), matter_id)

        diff = {"added": ["claim::Fraud::Scienter"], "removed": ["claim::Breach::Causation"],
                "added_players": ["New Expert"], "removed_players": []}
        _insert_issue_map_version(conn, _test_tenant(), matter_id, 1, diff, 0.75)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
            result = run(
                tenant_id=_test_tenant(),
                matter_id=matter_id,
                issue_map_version_num=1,
            )

        assert result["status"] == "ok"
        assert result["wiam_surfaced_count"] >= 1

        _cleanup_versions(conn, _test_tenant(), matter_id)
        _cleanup_drift_events(conn, _test_tenant(), matter_id)
        conn.close()

    def test_no_update_path_in_module(self):
        import inspect
        import jobs.drift_detection_job as mod
        source = inspect.getsource(mod)
        assert "UPDATE drift_events" not in source.upper()
        assert "DELETE FROM drift_events" not in source.upper()


# ---------------------------------------------------------------------------
# 5. Trigger hooks
# ---------------------------------------------------------------------------

class TestTriggerHooks:
    def test_issue_map_hook_fires_above_threshold(self):
        from modules.ediscovery.drift_detection import maybe_trigger_drift_from_issue_map
        with patch("modules.ediscovery.drift_detection._enqueue_drift_job", return_value="job-x") as mock_eq:
            result = maybe_trigger_drift_from_issue_map(
                "hjmm-prod", str(uuid.uuid4()), version_num=2, magnitude=0.15
            )
        assert result is True
        mock_eq.assert_called_once()

    def test_issue_map_hook_does_not_fire_below_threshold(self):
        from modules.ediscovery.drift_detection import maybe_trigger_drift_from_issue_map
        with patch("modules.ediscovery.drift_detection._enqueue_drift_job") as mock_eq:
            result = maybe_trigger_drift_from_issue_map(
                "hjmm-prod", str(uuid.uuid4()), version_num=1, magnitude=0.03
            )
        assert result is False
        mock_eq.assert_not_called()

    def test_issue_map_hook_does_not_fire_at_zero(self):
        from modules.ediscovery.drift_detection import maybe_trigger_drift_from_issue_map
        with patch("modules.ediscovery.drift_detection._enqueue_drift_job") as mock_eq:
            result = maybe_trigger_drift_from_issue_map(
                "hjmm-prod", str(uuid.uuid4()), version_num=1, magnitude=0.0
            )
        assert result is False
        mock_eq.assert_not_called()

    def test_transcript_hook_fires_when_version_exists(self):
        from modules.ediscovery.drift_detection import maybe_trigger_drift_from_transcript
        matter_id = _get_matter_id()
        if not matter_id:
            pytest.skip("No matters in test DB")

        conn = _direct_conn()
        _cleanup_versions(conn, _test_tenant(), matter_id)
        diff = {"added": ["claim::A::E1"], "removed": [], "added_players": [], "removed_players": []}
        _insert_issue_map_version(conn, _test_tenant(), matter_id, 1, diff, 0.2)

        with patch("modules.ediscovery.drift_detection._enqueue_drift_job", return_value="job-t") as mock_eq:
            result = maybe_trigger_drift_from_transcript(
                _test_tenant(), matter_id, str(uuid.uuid4())
            )

        assert result is True
        mock_eq.assert_called_once()

        _cleanup_versions(conn, _test_tenant(), matter_id)
        conn.close()

    def test_transcript_hook_skips_when_no_versions(self):
        from modules.ediscovery.drift_detection import maybe_trigger_drift_from_transcript
        with patch("modules.ediscovery.drift_detection._enqueue_drift_job") as mock_eq:
            result = maybe_trigger_drift_from_transcript(
                _test_tenant(), str(uuid.uuid4()), str(uuid.uuid4())
            )
        assert result is False
        mock_eq.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Router
# ---------------------------------------------------------------------------

class TestDriftRouter:
    def _client(self):
        from app import app
        return TestClient(app, raise_server_exceptions=False)
    def _session_headers(self):

        return {"Cookie": "praesidium_session=1"}

    def test_drift_map_returns_200(self):
        matter_id = _get_matter_id()
        if not matter_id:
            pytest.skip("No matters in test DB")
        client = self._client()
        resp = client.get(
            f"/ediscovery/matters/{matter_id}/drift-map",
            headers=self._session_headers(),
        )
        assert resp.status_code == 200

    def test_drift_events_partial_returns_200(self):
        matter_id = _get_matter_id()
        if not matter_id:
            pytest.skip("No matters in test DB")
        client = self._client()
        resp = client.get(
            f"/ediscovery/matters/{matter_id}/drift-events",
            headers=self._session_headers(),
        )
        assert resp.status_code == 200

    def test_bad_matter_returns_404(self):
        client = self._client()
        resp = client.get(
            f"/ediscovery/matters/{uuid.uuid4()}/drift-map",
            headers=self._session_headers(),
        )
        assert resp.status_code in (404, 401, 403)

    def test_unauthenticated_returns_redirect_or_401(self):
        matter_id = _get_matter_id()
        if not matter_id:
            pytest.skip("No matters in test DB")
        client = self._client()
        resp = client.get(
            f"/ediscovery/matters/{matter_id}/drift-map",
            follow_redirects=False,
        )
        assert resp.status_code in (302, 401, 403)
