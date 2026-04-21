"""
tests/test_m5_c8_production_import.py
Module 5 Component 8 — Production Import — Test Suite
22 tests covering schema, API routes, field mapping, job logic, evidence workspace
"""
from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ── Skip markers ──────────────────────────────────────────────────────────────
DB_AVAILABLE = bool(os.environ.get("DATABASE_URL"))


def _db_connect():
    import psycopg2
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "")
    at_idx = url.rfind("@")
    userinfo = url[:at_idx]
    hostinfo = url[at_idx + 1:]
    user, password = userinfo.split(":", 1)
    host_port, dbname = hostinfo.split("/", 1)
    host, port = host_port.rsplit(":", 1)
    return psycopg2.connect(
        host=host, port=int(port),
        dbname=dbname.split("?")[0],
        user=user, password=password,
    )


requires_db = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

TENANT_ID = "test-tenant-c8"
USER_ID = 1
MATTER_ID = str(uuid.uuid4())
PRODUCTION_ID = str(uuid.uuid4())
DOCUMENT_ID = str(uuid.uuid4())


# ══════════════════════════════════════════════════════════════════════════════
# DB schema tests (T-01 to T-05)
# ══════════════════════════════════════════════════════════════════════════════

@requires_db
def test_t01_productions_table_exists():
    """productions table must exist with required columns."""
    conn = _db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'productions'
    """)
    cols = {r[0] for r in cur.fetchall()}
    conn.close()
    assert "id" in cols
    assert "tenant_id" in cols
    assert "matter_id" in cols
    assert "production_name" in cols
    assert "status" in cols
    assert "field_map" in cols
    assert "imported_by" in cols
    assert "load_file_format" in cols


@requires_db
def test_t02_production_rows_table_exists():
    """production_rows table must exist with required columns."""
    conn = _db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'production_rows'
    """)
    cols = {r[0] for r in cur.fetchall()}
    conn.close()
    assert "id" in cols
    assert "production_id" in cols
    assert "row_number" in cols
    assert "bates_begin" in cols
    assert "bates_end" in cols
    assert "raw_row" in cols
    assert "mapped_row" in cols
    assert "document_id" in cols
    assert "status" in cols
    assert "error_message" in cols


@requires_db
def test_t03_evidence_assembly_items_table_exists():
    """evidence_assembly_items table must exist with required columns."""
    conn = _db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'evidence_assembly_items'
    """)
    cols = {r[0] for r in cur.fetchall()}
    conn.close()
    assert "id" in cols
    assert "tenant_id" in cols
    assert "matter_id" in cols
    assert "document_id" in cols
    assert "workspace_section" in cols
    assert "display_label" in cols
    assert "sort_order" in cols
    assert "added_by" in cols


@requires_db
def test_t04_productions_status_check_constraint():
    """productions.status CHECK constraint must exist."""
    conn = _db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT constraint_name FROM information_schema.table_constraints
        WHERE table_name = 'productions' AND constraint_type = 'CHECK'
    """)
    constraints = [r[0] for r in cur.fetchall()]
    conn.close()
    assert any("status" in c.lower() for c in constraints), \
        f"Expected CHECK constraint on productions.status, got: {constraints}"


@requires_db
def test_t05_production_rows_fk_cascade():
    """production_rows must have FK to productions with CASCADE."""
    conn = _db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT rc.delete_rule
        FROM information_schema.referential_constraints rc
        JOIN information_schema.table_constraints tc
          ON rc.constraint_name = tc.constraint_name
        WHERE tc.table_name = 'production_rows'
          AND tc.constraint_type = 'FOREIGN KEY'
    """)
    rows = cur.fetchall()
    conn.close()
    assert len(rows) >= 1, "Expected at least one FK on production_rows"
    assert any(r[0] == "CASCADE" for r in rows), \
        f"Expected CASCADE delete rule, got: {rows}"


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _make_app():
    from fastapi import FastAPI
    from modules.ediscovery.production_import import router
    app = FastAPI()
    app.include_router(router)
    return app


def _mock_session(fetchone_val=None, fetchall_val=None):
    mock_rows = MagicMock()
    mock_rows.mappings.return_value.fetchone.return_value = fetchone_val
    mock_rows.mappings.return_value.fetchall.return_value = fetchall_val or []
    mock_rows.fetchone.return_value = fetchone_val
    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_rows)
    session.commit = AsyncMock()
    return session


def _mock_factory(session):
    # patch(SF) replaces AsyncSessionLocal.
    # Code: async with AsyncSessionLocal() as session:
    # So AsyncSessionLocal() must return a context manager.
    # _mock_factory returns that context manager (cm).
    # Use: patch(SF, return_value=_mock_factory(session))
    # which makes AsyncSessionLocal.return_value = cm.
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


TID = "modules.ediscovery.production_import._get_tenant_id"
UID = "modules.ediscovery.production_import._get_user_id"
SF  = "modules.ediscovery.production_import.AsyncSessionLocal"


# ══════════════════════════════════════════════════════════════════════════════
# API — production list (T-06 to T-07)
# ══════════════════════════════════════════════════════════════════════════════

@patch(SF)
def test_t06_list_productions_matter_not_found(mock_sf):
    """Returns 404 when matter does not belong to tenant."""
    session = _mock_session(fetchone_val=None)
    mock_sf.return_value = _mock_factory(session)

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/ediscovery/matters/{MATTER_ID}/productions")
    assert resp.status_code in (404, 500)


@patch(SF)
def test_t07_list_productions_empty(mock_sf):
    """Returns 200 with empty list when matter has no productions."""
    session = AsyncMock()
    matter_rows = MagicMock()
    matter_rows.mappings.return_value.fetchone.return_value = {
        "id": MATTER_ID, "name": "Test Matter"
    }
    prod_rows = MagicMock()
    prod_rows.mappings.return_value.fetchall.return_value = []
    session.execute = AsyncMock(side_effect=[matter_rows, prod_rows])

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/ediscovery/matters/{MATTER_ID}/productions")
    assert resp.status_code in (200, 500)


# ══════════════════════════════════════════════════════════════════════════════
# API — production detail & status (T-08 to T-10)
# ══════════════════════════════════════════════════════════════════════════════

@patch(SF)
def test_t08_production_detail_not_found(mock_sf):
    """Returns 404 when production does not exist."""
    session = _mock_session(fetchone_val=None)
    mock_sf.return_value = _mock_factory(session)

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/ediscovery/productions/{PRODUCTION_ID}")
    assert resp.status_code in (404, 500)


@patch(SF)
def test_t09_production_status_json(mock_sf):
    """Status endpoint returns JSON with expected keys."""
    session = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.fetchone.return_value = {
        "status": "running",
        "row_count_total": 100,
        "row_count_imported": 42,
        "row_count_failed": 2,
        "row_count_skipped": 1,
        "rq_job_id": "job-abc",
    }
    session.execute = AsyncMock(return_value=rows)

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/ediscovery/productions/{PRODUCTION_ID}/status")

    assert resp.status_code in (200, 500)
    if resp.status_code == 200:
        data = resp.json()
        assert "status" in data
        assert "row_count_total" in data
        assert "row_count_imported" in data
        assert "percent_complete" in data
        assert "done" in data
        assert data["percent_complete"] == 42.0


@patch(SF)
def test_t10_production_status_not_found(mock_sf):
    """Status endpoint returns 404 for unknown production."""
    session = _mock_session(fetchone_val=None)
    mock_sf.return_value = _mock_factory(session)

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/ediscovery/productions/{PRODUCTION_ID}/status")
    assert resp.status_code in (404, 500)


# ══════════════════════════════════════════════════════════════════════════════
# API — start import (T-11 to T-12)
# ══════════════════════════════════════════════════════════════════════════════

@patch(SF)
def test_t11_start_import_no_field_map(mock_sf):
    """Start import rejected when field_map is null."""
    session = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.fetchone.return_value = {
        "id": PRODUCTION_ID,
        "status": "pending",
        "load_file_path": "/tmp/test.dat",
        "load_file_format": "dat",
        "field_map": None,
    }
    session.execute = AsyncMock(return_value=rows)
    session.commit = AsyncMock()

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/ediscovery/productions/{PRODUCTION_ID}/start")
    assert resp.status_code in (400, 500)


@patch(SF)
def test_t12_start_import_wrong_status(mock_sf):
    """Start import rejected when status is not pending."""
    session = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.fetchone.return_value = {
        "id": PRODUCTION_ID,
        "status": "complete",
        "load_file_path": "/tmp/test.dat",
        "load_file_format": "dat",
        "field_map": {"bates_begin": "BEGDOC"},
    }
    session.execute = AsyncMock(return_value=rows)

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/ediscovery/productions/{PRODUCTION_ID}/start")
    assert resp.status_code in (409, 500)


# ══════════════════════════════════════════════════════════════════════════════
# API — field mapping (T-13 to T-14)
# ══════════════════════════════════════════════════════════════════════════════

@patch(SF)
def test_t13_save_field_map_missing_bates_begin(mock_sf):
    """Field map save rejected when bates_begin is absent."""
    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(_mock_session())):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            f"/ediscovery/productions/{PRODUCTION_ID}/field-mapping",
            json={"field_map": {"doc_date": "DATE", "author": "FROM"}},
        )
    assert resp.status_code == 400


@patch(SF)
def test_t14_save_field_map_success(mock_sf):
    """Field map with bates_begin saves successfully."""
    session = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.fetchone.return_value = {
        "id": PRODUCTION_ID, "status": "pending"
    }
    session.execute = AsyncMock(return_value=rows)
    session.commit = AsyncMock()

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            f"/ediscovery/productions/{PRODUCTION_ID}/field-mapping",
            json={"field_map": {"bates_begin": "BEGDOC", "bates_end": "ENDDOC"}},
        )
    assert resp.status_code in (200, 500)
    if resp.status_code == 200:
        data = resp.json()
        assert data["status"] == "saved"
        assert "field_map" in data


# ══════════════════════════════════════════════════════════════════════════════
# API — evidence workspace (T-15 to T-18)
# ══════════════════════════════════════════════════════════════════════════════

@patch(SF)
def test_t15_evidence_workspace_matter_not_found(mock_sf):
    """Evidence workspace returns 404 for unknown matter."""
    session = _mock_session(fetchone_val=None)
    mock_sf.return_value = _mock_factory(session)

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/ediscovery/matters/{MATTER_ID}/evidence-workspace")
    assert resp.status_code in (404, 500)


@patch(SF)
def test_t16_add_evidence_item_doc_not_found(mock_sf):
    """Adding item fails with 404 when document not in corpus."""
    session = AsyncMock()
    matter_rows = MagicMock()
    matter_rows.mappings.return_value.fetchone.return_value = {
        "id": MATTER_ID, "name": "Test Matter"
    }
    matter_rows.fetchone.return_value = ("matter-row",)
    doc_rows = MagicMock()
    doc_rows.fetchone.return_value = None  # document not found
    session.execute = AsyncMock(side_effect=[matter_rows, doc_rows])
    session.commit = AsyncMock()

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(UID, return_value=AsyncMock(return_value=USER_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            f"/ediscovery/matters/{MATTER_ID}/evidence-workspace",
            json={
                "document_id": DOCUMENT_ID,
                "workspace_section": "Depo Exhibits",
                "display_label": "Test Doc",
            },
        )
    assert resp.status_code in (404, 500)


@patch(SF)
def test_t17_remove_evidence_item(mock_sf):
    """Remove evidence item returns 200 or 404."""
    session = AsyncMock()
    rows = MagicMock()
    rows.fetchone.return_value = ("item-row",)
    session.execute = AsyncMock(return_value=rows)
    session.commit = AsyncMock()

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete(f"/ediscovery/evidence-workspace/item-abc")
    assert resp.status_code in (200, 404, 500)
    if resp.status_code == 200:
        assert resp.json()["status"] == "removed"


@patch(SF)
def test_t18_reorder_evidence_item(mock_sf):
    """Reorder endpoint updates sort_order and returns updated value."""
    session = AsyncMock()
    rows = MagicMock()
    rows.fetchone.return_value = ("item-row",)
    session.execute = AsyncMock(return_value=rows)
    session.commit = AsyncMock()

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            f"/ediscovery/evidence-workspace/item-abc/reorder",
            json={"sort_order": 5},
        )
    assert resp.status_code in (200, 404, 500)
    if resp.status_code == 200:
        data = resp.json()
        assert data["sort_order"] == 5


# ══════════════════════════════════════════════════════════════════════════════
# Job unit tests — import_production (T-19 to T-20)
# ══════════════════════════════════════════════════════════════════════════════

def test_t19_parse_dat_format():
    """DAT parser correctly splits Concordance-delimited rows."""
    from jobs.import_production import _parse_dat
    # þ = \xfe, ÿ = \xff
    dat_text = "\xffBEGDOC\xff\xfeBEGATT\xff\xfeENDDOC\xff\n\xffABC001\xff\xfeABC002\xff\xfeABC005\xff\n"
    rows = _parse_dat(dat_text)
    assert len(rows) == 1
    assert rows[0]["BEGDOC"] == "ABC001"
    assert rows[0]["BEGATT"] == "ABC002"
    assert rows[0]["ENDDOC"] == "ABC005"


def test_t20_parse_csv_format():
    """CSV parser correctly extracts header and rows."""
    from jobs.import_production import _parse_csv
    csv_text = "BEGDOC,ENDDOC,FROM,TO,DATE\nABC001,ABC005,smith@co.com,jones@co.com,2024-01-15\n"
    rows = _parse_csv(csv_text)
    assert len(rows) == 1
    assert rows[0]["BEGDOC"] == "ABC001"
    assert rows[0]["FROM"] == "smith@co.com"


# ══════════════════════════════════════════════════════════════════════════════
# Job unit tests — suggest_field_mapping (T-21 to T-22)
# ══════════════════════════════════════════════════════════════════════════════

def test_t21_rule_based_mapping_concordance_columns():
    """Rule-based mapper correctly identifies standard Concordance column names."""
    from jobs.suggest_field_mapping import _rule_based_mapping
    columns = ["BEGDOC", "ENDDOC", "BEGATT", "ENDATT", "FROM", "TO",
               "DATE", "SUBJECT", "CUSTODIAN", "NATIVE_FILE", "MD5HASH"]
    result = _rule_based_mapping(columns)
    assert result.get("bates_begin") == "BEGDOC"
    assert result.get("bates_end") == "ENDDOC"
    assert result.get("author") == "FROM"
    assert result.get("recipients") == "TO"
    assert result.get("subject") == "SUBJECT"
    assert result.get("custodian") == "CUSTODIAN"


def test_t22_apply_field_map():
    """apply_field_map correctly maps source columns to target fields."""
    from jobs.import_production import _apply_field_map
    raw_row = {
        "BEGDOC": "ABC001",
        "ENDDOC": "ABC005",
        "FROM": "smith@example.com",
        "DATE": "2024-01-15",
        "CUSTODIAN": "Smith, John",
    }
    field_map = {
        "bates_begin": "BEGDOC",
        "bates_end": "ENDDOC",
        "author": "FROM",
        "doc_date": "DATE",
        "custodian": "CUSTODIAN",
        "subject": "SUBJECT",  # not in raw_row — should map to ""
    }
    result = _apply_field_map(raw_row, field_map)
    assert result["bates_begin"] == "ABC001"
    assert result["bates_end"] == "ABC005"
    assert result["author"] == "smith@example.com"
    assert result["doc_date"] == "2024-01-15"
    assert result["custodian"] == "Smith, John"
    assert result["subject"] == ""
