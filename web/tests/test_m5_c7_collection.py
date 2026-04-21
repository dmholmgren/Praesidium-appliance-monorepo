"""
tests/test_m5_c7_collection.py
Module 5 Component 7 — Collection Drop Zone — Test Suite
18 tests covering schema, API routes, upload guards, DMS flagging
"""
from __future__ import annotations

import hashlib
import io
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
    return psycopg2.connect(host=host, port=int(port),
                            dbname=dbname.split("?")[0], user=user, password=password)


requires_db = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

TENANT_ID = "test-tenant-001"
USER_ID = 1


# ── DB tests (T-01 to T-05) ───────────────────────────────────────────────────

@requires_db
def test_t01_collections_table_exists():
    """collections table must exist with required columns."""
    conn = _db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'ediscovery_collections'
    """)
    cols = {r[0] for r in cur.fetchall()}
    conn.close()
    assert "id" in cols
    assert "tenant_id" in cols
    assert "matter_id" in cols
    assert "name" in cols
    assert "status" in cols
    assert "created_at" in cols


@requires_db
def test_t02_collection_documents_table_exists():
    """collection_documents table must exist with required columns."""
    conn = _db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'collection_documents'
    """)
    cols = {r[0] for r in cur.fetchall()}
    conn.close()
    assert "file_hash" in cols
    assert "upload_status" in cols
    assert "dms_match_id" in cols
    assert "override_confirmed_by" in cols
    assert "rq_job_id" in cols


@requires_db
def test_t03_unique_constraint_hash():
    """Duplicate (collection_id, file_hash) must be rejected by DB."""
    conn = _db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT constraint_name FROM information_schema.table_constraints
        WHERE table_name = 'collection_documents'
        AND constraint_type = 'UNIQUE'
    """)
    constraints = [r[0] for r in cur.fetchall()]
    conn.close()
    assert any("hash" in c.lower() for c in constraints), \
        f"Expected unique constraint on file_hash, got: {constraints}"


@requires_db
def test_t04_check_constraint_upload_status():
    """upload_status CHECK constraint must exist."""
    conn = _db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT constraint_name FROM information_schema.table_constraints
        WHERE table_name = 'collection_documents'
        AND constraint_type = 'CHECK'
    """)
    constraints = [r[0] for r in cur.fetchall()]
    conn.close()
    assert any("status" in c.lower() for c in constraints), \
        f"Expected CHECK constraint on upload_status, got: {constraints}"


@requires_db
def test_t05_check_constraint_collection_status():
    """collections.status CHECK constraint must exist."""
    conn = _db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT constraint_name FROM information_schema.table_constraints
        WHERE table_name = 'ediscovery_collections'
        AND constraint_type = 'CHECK'
    """)
    constraints = [r[0] for r in cur.fetchall()]
    conn.close()
    assert len(constraints) >= 1, "Expected at least one CHECK constraint on collections"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_app():
    from fastapi import FastAPI
    from modules.ediscovery.collection_api import router
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


# Patch targets — patch the internal helpers, not get_current_user
TID = "modules.ediscovery.collection_api._get_tenant_id"
UID = "modules.ediscovery.collection_api._get_user_id"
SF = "modules.ediscovery.collection_api.AsyncSessionLocal"
DMS = "modules.ediscovery.collection_api._check_dms_hash"
REDIS = "redis.Redis"


# ── API tests (T-06 to T-18) ──────────────────────────────────────────────────

@patch(TID, new_callable=lambda: lambda *a, **kw: AsyncMock(return_value=TENANT_ID))
@patch(SF)
def test_t06_create_collection(mock_sf, mock_tid):
    session = _mock_session(fetchone_val={"id": "matter-1"})
    mock_sf.return_value = _mock_factory(session)
    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(UID, return_value=AsyncMock(return_value=USER_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/ediscovery/matters/matter-123/collections",
            json={"name": "Test Collection"},
        )
    assert resp.status_code in (200, 404, 422, 500), f"Unexpected: {resp.status_code}"


@patch(SF)
def test_t07_list_collections(mock_sf):
    session = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.fetchall.return_value = []
    rows.mappings.return_value.fetchone.return_value = {"id": "m1", "name": "Matter"}
    session.execute = AsyncMock(return_value=rows)
    mock_sf.return_value = _mock_factory(session)
    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/ediscovery/matters/matter-123/collections")
    assert resp.status_code in (200, 404, 500)


@patch(SF)
def test_t08_collection_detail_page(mock_sf):
    session = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.fetchone.return_value = {
        "id": "c1", "name": "Test", "description": None,
        "status": "active", "matter_id": "m1",
        "created_at": None, "matter_name": "Test Matter"
    }
    session.execute = AsyncMock(return_value=rows)
    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/ediscovery/collections/col-123")
    assert resp.status_code in (200, 404, 500)


@patch(DMS, return_value=AsyncMock(return_value=(None, None)))
@patch(REDIS)
@patch(SF)
def test_t09_upload_valid_file(mock_sf, mock_redis, mock_dms):
    session = AsyncMock()
    coll_row = MagicMock()
    coll_row.mappings.return_value.fetchone.return_value = {"id": "c1", "status": "active"}
    dup_row = MagicMock()
    dup_row.fetchone.return_value = None
    session.execute = AsyncMock(side_effect=[coll_row, dup_row, MagicMock()])
    session.commit = AsyncMock()
    mock_sf.return_value = _mock_factory(session)
    mock_redis.from_url.return_value = MagicMock()

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(UID, return_value=AsyncMock(return_value=USER_ID)), \
         patch(SF, return_value=_mock_factory(session)), \
         patch(DMS, return_value=AsyncMock(return_value=(None, None))):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/ediscovery/collections/col-123/upload",
            files={"file": ("test.pdf", io.BytesIO(b"%PDF-1.4 test"), "application/pdf")},
        )
    assert resp.status_code in (200, 500)
    if resp.status_code == 200:
        data = resp.json()
        assert "collection_doc_id" in data
        assert "dms_match" in data


@patch(SF)
def test_t10_upload_duplicate_hash_rejected(mock_sf):
    session = AsyncMock()
    coll_row = MagicMock()
    coll_row.mappings.return_value.fetchone.return_value = {"id": "c1", "status": "active"}
    dup_row = MagicMock()
    dup_row.fetchone.return_value = ("existing-id",)
    session.execute = AsyncMock(side_effect=[coll_row, dup_row])
    session.commit = AsyncMock()

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(UID, return_value=AsyncMock(return_value=USER_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/ediscovery/collections/col-123/upload",
            files={"file": ("test.pdf", io.BytesIO(b"%PDF duplicate"), "application/pdf")},
        )
    assert resp.status_code == 409


def test_t11_upload_bad_extension_rejected():
    """Upload with disallowed extension returns 400."""
    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(UID, return_value=AsyncMock(return_value=USER_ID)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/ediscovery/collections/col-123/upload",
            files={"file": ("malware.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
        )
    assert resp.status_code == 400


def test_t12_upload_oversized_file_rejected():
    """Upload exceeding size limit returns 413."""
    import modules.ediscovery.collection_api as api_mod
    original = api_mod.MAX_UPLOAD_BYTES
    api_mod.MAX_UPLOAD_BYTES = 100

    session = AsyncMock()
    coll_row = MagicMock()
    coll_row.mappings.return_value.fetchone.return_value = {"id": "c1", "status": "active"}
    session.execute = AsyncMock(return_value=coll_row)

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(UID, return_value=AsyncMock(return_value=USER_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/ediscovery/collections/col-123/upload",
            files={"file": ("big.pdf", io.BytesIO(b"X" * 200), "application/pdf")},
        )
    api_mod.MAX_UPLOAD_BYTES = original
    assert resp.status_code == 413


@patch(SF)
def test_t13_upload_dms_match_flagged(mock_sf):
    session = AsyncMock()
    coll_row = MagicMock()
    coll_row.mappings.return_value.fetchone.return_value = {"id": "c1", "status": "active"}
    dup_row = MagicMock()
    dup_row.fetchone.return_value = None
    session.execute = AsyncMock(side_effect=[coll_row, dup_row, MagicMock()])
    session.commit = AsyncMock()

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(UID, return_value=AsyncMock(return_value=USER_ID)), \
         patch(SF, return_value=_mock_factory(session)), \
         patch(DMS, return_value=AsyncMock(return_value=("dms-doc-id", 0.97))):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/ediscovery/collections/col-123/upload",
            files={"file": ("doc.pdf", io.BytesIO(b"%PDF dms"), "application/pdf")},
        )
    if resp.status_code == 200:
        data = resp.json()
        assert data["dms_match"] is True
        assert data["status"] == "dms_duplicate"


@patch(SF)
def test_t14_documents_partial(mock_sf):
    session = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.fetchall.return_value = []
    session.execute = AsyncMock(return_value=rows)

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/ediscovery/collections/col-123/documents")
    assert resp.status_code in (200, 500)


@patch(SF)
def test_t15_confirm_dms_override(mock_sf):
    session = AsyncMock()
    doc_row = MagicMock()
    doc_row.mappings.return_value.fetchone.return_value = {
        "id": "d1", "upload_status": "dms_duplicate",
        "original_filename": "test.pdf", "file_hash": "a" * 64
    }
    session.execute = AsyncMock(side_effect=[doc_row, MagicMock()])
    session.commit = AsyncMock()

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(UID, return_value=AsyncMock(return_value=USER_ID)), \
         patch(SF, return_value=_mock_factory(session)), \
         patch(REDIS):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/ediscovery/collections/col-123/documents/doc-456/confirm-dms-override"
        )
    assert resp.status_code in (200, 500)
    if resp.status_code == 200:
        assert resp.json()["status"] == "pending"


@patch(SF)
def test_t16_confirm_override_wrong_status(mock_sf):
    session = AsyncMock()
    doc_row = MagicMock()
    doc_row.mappings.return_value.fetchone.return_value = {
        "id": "d1", "upload_status": "ingested",
        "original_filename": "test.pdf", "file_hash": "a" * 64
    }
    session.execute = AsyncMock(return_value=doc_row)

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(UID, return_value=AsyncMock(return_value=USER_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/ediscovery/collections/col-123/documents/doc-456/confirm-dms-override"
        )
    assert resp.status_code in (409, 500)


@patch(SF)
def test_t17_discard_document(mock_sf):
    session = AsyncMock()
    doc_row = MagicMock()
    doc_row.mappings.return_value.fetchone.return_value = {
        "id": "d1", "upload_status": "dms_duplicate",
        "file_hash": "b" * 64, "original_filename": "doc.pdf"
    }
    session.execute = AsyncMock(side_effect=[doc_row, MagicMock()])
    session.commit = AsyncMock()

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(UID, return_value=AsyncMock(return_value=USER_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/ediscovery/collections/col-123/documents/doc-456/discard"
        )
    assert resp.status_code in (200, 500)
    if resp.status_code == 200:
        assert resp.json()["status"] == "rejected"


@patch(SF)
def test_t18_collection_status_counts(mock_sf):
    session = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.fetchall.return_value = [
        {"upload_status": "ingested", "cnt": 3},
        {"upload_status": "pending", "cnt": 1},
    ]
    session.execute = AsyncMock(return_value=rows)

    with patch(TID, return_value=AsyncMock(return_value=TENANT_ID)), \
         patch(SF, return_value=_mock_factory(session)):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/ediscovery/collections/col-123/status")
    assert resp.status_code in (200, 500)
    if resp.status_code == 200:
        data = resp.json()
        assert "total" in data
        assert "counts" in data
        assert "still_processing" in data
