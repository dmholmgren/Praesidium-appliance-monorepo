"""
M5i-C — Knowledge Graph Extraction Tests
Tests importability, entity upsert, relationship deduplication,
and full job round-trips with mocked AI.
Expected: 16 passed
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import patch, MagicMock

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
    from jobs import kg_extraction
    assert hasattr(kg_extraction, "run_document")
    assert hasattr(kg_extraction, "run_matter")


def test_run_document_callable():
    from jobs.kg_extraction import run_document
    assert callable(run_document)


def test_run_matter_callable():
    from jobs.kg_extraction import run_matter
    assert callable(run_matter)


def test_constants_defined():
    from jobs.kg_extraction import VALID_ENTITY_TYPES, VALID_RELATIONSHIP_TYPES, DEPOSITION_TYPES
    assert "admission" in VALID_ENTITY_TYPES
    assert "contradiction" in VALID_ENTITY_TYPES
    assert "contradicts" in VALID_RELATIONSHIP_TYPES
    assert "admitted_in" in VALID_RELATIONSHIP_TYPES
    assert "deposition_transcript" in DEPOSITION_TYPES


# ================================================================== #
# Entity upsert unit tests                                            #
# ================================================================== #

def test_upsert_entity_creates_new():
    from jobs.kg_extraction import _upsert_entity
    matter_id = str(uuid.uuid4())
    tenant_id = "hjmm-prod           "
    conn = _get_conn()
    try:
        eid = _upsert_entity(
            conn, matter_id, tenant_id,
            "person", "Kenneth Lay",
            {"role": "CEO"}, str(uuid.uuid4()), 0.95, "ai",
        )
        conn.commit()
        assert eid != ""
        # Verify stored
        with conn.cursor() as cur:
            cur.execute(
                "SELECT canonical_name FROM kg_entities WHERE id = %s", (eid,)
            )
            row = cur.fetchone()
        assert row["canonical_name"] == "Kenneth Lay"
    finally:
        conn.close()


def test_upsert_entity_deduplicates():
    """Same canonical_name + entity_type + matter returns same ID."""
    from jobs.kg_extraction import _upsert_entity
    matter_id = str(uuid.uuid4())
    tenant_id = "hjmm-prod           "
    src_doc = str(uuid.uuid4())
    conn = _get_conn()
    try:
        eid1 = _upsert_entity(
            conn, matter_id, tenant_id,
            "person", "Andrew Fastow",
            {"role": "CFO"}, src_doc, 0.9, "ai",
        )
        conn.commit()
        eid2 = _upsert_entity(
            conn, matter_id, tenant_id,
            "person", "Andrew Fastow",
            {"role": "CFO"}, src_doc, 0.9, "ai",
        )
        conn.commit()
        assert eid1 == eid2
    finally:
        conn.close()


def test_upsert_entity_invalid_type_defaults_to_fact():
    """Invalid entity_type defaults to 'fact' without error."""
    from jobs.kg_extraction import _upsert_entity
    matter_id = str(uuid.uuid4())
    tenant_id = "hjmm-prod           "
    conn = _get_conn()
    try:
        eid = _upsert_entity(
            conn, matter_id, tenant_id,
            "invalid_type", "Some Entity",
            {}, str(uuid.uuid4()), 0.5, "ai",
        )
        conn.commit()
        assert eid != ""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT entity_type FROM kg_entities WHERE id = %s", (eid,)
            )
            row = cur.fetchone()
        assert row["entity_type"] == "fact"
    finally:
        conn.close()


def test_insert_relationship_deduplicates():
    """Same entity pair + relationship type is not inserted twice."""
    from jobs.kg_extraction import _upsert_entity, _insert_relationship
    matter_id = str(uuid.uuid4())
    tenant_id = "hjmm-prod           "
    src_doc = str(uuid.uuid4())
    conn = _get_conn()
    try:
        eid_a = _upsert_entity(
            conn, matter_id, tenant_id,
            "person", "Jeff Skilling",
            {"role": "CEO"}, src_doc, 0.9, "ai",
        )
        eid_b = _upsert_entity(
            conn, matter_id, tenant_id,
            "organization", "Enron Corp",
            {}, src_doc, 0.95, "ai",
        )
        conn.commit()

        _insert_relationship(
            conn, matter_id, tenant_id,
            eid_a, eid_b, "employed_by", src_doc, 0.9,
        )
        _insert_relationship(
            conn, matter_id, tenant_id,
            eid_a, eid_b, "employed_by", src_doc, 0.9,
        )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS cnt FROM kg_relationships
                WHERE entity_a_id = %s AND entity_b_id = %s
                  AND relationship_type = 'employed_by'
            """, (eid_a, eid_b))
            row = cur.fetchone()
        assert row["cnt"] == 1
    finally:
        conn.close()


def test_insert_relationship_invalid_type_defaults():
    """Invalid relationship_type defaults to 'referenced_in'."""
    from jobs.kg_extraction import _upsert_entity, _insert_relationship
    matter_id = str(uuid.uuid4())
    tenant_id = "hjmm-prod           "
    src_doc = str(uuid.uuid4())
    conn = _get_conn()
    try:
        eid_a = _upsert_entity(
            conn, matter_id, tenant_id,
            "person", "Lou Pai",
            {}, src_doc, 0.8, "ai",
        )
        eid_b = _upsert_entity(
            conn, matter_id, tenant_id,
            "organization", "Enron EES",
            {}, src_doc, 0.8, "ai",
        )
        conn.commit()
        _insert_relationship(
            conn, matter_id, tenant_id,
            eid_a, eid_b, "invented_relationship", src_doc, 0.5,
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT relationship_type FROM kg_relationships
                WHERE entity_a_id = %s AND entity_b_id = %s
            """, (eid_a, eid_b))
            row = cur.fetchone()
        assert row["relationship_type"] == "referenced_in"
    finally:
        conn.close()


# ================================================================== #
# Integration tests with mocked AI                                    #
# ================================================================== #

def _make_ai_response(text: str):
    """Build a mock Anthropic response object."""
    content = MagicMock()
    content.text = text
    response = MagicMock()
    response.content = [content]
    return response


def test_run_matter_no_documents():
    """run_matter on a matter with no documents returns zero counts."""
    from jobs.kg_extraction import run_matter
    matter_id = str(uuid.uuid4())
    tenant_id = "hjmm-prod"

    result = run_matter(matter_id=matter_id, tenant_id=tenant_id)
    assert result["status"] == "complete"
    assert result["docs_processed"] == 0
    assert result["entities"] == 0


def test_run_document_not_found():
    """run_document for a non-existent doc_id returns not_found."""
    from jobs.kg_extraction import run_document
    result = run_document(
        doc_id=str(uuid.uuid4()),
        matter_id=str(uuid.uuid4()),
        tenant_id="hjmm-prod",
    )
    assert result["status"] == "not_found"


def test_process_document_with_entities_mocked():
    """
    _process_document with mocked AI returns correct entity/relationship counts.
    Writes entities and relationships to the DB.
    """
    from jobs.kg_extraction import _process_document

    matter_id = str(uuid.uuid4())
    tenant_id = "hjmm-prod           "
    doc_id    = str(uuid.uuid4())

    ai_response = json.dumps({
        "entities": [
            {
                "entity_type": "person",
                "canonical_name": "Rebecca Mark",
                "confidence": 0.92,
                "properties": {"role": "CEO", "organization": "Enron International"},
            },
            {
                "entity_type": "organization",
                "canonical_name": "Enron International",
                "confidence": 0.95,
                "properties": {"role": "subsidiary"},
            },
        ],
        "relationships": [
            {
                "entity_a": "Rebecca Mark",
                "entity_b": "Enron International",
                "relationship_type": "employed_by",
                "confidence": 0.9,
            }
        ],
    })

    doc = {
        "id": doc_id,
        "file_name": "complaint.pdf",
        "doc_type": "pleading",
        "custodian": None,
        "doc_date": None,
        "email_subject": None,
        "email_from": None,
    }

    conn = _get_conn()
    try:
        with patch("jobs.kg_extraction._get_ai_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = _make_ai_response(ai_response)
            mock_client_fn.return_value = mock_client

            result = _process_document(doc, matter_id, tenant_id, conn)

        assert result["entities"] == 2
        assert result["relationships"] == 1
        assert result["admissions"] == 0
        assert result["contradictions"] == 0

        # Verify DB
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM kg_entities WHERE matter_id = %s",
                (matter_id,)
            )
            assert cur.fetchone()["cnt"] == 2

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM kg_relationships WHERE matter_id = %s",
                (matter_id,)
            )
            assert cur.fetchone()["cnt"] == 1
    finally:
        conn.close()


def test_process_deposition_detects_admissions_mocked():
    """
    _process_document on a deposition transcript runs admission detection.
    """
    from jobs.kg_extraction import _process_document

    matter_id = str(uuid.uuid4())
    tenant_id = "hjmm-prod           "
    doc_id    = str(uuid.uuid4())

    entity_response = json.dumps({
        "entities": [
            {
                "entity_type": "person",
                "canonical_name": "Andrew Fastow",
                "confidence": 0.95,
                "properties": {"role": "CFO"},
            }
        ],
        "relationships": [],
    })

    admission_response = json.dumps([
        {
            "canonical_name": "Andrew Fastow",
            "admission_text": "I knew the SPEs were designed to hide debt.",
            "adverse_to": "Enron",
            "element_affected": "scienter",
            "confidence": 0.88,
            "properties": {"page": "42", "line": "15", "context": "cross-examination"},
        }
    ])

    doc = {
        "id": doc_id,
        "file_name": "fastow_depo.txt",
        "doc_type": "deposition_transcript",
        "custodian": "Andrew Fastow",
        "doc_date": None,
        "email_subject": None,
        "email_from": None,
    }

    conn = _get_conn()
    try:
        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_ai_response(entity_response)
            elif call_count[0] == 2:
                return _make_ai_response(admission_response)
            else:
                return _make_ai_response("[]")  # no contradictions

        with patch("jobs.kg_extraction._get_ai_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client.messages.create.side_effect = side_effect
            mock_client_fn.return_value = mock_client

            result = _process_document(doc, matter_id, tenant_id, conn)

        assert result["entities"] >= 1
        assert result["admissions"] == 1

        # Verify admission stored as kg_entity
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS cnt FROM kg_entities
                WHERE matter_id = %s AND entity_type = 'admission'
            """, (matter_id,))
            assert cur.fetchone()["cnt"] >= 1
    finally:
        conn.close()


def test_ai_failure_returns_empty_gracefully():
    """When AI call fails, entity extraction returns empty without crashing."""
    from jobs.kg_extraction import _extract_entities_and_relationships

    with patch("jobs.kg_extraction._get_ai_client", side_effect=RuntimeError("no key")):
        entities, relationships = _extract_entities_and_relationships(
            doc_id=str(uuid.uuid4()),
            doc_type="pleading",
            file_name="complaint.pdf",
            custodian=None,
            doc_summary="Some legal document content.",
        )

    assert entities == []
    assert relationships == []
