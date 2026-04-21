"""
M5i-D — WIAM Engine Tests
Tests citation validation, finding write/reject logic, session lifecycle,
drift gap auto-surfacing, and full job run with mocked AI.
Expected: 16 passed
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


def _make_session(matter_id: str, tenant_id: str) -> str:
    """Insert a wiam_session and return its id."""
    sid = str(uuid.uuid4())
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO wiam_sessions
                    (id, matter_id, tenant_id, triggered_by, status)
                VALUES (%s, %s, %s, 'manual', 'running')
            """, (sid, matter_id, tenant_id))
            conn.commit()
    finally:
        conn.close()
    return sid


def _make_ai_response(text: str):
    content = MagicMock()
    content.text = text
    response = MagicMock()
    response.content = [content]
    return response


# ================================================================== #
# Import tests                                                        #
# ================================================================== #

def test_job_importable():
    from jobs import wiam_engine
    assert hasattr(wiam_engine, "run")


def test_validate_citations_importable():
    from jobs.wiam_engine import _validate_citations
    assert callable(_validate_citations)


def test_write_finding_importable():
    from jobs.wiam_engine import _write_finding
    assert callable(_write_finding)


# ================================================================== #
# Citation validation — hard enforcement (Claim 11)                  #
# ================================================================== #

def test_citation_valid():
    from jobs.wiam_engine import _validate_citations
    citations = [{"doc_id": str(uuid.uuid4()), "page": "5", "line": "12"}]
    assert _validate_citations(citations) is True


def test_citation_empty_list_rejected():
    from jobs.wiam_engine import _validate_citations
    assert _validate_citations([]) is False


def test_citation_none_rejected():
    from jobs.wiam_engine import _validate_citations
    assert _validate_citations(None) is False


def test_citation_missing_doc_id_rejected():
    from jobs.wiam_engine import _validate_citations
    assert _validate_citations([{"page": "1", "line": "1"}]) is False


def test_citation_missing_page_rejected():
    from jobs.wiam_engine import _validate_citations
    assert _validate_citations([{"doc_id": str(uuid.uuid4()), "line": "1"}]) is False


def test_citation_missing_line_rejected():
    from jobs.wiam_engine import _validate_citations
    assert _validate_citations([{"doc_id": str(uuid.uuid4()), "page": "1"}]) is False


# ================================================================== #
# Finding write and rejection tests                                   #
# ================================================================== #

def test_write_finding_valid_citations():
    """Finding with valid citations is written and returns an ID."""
    from jobs.wiam_engine import _write_finding
    session_id = str(uuid.uuid4())
    matter_id  = str(uuid.uuid4())
    tenant_id  = "hjmm-prod           "
    doc_id     = str(uuid.uuid4())

    # Seed session
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO wiam_sessions
                    (id, matter_id, tenant_id, triggered_by, status)
                VALUES (%s, %s, %s, 'manual', 'running')
            """, (session_id, matter_id, tenant_id))
            conn.commit()

        citations = [{"doc_id": doc_id, "page": "12", "line": "5",
                      "excerpt_summary": "Key admission by CFO"}]
        fid = _write_finding(
            conn=conn,
            session_id=session_id,
            matter_id=matter_id,
            tenant_id=tenant_id,
            finding_type="opp_gap",
            claim_element="Element 2 — Scienter",
            description="No testimony supports OC's scienter allegation.",
            citations=citations,
            confidence=0.85,
            priority="high",
            suggested_action="File motion for partial summary judgment on scienter.",
        )
        conn.commit()
        assert fid is not None
        assert len(fid) == 36  # UUID

        # Verify DB
        with conn.cursor() as cur:
            cur.execute(
                "SELECT finding_type, priority FROM wiam_findings WHERE id = %s",
                (fid,)
            )
            row = cur.fetchone()
        assert row["finding_type"] == "opp_gap"
        assert row["priority"] == "high"
    finally:
        conn.close()


def test_write_finding_empty_citations_rejected():
    """Finding with empty citations returns None and is not written."""
    from jobs.wiam_engine import _write_finding
    session_id = str(uuid.uuid4())
    matter_id  = str(uuid.uuid4())
    tenant_id  = "hjmm-prod           "

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO wiam_sessions
                    (id, matter_id, tenant_id, triggered_by, status)
                VALUES (%s, %s, %s, 'manual', 'running')
            """, (session_id, matter_id, tenant_id))
            conn.commit()

        fid = _write_finding(
            conn=conn,
            session_id=session_id,
            matter_id=matter_id,
            tenant_id=tenant_id,
            finding_type="own_gap",
            claim_element="Element 1",
            description="Some gap",
            citations=[],  # EMPTY — must be rejected
            confidence=0.8,
            priority="medium",
            suggested_action="Do something",
        )
        assert fid is None

        # Confirm not in DB
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM wiam_findings WHERE session_id = %s",
                (session_id,)
            )
            assert cur.fetchone()["cnt"] == 0
    finally:
        conn.close()


# ================================================================== #
# Session lifecycle tests                                             #
# ================================================================== #

def test_session_marked_complete():
    from jobs.wiam_engine import _mark_session
    matter_id = str(uuid.uuid4())
    tenant_id = "hjmm-prod           "
    sid = _make_session(matter_id, tenant_id)

    conn = _get_conn()
    try:
        _mark_session(conn, sid, "complete")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, completed_at FROM wiam_sessions WHERE id = %s", (sid,)
            )
            row = cur.fetchone()
        assert row["status"] == "complete"
        assert row["completed_at"] is not None
    finally:
        conn.close()


def test_session_marked_failed():
    from jobs.wiam_engine import _mark_session
    matter_id = str(uuid.uuid4())
    tenant_id = "hjmm-prod           "
    sid = _make_session(matter_id, tenant_id)

    conn = _get_conn()
    try:
        _mark_session(conn, sid, "failed")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM wiam_sessions WHERE id = %s", (sid,)
            )
            row = cur.fetchone()
        assert row["status"] == "failed"
    finally:
        conn.close()


# ================================================================== #
# Drift gap auto-surfacing                                            #
# ================================================================== #

def test_surface_drift_findings_no_source_doc_skipped():
    """Drift events without source_doc_id are skipped (cannot build citation)."""
    from jobs.wiam_engine import _surface_drift_findings
    matter_id = str(uuid.uuid4())
    tenant_id = "hjmm-prod           "
    sid = _make_session(matter_id, tenant_id)

    ctx = {
        "drift_events": [
            {
                "id": str(uuid.uuid4()),
                "dimension": "theory_drift",
                "drift_type": "addition",
                "description": "New fraud claim added",
                "magnitude": 0.8,
                "source_doc_id": None,  # No source — must be skipped
            }
        ]
    }

    conn = _get_conn()
    try:
        written = _surface_drift_findings(ctx, sid, matter_id, tenant_id, conn)
        assert written == 0
    finally:
        conn.close()


def test_surface_drift_findings_with_source_doc():
    """High-magnitude drift event with source_doc creates a finding."""
    from jobs.wiam_engine import _surface_drift_findings
    matter_id = str(uuid.uuid4())
    tenant_id = "hjmm-prod           "
    sid = _make_session(matter_id, tenant_id)
    src_doc = str(uuid.uuid4())

    ctx = {
        "drift_events": [
            {
                "id": str(uuid.uuid4()),
                "dimension": "theory_drift",
                "drift_type": "addition",
                "description": "New fraud claim added to complaint",
                "magnitude": 0.75,
                "source_doc_id": src_doc,
            }
        ]
    }

    conn = _get_conn()
    try:
        written = _surface_drift_findings(ctx, sid, matter_id, tenant_id, conn)
        assert written == 1

        with conn.cursor() as cur:
            cur.execute(
                "SELECT finding_type, priority FROM wiam_findings WHERE session_id = %s",
                (sid,)
            )
            row = cur.fetchone()
        assert row["finding_type"] == "drift_gap"
        assert row["priority"] == "high"  # magnitude 0.75 >= 0.70
    finally:
        conn.close()


# ================================================================== #
# Full job run with mocked AI                                         #
# ================================================================== #

def test_run_session_not_found():
    """run() with a non-existent session_id returns error status."""
    from jobs.wiam_engine import run
    result = run(
        session_id=str(uuid.uuid4()),
        matter_id=str(uuid.uuid4()),
        tenant_id="hjmm-prod",
    )
    assert result["status"] == "error"


def test_run_full_with_mocked_ai():
    """
    Full WIAM run with AI mocked to return one finding per dimension.
    Verifies session marked complete and findings written.
    """
    from jobs.wiam_engine import run

    matter_id = str(uuid.uuid4())
    tenant_id = "hjmm-prod           "
    sid = _make_session(matter_id, tenant_id)
    doc_id = str(uuid.uuid4())

    # AI returns one valid finding with proper citation
    finding_json = json.dumps([{
        "finding_type": "opp_gap",
        "claim_element": "Element 1 — Breach",
        "description": "Opposing party has no witness who testified to this element.",
        "citations": [{
            "doc_id": doc_id,
            "page": "23",
            "line": "7",
            "excerpt_summary": "Witness testified he had no knowledge of the agreement.",
        }],
        "confidence": 0.82,
        "priority": "high",
        "suggested_action": "File motion for summary judgment on this element.",
    }])

    with patch("jobs.wiam_engine._get_ai_client") as mock_fn, \
         patch("jobs.wiam_engine._assemble_context") as mock_ctx, \
         patch("jobs.wiam_engine._context_summary", return_value="mock context"), \
         patch("jobs.wiam_engine._doc_citation_map", return_value={doc_id: doc_id}):

        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_ai_response(finding_json)
        mock_fn.return_value = mock_client

        mock_ctx.return_value = {
            "issue_map": {"claims": [], "defenses": []},
            "entities": [],
            "admissions": [],
            "contradictions": [],
            "drift_events": [],
            "documents": [{"id": doc_id, "file_name": "complaint.pdf",
                           "doc_type": "pleading", "custodian": None,
                           "doc_date": None, "bates_begin": None, "bates_end": None}],
            "issue_map_version": 1,
            "issue_map_magnitude": 0.0,
        }

        result = run(
            session_id=sid,
            matter_id=matter_id,
            tenant_id=tenant_id,
        )

    assert result["status"] == "complete"
    assert result["session_id"] == sid
    assert result["total_findings"] >= 1

    # Verify session marked complete in DB
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM wiam_sessions WHERE id = %s", (sid,)
            )
            row = cur.fetchone()
        assert row["status"] == "complete"
    finally:
        conn.close()
