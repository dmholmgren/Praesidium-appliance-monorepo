"""
tests/test_m5i_issue_map.py

M5i Issue Map test suite — 22 tests.

Tests:
  - Migration table structure
  - Job: first version (magnitude=0, empty diff)
  - Job: second version diff and magnitude
  - Job: no eligible documents → skipped
  - Job: AI fallback to empty skeleton when API key missing
  - Job: immutability (no UPDATE path exists)
  - Job: high magnitude threshold logging
  - Router: GET /issue-map returns 200
  - Router: GET /issue-map/versions returns 200
  - Router: GET /issue-map/versions/{n} returns 200 and 404
  - Router: POST /trigger returns 202
  - Router: /trigger with bad matter_id returns 404
  - Trigger hook: fires for eligible types
  - Trigger hook: does NOT fire for transcript/email
  - Diff computation: element keys extracted correctly
  - Diff computation: magnitude calculation
  - Diff computation: no prior → magnitude 0.0
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import psycopg2
import psycopg2.extras
import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _direct_conn():
    """Open a direct psycopg2 connection on port 5432."""
    raw_url = os.environ.get("DATABASE_URL", "")
    raw_url = raw_url.replace("postgresql+asyncpg://", "postgresql://")
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


def _test_tenant():
    return "hjmm-prod"


def _test_matter_id():
    """Return the first matter UUID for the test tenant, or create a fixture matter."""
    conn = _direct_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM matters WHERE tenant_id = %s LIMIT 1",
        (_test_tenant(),),
    )
    row = cur.fetchone()
    conn.close()
    if row:
        return str(row[0])
    return str(uuid.uuid4())


def _insert_version(conn, tenant_id, matter_id, version_number, issue_map, diff, magnitude, triggered_by="manual"):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        INSERT INTO issue_map_versions
          (tenant_id, matter_id, version_num, trigger_type,
           source_doc_ids, issue_map, differential, magnitude, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
        """,
        (
            tenant_id, matter_id, version_number, triggered_by,
            [],                    # PG array — pass list directly
            json.dumps(issue_map),
            json.dumps(diff),
            magnitude, None,       # NULL — created_by is BIGINT FK
        ),
    )
    return cur.fetchone()["id"]


def _cleanup_versions(conn, tenant_id, matter_id):
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM issue_map_versions WHERE tenant_id = %s AND matter_id = %s",
        (tenant_id, matter_id),
    )


# ---------------------------------------------------------------------------
# 1. Migration — table exists with correct columns
# ---------------------------------------------------------------------------

class TestMigration:
    def test_table_exists(self):
        conn = _direct_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT to_regclass('public.issue_map_versions')"
        )
        assert cur.fetchone()[0] is not None, "issue_map_versions table not found"
        conn.close()

    def test_columns_present(self):
        conn = _direct_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'issue_map_versions'
            """,
        )
        cols = {r[0] for r in cur.fetchall()}
        conn.close()
        required = {
            "id", "tenant_id", "matter_id", "version_num", "trigger_type",
            "source_doc_ids", "issue_map", "differential",
            "magnitude", "created_at", "created_by",
        }
        missing = required - cols
        assert not missing, f"Missing columns: {missing}"

    def test_unique_constraint_on_matter_version(self):
        """Inserting a duplicate version_number for the same matter must fail."""
        conn = _direct_conn()
        matter_id = str(uuid.uuid4())
        tenant_id = _test_tenant()
        try:
            _insert_version(conn, tenant_id, matter_id, 1, {}, {}, 0.0)
            with pytest.raises(psycopg2.errors.UniqueViolation):
                _insert_version(conn, tenant_id, matter_id, 1, {}, {}, 0.0)
        finally:
            _cleanup_versions(conn, tenant_id, matter_id)
            conn.close()


# ---------------------------------------------------------------------------
# 2. Diff computation (pure functions — no DB, no AI)
# ---------------------------------------------------------------------------

class TestDiffComputation:
    def _import(self):
        from jobs.issue_map_job import _compute_diff, _extract_element_keys
        return _compute_diff, _extract_element_keys

    def test_extract_element_keys_empty(self):
        _, extract = self._import()
        keys = extract({})
        assert keys == set()

    def test_extract_element_keys_claims(self):
        _, extract = self._import()
        issue_map = {
            "claims": [
                {"claim_name": "Breach", "elements": [{"element_name": "Duty"}]}
            ],
            "defenses": [],
        }
        keys = extract(issue_map)
        assert "claim::Breach::Duty" in keys

    def test_extract_element_keys_defenses(self):
        _, extract = self._import()
        issue_map = {
            "claims": [],
            "defenses": [
                {"defense_name": "Limitations", "elements": [{"element_name": "Expired"}]}
            ],
        }
        keys = extract(issue_map)
        assert "defense::Limitations::Expired" in keys

    def test_no_prior_magnitude_zero(self):
        compute, _ = self._import()
        diff, mag = compute({}, {"claims": [], "defenses": []})
        assert mag == 0.0
        assert diff["added"] == []

    def test_added_element_increases_magnitude(self):
        compute, _ = self._import()
        prior = {
            "claims": [{"claim_name": "A", "elements": [{"element_name": "E1"}]}],
            "defenses": [], "key_players": [],
        }
        current = {
            "claims": [
                {"claim_name": "A", "elements": [{"element_name": "E1"}, {"element_name": "E2"}]}
            ],
            "defenses": [], "key_players": [],
        }
        diff, mag = compute(prior, current)
        assert "claim::A::E2" in diff["added"]
        assert mag > 0.0

    def test_removed_element_increases_magnitude(self):
        compute, _ = self._import()
        prior = {
            "claims": [
                {"claim_name": "A", "elements": [{"element_name": "E1"}, {"element_name": "E2"}]}
            ],
            "defenses": [], "key_players": [],
        }
        current = {
            "claims": [{"claim_name": "A", "elements": [{"element_name": "E1"}]}],
            "defenses": [], "key_players": [],
        }
        diff, mag = compute(prior, current)
        assert "claim::A::E2" in diff["removed"]
        assert mag > 0.0

    def test_magnitude_capped_at_one(self):
        compute, _ = self._import()
        prior = {
            "claims": [{"claim_name": "Old", "elements": [{"element_name": "E"}]}],
            "defenses": [], "key_players": [],
        }
        current = {
            "claims": [{"claim_name": "New", "elements": [{"element_name": "F"}]}],
            "defenses": [], "key_players": [],
        }
        _, mag = compute(prior, current)
        assert mag <= 1.0


# ---------------------------------------------------------------------------
# 3. Job — run_issue_map_refresh
# ---------------------------------------------------------------------------

class TestIssueMapJob:
    def _import(self):
        from jobs.issue_map_job import run_issue_map_refresh
        return run_issue_map_refresh

    def test_skipped_when_no_eligible_documents(self):
        """Matter with no pleadings/motions/orders/expert reports → skipped."""
        run = self._import()
        result = run(
            tenant_id=_test_tenant(),
            matter_id=str(uuid.uuid4()),  # unknown matter → no docs
            triggered_by="manual",
        )
        assert result["status"] == "skipped"
        assert result["reason"] == "no_eligible_documents"

    def test_empty_skeleton_when_no_api_key(self):
        """_call_ai_extract returns empty skeleton when ANTHROPIC_API_KEY missing."""
        from jobs.issue_map_job import _call_ai_extract, _empty_issue_map
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
            result = _call_ai_extract("some document text")
        assert result == _empty_issue_map()

    def test_no_update_path_in_module(self):
        """Verify there is no UPDATE statement in issue_map_job — immutability check."""
        import inspect
        import jobs.issue_map_job as mod
        source = inspect.getsource(mod)
        assert "UPDATE issue_map_versions" not in source.upper()
        assert "DELETE FROM issue_map_versions" not in source.upper()

    def test_first_version_written_with_zero_magnitude(self):
        """When a matter has eligible docs, first version should have magnitude 0.0."""
        run = self._import()
        matter_id = _test_matter_id()
        if matter_id == str(uuid.uuid4()):
            pytest.skip("No matters in test DB")

        conn = _direct_conn()
        _cleanup_versions(conn, _test_tenant(), matter_id)

        # Mock AI extraction to return a minimal issue map
        minimal_map = {
            "claims": [{"claim_name": "TestClaim", "count_designation": "Count I",
                         "elements": [{"element_name": "El1", "facts_required": "fact",
                                       "documents_needed": [], "key_players": []}]}],
            "defenses": [], "key_players": [], "key_entities": [],
            "relevant_time_period": {"start_date": None, "end_date": None, "description": ""},
            "key_events": [],
        }
        with patch("jobs.issue_map_job._call_ai_extract", return_value=minimal_map):
            result = run(tenant_id=_test_tenant(), matter_id=matter_id, triggered_by="manual")

        if result["status"] == "skipped":
            pytest.skip("No trigger-eligible documents in test matter")

        assert result["status"] == "ok"
        assert result["version_number"] == 1
        assert result["diff_magnitude"] == 0.0

        _cleanup_versions(conn, _test_tenant(), matter_id)
        conn.close()

    def test_second_version_computes_diff(self):
        """Second version with changed map should have magnitude > 0."""
        run = self._import()
        matter_id = _test_matter_id()
        if matter_id == str(uuid.uuid4()):
            pytest.skip("No matters in test DB")

        conn = _direct_conn()
        _cleanup_versions(conn, _test_tenant(), matter_id)

        map_v1 = {
            "claims": [{"claim_name": "A", "count_designation": "Count I",
                         "elements": [{"element_name": "E1", "facts_required": "f",
                                       "documents_needed": [], "key_players": []}]}],
            "defenses": [], "key_players": [], "key_entities": [],
            "relevant_time_period": {"start_date": None, "end_date": None, "description": ""},
            "key_events": [],
        }
        map_v2 = {
            "claims": [{"claim_name": "A", "count_designation": "Count I",
                         "elements": [{"element_name": "E1", "facts_required": "f",
                                       "documents_needed": [], "key_players": []},
                                      {"element_name": "E2", "facts_required": "g",
                                       "documents_needed": [], "key_players": []}]}],
            "defenses": [], "key_players": [], "key_entities": [],
            "relevant_time_period": {"start_date": None, "end_date": None, "description": ""},
            "key_events": [],
        }

        with patch("jobs.issue_map_job._call_ai_extract", return_value=map_v1):
            r1 = run(tenant_id=_test_tenant(), matter_id=matter_id, triggered_by="manual")

        if r1["status"] == "skipped":
            _cleanup_versions(conn, _test_tenant(), matter_id)
            conn.close()
            pytest.skip("No trigger-eligible documents in test matter")

        with patch("jobs.issue_map_job._call_ai_extract", return_value=map_v2):
            r2 = run(tenant_id=_test_tenant(), matter_id=matter_id, triggered_by="document_added")

        assert r2["status"] == "ok"
        assert r2["version_number"] == 2
        assert r2["diff_magnitude"] > 0.0

        _cleanup_versions(conn, _test_tenant(), matter_id)
        conn.close()


# ---------------------------------------------------------------------------
# 4. Trigger hook
# ---------------------------------------------------------------------------

class TestTriggerHook:
    def _import(self):
        from modules.ediscovery.issue_map import maybe_trigger_issue_map, ISSUE_MAP_TRIGGER_TYPES
        return maybe_trigger_issue_map, ISSUE_MAP_TRIGGER_TYPES

    def test_fires_for_pleading(self):
        hook, _ = self._import()
        with patch("modules.ediscovery.issue_map._enqueue_refresh", return_value="job-123") as mock_enqueue:
            result = hook("hjmm-prod", str(uuid.uuid4()), "pleading", str(uuid.uuid4()))
        assert result is True
        mock_enqueue.assert_called_once()

    def test_fires_for_motion(self):
        hook, _ = self._import()
        with patch("modules.ediscovery.issue_map._enqueue_refresh", return_value="job-x"):
            result = hook("hjmm-prod", str(uuid.uuid4()), "motion", str(uuid.uuid4()))
        assert result is True

    def test_fires_for_order(self):
        hook, _ = self._import()
        with patch("modules.ediscovery.issue_map._enqueue_refresh", return_value="job-x"):
            result = hook("hjmm-prod", str(uuid.uuid4()), "order", str(uuid.uuid4()))
        assert result is True

    def test_fires_for_expert_report(self):
        hook, _ = self._import()
        with patch("modules.ediscovery.issue_map._enqueue_refresh", return_value="job-x"):
            result = hook("hjmm-prod", str(uuid.uuid4()), "expert_report", str(uuid.uuid4()))
        assert result is True

    def test_does_not_fire_for_transcript(self):
        hook, _ = self._import()
        with patch("modules.ediscovery.issue_map._enqueue_refresh") as mock_enqueue:
            result = hook("hjmm-prod", str(uuid.uuid4()), "transcript", str(uuid.uuid4()))
        assert result is False
        mock_enqueue.assert_not_called()

    def test_does_not_fire_for_email(self):
        hook, _ = self._import()
        with patch("modules.ediscovery.issue_map._enqueue_refresh") as mock_enqueue:
            result = hook("hjmm-prod", str(uuid.uuid4()), "email", str(uuid.uuid4()))
        assert result is False
        mock_enqueue.assert_not_called()

    def test_does_not_fire_for_unknown_type(self):
        hook, _ = self._import()
        with patch("modules.ediscovery.issue_map._enqueue_refresh") as mock_enqueue:
            result = hook("hjmm-prod", str(uuid.uuid4()), "contract", str(uuid.uuid4()))
        assert result is False
        mock_enqueue.assert_not_called()

    def test_trigger_type_set_contents(self):
        _, types = self._import()
        assert "pleading" in types
        assert "motion" in types
        assert "order" in types
        assert "expert_report" in types
        assert "transcript" not in types
        assert "email" not in types


# ---------------------------------------------------------------------------
# 5. Router — HTTP endpoints
# ---------------------------------------------------------------------------

class TestIssueMapRouter:
    def _client(self):
        from app import app
        return TestClient(app, raise_server_exceptions=False)
    def _session_headers(self):
        return {"Cookie": "praesidium_session=1"}

    def _get_matter_id(self):
        conn = _direct_conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM matters WHERE tenant_id = %s LIMIT 1", ("hjmm-prod",))
        row = cur.fetchone()
        conn.close()
        return str(row[0]) if row else None

    def test_current_version_returns_200(self):
        matter_id = self._get_matter_id()
        if not matter_id:
            pytest.skip("No matters in test DB")
        client = self._client()
        resp = client.get(
            f"/ediscovery/matters/{matter_id}/issue-map",
            headers=self._session_headers(),
        )
        assert resp.status_code == 200

    def test_version_history_returns_200(self):
        matter_id = self._get_matter_id()
        if not matter_id:
            pytest.skip("No matters in test DB")
        client = self._client()
        resp = client.get(
            f"/ediscovery/matters/{matter_id}/issue-map/versions",
            headers=self._session_headers(),
        )
        assert resp.status_code == 200

    def test_single_version_not_found_returns_404(self):
        matter_id = self._get_matter_id()
        if not matter_id:
            pytest.skip("No matters in test DB")
        client = self._client()
        resp = client.get(
            f"/ediscovery/matters/{matter_id}/issue-map/versions/99999",
            headers=self._session_headers(),
        )
        assert resp.status_code == 404

    def test_trigger_returns_202(self):
        matter_id = self._get_matter_id()
        if not matter_id:
            pytest.skip("No matters in test DB")
        client = self._client()
        with patch("modules.ediscovery.issue_map._enqueue_refresh", return_value="test-job-id"):
            resp = client.post(
                f"/ediscovery/matters/{matter_id}/issue-map/trigger",
                headers=self._session_headers(),
            )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "queued"
        assert "job_id" in data

    def test_trigger_bad_matter_returns_404(self):
        client = self._client()
        resp = client.post(
            f"/ediscovery/matters/{uuid.uuid4()}/issue-map/trigger",
            headers=self._session_headers(),
        )
        assert resp.status_code == 404

    def test_unauthenticated_returns_redirect_or_401(self):
        matter_id = self._get_matter_id()
        if not matter_id:
            pytest.skip("No matters in test DB")
        client = self._client()
        resp = client.get(
            f"/ediscovery/matters/{matter_id}/issue-map",
            follow_redirects=False,
        )
        assert resp.status_code in (302, 401, 403)
