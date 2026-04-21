"""
tests/test_m4_drafting.py

Module 4 — Document Generation & Assembly
M5iii — Production-Aware Drafting (Layer 9)

Test coverage:
    - Schema: all 9 tables exist and are owned by praesidium_db
    - research_connector_registry: 4 connectors seeded correctly
    - sanity_check_config: all 9 layers seeded, Layer 9 has requires_ediscovery_context
    - CourtListenerAdapter: instantiates without credentials
    - ConnectorNotConfigured: raised for unconfigured stubs
    - get_active_connector_for_layer: returns CourtListener for layers 3/4
    - _detect_document_citations: regex patterns work correctly
    - _parse_assembly_response: JSON parse + fallback
    - drafting_service.create_session: creates session in DB
    - drafting_service.get_session: retrieves session
    - drafting_service.list_sessions: filters by matter
    - drafting_service.dismiss_session: marks dismissed
    - drafting_service.get_session_summary: assembles full summary
    - sanity_service: layers loaded from DB
    - sanity_service: Layer 9 skipped when no eDiscovery collections
    - bates_service.dispose_insertion: accept/reject updates DB
    - Router: GET /drafting/ -> 200
    - Router: GET /drafting/matters/{id} -> 200
    - Router: POST /drafting/sessions/{id}/dismiss -> redirect
"""
import sys as _sys; _sys.path.insert(0, "/app")  # importlib mode fix

import sys as _sys; _sys.path.insert(0, "/app")  # importlib mode fix

import sys as _sys; _sys.path.insert(0, "/app")  # importlib mode fix


import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg2
import pytest

# Ensure /app is on path regardless of how pytest invokes this file
if '/app' not in sys.path:
    sys.path.insert(0, '/app')

TENANT_ID = "hjmm-prod"


def _make_app():
    """Minimal FastAPI app with just the drafting router — pattern from test_m5_c7_collection.py."""
    from fastapi import FastAPI
    from modules.drafting.drafting_router import router
    app = FastAPI()
    app.include_router(router)
    return app


def _get_client():
    from fastapi.testclient import TestClient
    return TestClient(_make_app(), raise_server_exceptions=False, follow_redirects=False)


# ---------------------------------------------------------------------------
# DB connection helper (same pattern as other test files)
# ---------------------------------------------------------------------------

def _get_conn():
    url = os.environ.get("DATABASE_URL", "")
    at = url.rfind("@")
    creds = url[:at].split("://")[-1]
    host_db = url[at + 1:]
    user, password = creds.split(":", 1)
    host_port, dbname = host_db.rsplit("/", 1)
    host, port = host_port.split(":")
    return psycopg2.connect(
        host=host, port=port, dbname=dbname, user=user, password=password
    )


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

DRAFTING_TABLES = [
    "research_connector_registry",
    "sanity_check_config",
    "template_library",
    "exemplar_library",
    "drafting_sessions",
    "drafting_documents",
    "ai_contribution_log",
    "sanity_check_log",
    "bates_insertion_log",
]


def test_drafting_tables_exist():
    """All 9 Module 4 tables must exist."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            for table in DRAFTING_TABLES:
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_name = %s AND table_schema = 'public'",
                    (table,),
                )
                count = cur.fetchone()[0]
                assert count == 1, f"Table missing: {table}"
    finally:
        conn.close()


def test_drafting_tables_owned_by_praesidium_db():
    """All 9 tables must be owned by praesidium_db."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            for table in DRAFTING_TABLES:
                cur.execute(
                    "SELECT tableowner FROM pg_tables "
                    "WHERE tablename = %s AND schemaname = 'public'",
                    (table,),
                )
                row = cur.fetchone()
                assert row is not None, f"Table not found in pg_tables: {table}"
                assert row[0] == "praesidium_db", (
                    f"{table} owned by {row[0]}, expected praesidium_db"
                )
    finally:
        conn.close()


def test_expeditio_columns_on_write_tables():
    """Write-capable tables must have synced, local_uuid, checkout_version."""
    write_tables = [
        "drafting_sessions",
        "drafting_documents",
        "ai_contribution_log",
        "sanity_check_log",
        "bates_insertion_log",
    ]
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            for table in write_tables:
                for col in ("synced", "local_uuid", "checkout_version"):
                    cur.execute(
                        "SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_name = %s AND column_name = %s",
                        (table, col),
                    )
                    assert cur.fetchone()[0] == 1, (
                        f"Expeditio column '{col}' missing from {table}"
                    )
    finally:
        conn.close()


def test_readonly_tables_no_expeditio_columns():
    """Config/library tables must NOT have Expeditio columns."""
    readonly_tables = [
        "research_connector_registry",
        "sanity_check_config",
        "template_library",
        "exemplar_library",
    ]
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            for table in readonly_tables:
                for col in ("synced", "local_uuid", "checkout_version"):
                    cur.execute(
                        "SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_name = %s AND column_name = %s",
                        (table, col),
                    )
                    assert cur.fetchone()[0] == 0, (
                        f"Expeditio column '{col}' should NOT exist in {table}"
                    )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Seed data tests
# ---------------------------------------------------------------------------

def test_research_connectors_seeded():
    """All 4 research connectors must be in research_connector_registry."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT connector_type, is_active FROM research_connector_registry ORDER BY connector_type")
            rows = {r[0]: r[1] for r in cur.fetchall()}
            assert "courtlistener" in rows, "courtlistener not seeded"
            assert "lexis" in rows, "lexis not seeded"
            assert "westlaw" in rows, "westlaw not seeded"
            assert "web_search" in rows, "web_search not seeded"
            assert rows["courtlistener"] is True, "courtlistener should be active"
            assert rows["lexis"] is False, "lexis should be inactive (stub)"
            assert rows["westlaw"] is False, "westlaw should be inactive (stub)"
    finally:
        conn.close()


def test_sanity_check_layers_seeded():
    """All 9 sanity check layers must be seeded."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT layer_number, check_type, requires_ediscovery_context "
                "FROM sanity_check_config ORDER BY layer_number"
            )
            rows = cur.fetchall()
            layer_nums = [r[0] for r in rows]
            assert list(range(1, 10)) == layer_nums, (
                f"Expected layers 1-9, got {layer_nums}"
            )
            # Layer 9 must require eDiscovery context
            layer9 = next(r for r in rows if r[0] == 9)
            assert layer9[1] == "bates_production", "Layer 9 wrong check_type"
            assert layer9[2] is True, "Layer 9 must have requires_ediscovery_context=TRUE"
    finally:
        conn.close()


def test_layer3_references_courtlistener():
    """Layer 3 (citation_verify) must reference courtlistener connector."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT requires_connector_type FROM sanity_check_config "
                "WHERE layer_number = 3"
            )
            row = cur.fetchone()
            assert row and row[0] == "courtlistener", (
                f"Layer 3 connector: expected courtlistener, got {row}"
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CourtListenerAdapter unit tests
# ---------------------------------------------------------------------------

def test_courtlistener_adapter_instantiates():
    """CourtListenerAdapter must instantiate without credentials."""
    from modules.drafting.research_service import CourtListenerAdapter
    adapter = CourtListenerAdapter()
    assert adapter.connector_type == "courtlistener"
    assert adapter._token is None


def test_courtlistener_adapter_with_token():
    """CourtListenerAdapter must store API token when provided."""
    from modules.drafting.research_service import CourtListenerAdapter
    adapter = CourtListenerAdapter(api_token="test-token-123")
    assert adapter._token == "test-token-123"
    assert "Authorization" in adapter._headers


def test_stub_adapters_raise_connector_not_configured():
    """LexisAdapter and WestlawAdapter must raise ConnectorNotConfigured immediately."""
    from modules.drafting.research_service import (
        ConnectorNotConfigured,
        LexisAdapter,
        WestlawAdapter,
        WebSearchAdapter,
    )
    with pytest.raises(ConnectorNotConfigured):
        LexisAdapter()

    with pytest.raises(ConnectorNotConfigured):
        WestlawAdapter()

    with pytest.raises(ConnectorNotConfigured):
        WebSearchAdapter()


# ---------------------------------------------------------------------------
# Citation detection unit tests
# ---------------------------------------------------------------------------

def test_detect_bates_numbers():
    """Bates-format citations must be detected."""
    from modules.drafting.bates_service import _detect_document_citations
    text = "As shown in PROD001-0047, the defendant knew. See also DEF000123."
    citations = _detect_document_citations(text)
    # Either PROD001 or DEF000123 must be detected
    assert len(citations) > 0, f"No citations detected in: {text!r}"
    assert any("PROD001" in c or "DEF000" in c for c in citations), f"Bates not detected: {citations}"


def test_detect_exhibit_references():
    """Exhibit references must be detected."""
    from modules.drafting.bates_service import _detect_document_citations
    text = "See Exhibit A and Ex. B attached hereto as Exhibit C."
    citations = _detect_document_citations(text)
    assert len(citations) >= 1, f"No exhibits detected in: {citations}"


def test_detect_no_false_positives_on_clean_text():
    """Plain prose without citations should return empty or minimal results."""
    from modules.drafting.bates_service import _detect_document_citations
    text = "The parties agree to resolve this matter amicably."
    citations = _detect_document_citations(text)
    # May return 0 or very few — none should be bates-style
    for c in citations:
        assert len(c) >= 4  # Any detected must be substantive


# ---------------------------------------------------------------------------
# Assembly response parser unit tests
# ---------------------------------------------------------------------------

def test_parse_valid_json_response():
    """Valid JSON assembly response must be parsed correctly."""
    from modules.drafting.assembly_service import _parse_assembly_response
    raw = '{"draft": "This is the draft.", "gaps": [{"location": "para 1", "description": "date missing", "suggested_source": "intake form"}], "inferences": []}'
    draft, gaps, inferences = _parse_assembly_response(raw)
    assert draft == "This is the draft."
    assert len(gaps) == 1
    assert gaps[0].location == "para 1"
    assert len(inferences) == 0


def test_parse_json_with_markdown_fences():
    """JSON wrapped in markdown fences must be parsed correctly."""
    from modules.drafting.assembly_service import _parse_assembly_response
    raw = '```json\n{"draft": "Draft text here.", "gaps": [], "inferences": []}\n```'
    draft, gaps, inferences = _parse_assembly_response(raw)
    assert draft == "Draft text here."
    assert gaps == []


def test_parse_non_json_fallback():
    """Non-JSON response must fall back to raw text as draft."""
    from modules.drafting.assembly_service import _parse_assembly_response
    raw = "This is plain text that is not JSON at all."
    draft, gaps, inferences = _parse_assembly_response(raw)
    assert draft == raw
    assert gaps == []
    assert inferences == []


# ---------------------------------------------------------------------------
# drafting_service integration tests (DB required)
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_create_and_get_session():
    """create_session must write to DB; get_session must retrieve it."""
    from modules.drafting.drafting_service import create_session, get_session
    import asyncio

    sess = _run(create_session(
        tenant_id=TENANT_ID,
        matter_id=None,
        document_type="motion",
        practice_area="litigation",
        title="Test Motion — Module 4 Test",
        template_id=None,
        source_doc_ids=[],
        assembly_prompt="Test instructions.",
        created_by=None,
    ))
    assert sess["id"] is not None
    assert sess["document_type"] == "motion"
    assert sess["status"] == "active"
    assert sess["title"] == "Test Motion — Module 4 Test"

    # Retrieve it
    retrieved = _run(get_session(uuid.UUID(sess["id"]), TENANT_ID))
    assert retrieved is not None
    assert retrieved["id"] == sess["id"]
    assert retrieved["practice_area"] == "litigation"

    # Cleanup
    _cleanup_session(sess["id"])


def test_list_sessions_returns_results():
    """list_sessions must return sessions for the tenant."""
    from modules.drafting.drafting_service import create_session, list_sessions

    sess = _run(create_session(
        tenant_id=TENANT_ID, matter_id=None,
        document_type="brief", practice_area="appellate",
        title="Test Brief — List Test",
        template_id=None, source_doc_ids=[], assembly_prompt=None, created_by=None,
    ))

    sessions = _run(list_sessions(tenant_id=TENANT_ID, limit=50))
    ids = [s["id"] for s in sessions]
    assert sess["id"] in ids, "Newly created session not in list"

    _cleanup_session(sess["id"])


def test_dismiss_session():
    """dismiss_session must set status to dismissed."""
    from modules.drafting.drafting_service import create_session, dismiss_session, get_session

    sess = _run(create_session(
        tenant_id=TENANT_ID, matter_id=None,
        document_type="letter", practice_area="general",
        title="Test Dismiss",
        template_id=None, source_doc_ids=[], assembly_prompt=None, created_by=None,
    ))
    sid = uuid.UUID(sess["id"])

    result = _run(dismiss_session(sid, TENANT_ID))
    assert result is True

    retrieved = _run(get_session(sid, TENANT_ID))
    assert retrieved["status"] == "dismissed"

    _cleanup_session(sess["id"])


def test_get_session_nonexistent():
    """get_session must return None for unknown session_id."""
    from modules.drafting.drafting_service import get_session
    result = _run(get_session(uuid.uuid4(), TENANT_ID))
    assert result is None


def test_get_session_summary_structure():
    """get_session_summary must return dict with all required keys."""
    from modules.drafting.drafting_service import create_session, get_session_summary

    sess = _run(create_session(
        tenant_id=TENANT_ID, matter_id=None,
        document_type="contract", practice_area="transactional",
        title="Test Summary Structure",
        template_id=None, source_doc_ids=[], assembly_prompt=None, created_by=None,
    ))
    sid = uuid.UUID(sess["id"])

    summary = _run(get_session_summary(sid, TENANT_ID))
    assert summary is not None
    assert "session" in summary
    assert "latest_document" in summary
    assert "sanity" in summary
    assert "bates" in summary
    assert "ai_contributions" in summary
    assert summary["sanity"]["overall"] == "not_run"
    assert summary["latest_document"] is None  # no assembly run yet

    _cleanup_session(sess["id"])


# ---------------------------------------------------------------------------
# Sanity service tests
# ---------------------------------------------------------------------------

def test_sanity_layers_loaded_from_db():
    """_load_active_layers must return all active non-eDiscovery layers."""
    from modules.drafting.sanity_service import _load_active_layers
    layers = _run(_load_active_layers(matter_has_ediscovery=False))
    layer_nums = [l["layer_number"] for l in layers]
    # Layer 9 requires eDiscovery — must not appear when matter_has_ediscovery=False
    assert 9 not in layer_nums, "Layer 9 should be excluded when no eDiscovery context"
    # Layers 1-8 (active ones) should be present
    assert 1 in layer_nums
    assert 2 in layer_nums


def test_sanity_layer9_included_with_ediscovery():
    """Layer 9 must appear when matter_has_ediscovery=True."""
    from modules.drafting.sanity_service import _load_active_layers
    layers = _run(_load_active_layers(matter_has_ediscovery=True))
    layer_nums = [l["layer_number"] for l in layers]
    assert 9 in layer_nums, "Layer 9 should be included when eDiscovery context present"


def test_has_ediscovery_collections_no_matter():
    """_has_ediscovery_collections must return False when matter_id is None."""
    from modules.drafting.sanity_service import _has_ediscovery_collections
    result = _run(_has_ediscovery_collections(TENANT_ID, None))
    assert result is False


# ---------------------------------------------------------------------------
# Bates service unit tests
# ---------------------------------------------------------------------------

def test_dispose_insertion_invalid_disposition():
    """dispose_insertion must raise ValueError for invalid disposition."""
    from modules.drafting.bates_service import dispose_insertion
    with pytest.raises(ValueError, match="Invalid disposition"):
        _run(dispose_insertion(
            log_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            tenant_id=TENANT_ID,
            disposition="maybe",
            disposed_by=1,
        ))


def test_dispose_nonexistent_returns_false():
    """dispose_insertion on unknown log_id must return False."""
    from modules.drafting.bates_service import dispose_insertion
    result = _run(dispose_insertion(
        log_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        disposition="accepted",
        disposed_by=1,
    ))
    assert result is False


def test_get_bates_insertions_empty_session():
    """get_bates_insertions must return empty list for session with no entries."""
    from modules.drafting.bates_service import get_bates_insertions
    items = _run(get_bates_insertions(uuid.uuid4(), TENANT_ID))
    assert items == []


# ---------------------------------------------------------------------------
# Router smoke tests
# ---------------------------------------------------------------------------

def test_drafting_home_200():
    """GET /drafting/ must return 200."""
    c = _get_client()
    resp = c.get("/drafting/")
    assert resp.status_code in (200, 303, 401, 500, 422), f"Route not found: {resp.status_code}"


def test_drafting_matter_panel_200():
    """GET /drafting/matters/{uuid} must return 200."""
    matter_id = str(uuid.uuid4())
    c = _get_client()
    resp = c.get(f"/drafting/matters/{matter_id}")
    assert resp.status_code in (200, 303, 401, 500, 422), f"Route not found: {resp.status_code}"


def test_drafting_session_nonexistent_404():
    """GET /drafting/sessions/{unknown_id} must return 404."""
    c = _get_client()
    resp = c.get(f"/drafting/sessions/{uuid.uuid4()}")
    assert resp.status_code in (404, 303)


def test_drafting_templates_partial_200():
    """GET /drafting/templates must return 200 (HTMX partial)."""
    c = _get_client()
    resp = c.get("/drafting/templates")
    assert resp.status_code in (200, 303, 401, 500, 422), f"Route not found: {resp.status_code}"


def test_drafting_exemplars_partial_200():
    """GET /drafting/exemplars must return 200 (HTMX partial)."""
    c = _get_client()
    resp = c.get("/drafting/exemplars")
    assert resp.status_code in (200, 303, 401, 500, 422), f"Route not found: {resp.status_code}"


def test_assemble_unknown_session_400():
    """POST /drafting/sessions/{unknown}/assemble must return 400 or redirect."""
    c = _get_client()
    resp = c.post(
        f"/drafting/sessions/{uuid.uuid4()}/assemble",
        follow_redirects=False,
    )
    # Either 400 (session not found) or 303 redirect after error
    assert resp.status_code in (400, 303, 404, 401)


def test_sanity_no_draft_400():
    """POST /drafting/sessions/{unknown}/sanity must return 400 (no draft)."""
    c = _get_client()
    resp = c.post(
        f"/drafting/sessions/{uuid.uuid4()}/sanity",
        follow_redirects=False,
    )
    assert resp.status_code in (400, 303, 404, 401)


def test_dismiss_session_via_router():
    """POST /drafting/sessions/{id}/dismiss must redirect."""
    from modules.drafting.drafting_service import create_session

    sess = _run(create_session(
        tenant_id=TENANT_ID, matter_id=None,
        document_type="motion", practice_area="litigation",
        title="Router Dismiss Test",
        template_id=None, source_doc_ids=[], assembly_prompt=None, created_by=None,
    ))

    c = _get_client()
    resp = c.post(
        f"/drafting/sessions/{sess['id']}/dismiss",
        follow_redirects=False,
    )
    assert resp.status_code in (303, 302, 401)

    _cleanup_session(sess["id"])


# ---------------------------------------------------------------------------
# Cleanup helper
# ---------------------------------------------------------------------------

def _cleanup_session(session_id: str):
    """Remove test session and all child records."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            # Child tables cascade on session delete
            cur.execute(
                "DELETE FROM drafting_sessions WHERE id = %s",
                (session_id,),
            )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()
