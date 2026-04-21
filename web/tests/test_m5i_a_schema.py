"""
M5i-A Schema Tests
Verifies all 6 intelligence layer tables exist with correct columns and constraints.
Uses direct psycopg2 connection — same pattern as other infrastructure tests.
Expected: 18 passed
"""

from __future__ import annotations

import os
import uuid
import pytest
import psycopg2
from psycopg2.extras import RealDictCursor


# ------------------------------------------------------------------ #
# DB connection                                                        #
# ------------------------------------------------------------------ #

def _get_conn():
    """Direct psycopg2 connection to PostgreSQL (port 5432, not PgBouncer)."""
    raw = os.environ.get("DATABASE_URL", "")
    raw = raw.replace("postgresql+asyncpg://", "postgresql://")

    # DB password contains @ — use rfind to split host from credentials
    at = raw.rfind("@")
    creds = raw[len("postgresql://"):at]
    rest = raw[at + 1:]

    user, password = creds.split(":", 1)

    host_port, dbname = rest.rsplit("/", 1)
    host = host_port.split(":")[0]

    return psycopg2.connect(
        host=host,
        port=5432,
        dbname=dbname,
        user=user,
        password=password,
    )


def _tables_exist(names: list) -> set:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = ANY(%s)
            """, (names,))
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def _columns(table: str) -> set:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s AND table_schema = 'public'
            """, (table,))
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# Table existence — 6 tests                                           #
# ------------------------------------------------------------------ #

def test_issue_map_versions_exists():
    assert 'issue_map_versions' in _tables_exist(['issue_map_versions'])


def test_drift_events_exists():
    assert 'drift_events' in _tables_exist(['drift_events'])


def test_kg_entities_exists():
    assert 'kg_entities' in _tables_exist(['kg_entities'])


def test_kg_relationships_exists():
    assert 'kg_relationships' in _tables_exist(['kg_relationships'])


def test_wiam_sessions_exists():
    assert 'wiam_sessions' in _tables_exist(['wiam_sessions'])


def test_wiam_findings_exists():
    assert 'wiam_findings' in _tables_exist(['wiam_findings'])


# ------------------------------------------------------------------ #
# Column completeness — 4 tests                                       #
# ------------------------------------------------------------------ #

def test_issue_map_versions_columns():
    cols = _columns('issue_map_versions')
    required = {
        'id', 'matter_id', 'tenant_id', 'version_num', 'trigger_type',
        'source_doc_ids', 'issue_map', 'differential', 'magnitude',
        'created_by', 'created_at'
    }
    assert required <= cols, f"Missing: {required - cols}"


def test_drift_events_columns():
    cols = _columns('drift_events')
    required = {
        'id', 'matter_id', 'tenant_id', 'dimension', 'drift_type',
        'description', 'magnitude', 'version_before', 'version_after',
        'source_doc_id', 'detection_source', 'created_at'
    }
    assert required <= cols, f"Missing: {required - cols}"


def test_kg_entities_columns():
    cols = _columns('kg_entities')
    required = {
        'id', 'matter_id', 'tenant_id', 'entity_type', 'canonical_name',
        'properties', 'source_doc_id', 'confidence', 'attribution', 'created_at'
    }
    assert required <= cols, f"Missing: {required - cols}"


def test_wiam_findings_columns():
    cols = _columns('wiam_findings')
    required = {
        'id', 'session_id', 'matter_id', 'tenant_id', 'finding_type',
        'claim_element', 'description', 'citations', 'confidence',
        'priority', 'suggested_action', 'disposition',
        'disposed_by', 'disposed_at', 'created_at'
    }
    assert required <= cols, f"Missing: {required - cols}"


# ------------------------------------------------------------------ #
# Constraint enforcement — 4 tests                                    #
# ------------------------------------------------------------------ #

def test_issue_map_version_unique_constraint():
    """matter_id + version_num must be unique."""
    mid = str(uuid.uuid4())
    tid = "test-m5ia-uniq  "
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO issue_map_versions
                    (id, matter_id, tenant_id, version_num, trigger_type)
                VALUES (%s, %s, %s, 1, 'manual')
            """, (str(uuid.uuid4()), mid, tid))
            conn.commit()

            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute("""
                    INSERT INTO issue_map_versions
                        (id, matter_id, tenant_id, version_num, trigger_type)
                    VALUES (%s, %s, %s, 1, 'manual')
                """, (str(uuid.uuid4()), mid, tid))
                conn.commit()
    finally:
        conn.rollback()
        conn.close()


def test_drift_event_dimension_check():
    """Invalid dimension must fail CHECK constraint."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute("""
                    INSERT INTO drift_events
                        (id, matter_id, tenant_id, version_after,
                         dimension, drift_type, description, magnitude)
                    VALUES (gen_random_uuid(), gen_random_uuid(), 'test',
                            1, 'invalid_dim', 'addition', 'test', 0.5)
                """)
                conn.commit()
    finally:
        conn.rollback()
        conn.close()


def test_kg_entity_type_check():
    """Invalid entity_type must fail CHECK constraint."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute("""
                    INSERT INTO kg_entities
                        (id, matter_id, tenant_id, entity_type, canonical_name)
                    VALUES (gen_random_uuid(), gen_random_uuid(), 'test',
                            'invalid_type', 'Test Entity')
                """)
                conn.commit()
    finally:
        conn.rollback()
        conn.close()


def test_wiam_finding_disposition_check():
    """Invalid disposition must fail CHECK constraint."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute("""
                    INSERT INTO wiam_findings
                        (id, session_id, matter_id, tenant_id, finding_type,
                         description, citations, disposition)
                    VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(),
                            'test', 'own_gap', 'test desc', '[]', 'maybe')
                """)
                conn.commit()
    finally:
        conn.rollback()
        conn.close()


# ------------------------------------------------------------------ #
# Read/write round-trips — 3 tests                                    #
# ------------------------------------------------------------------ #

def test_wiam_session_insert_and_read():
    mid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO wiam_sessions
                    (id, matter_id, tenant_id, triggered_by, status)
                VALUES (%s, %s, 'hjmm-prod', 'manual', 'running')
            """, (sid, mid))
            conn.commit()
            cur.execute(
                "SELECT status FROM wiam_sessions WHERE id = %s", (sid,)
            )
            assert cur.fetchone()[0] == 'running'
    finally:
        conn.close()


def test_kg_entity_insert_and_read():
    eid = str(uuid.uuid4())
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO kg_entities
                    (id, matter_id, tenant_id, entity_type,
                     canonical_name, confidence, attribution)
                VALUES (%s, gen_random_uuid(), 'hjmm-prod',
                        'person', 'Kenneth Lay', 0.95, 'ai')
            """, (eid,))
            conn.commit()
            cur.execute(
                "SELECT canonical_name FROM kg_entities WHERE id = %s", (eid,)
            )
            assert cur.fetchone()[0] == 'Kenneth Lay'
    finally:
        conn.close()


def test_magnitude_range_check():
    """magnitude > 1.0 must fail CHECK constraint."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute("""
                    INSERT INTO issue_map_versions
                        (id, matter_id, tenant_id, version_num,
                         trigger_type, magnitude)
                    VALUES (gen_random_uuid(), gen_random_uuid(), 'test',
                            999, 'manual', 1.5)
                """)
                conn.commit()
    finally:
        conn.rollback()
        conn.close()


# ------------------------------------------------------------------ #
# Alembic stamp — 1 test                                              #
# ------------------------------------------------------------------ #

def test_alembic_head_includes_0026_widget_registry():
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT version_num FROM alembic_version
                WHERE version_num = '0026_widget_registry'
            """)
            assert cur.fetchone() is not None, \
                "0026_widget_registry not in alembic_version"
    finally:
        conn.close()
