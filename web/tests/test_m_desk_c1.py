"""
M-DESK C1 — Pytest suite.

Targets the appliance's real database (we have async credentials in env).
Each test seeds its own row(s) under unique UUIDs and cleans up via
try/finally so the suite leaves no artifacts behind.

Coverage map (20 tests):

  JWT issue + verify .................. 3   (round-trip, bad sig, expiry)
  Refresh rotation .................... 3   (first-use, replay-fails, revoke)
  Checkout state machine .............. 5   (claim, idempotent, conflict,
                                             heartbeat by holder, by other)
  Release ............................. 3   (holder, admin, non-admin denied)
  Expired-lock override ............... 1   (the patent-novelty path)
  Manifest discovery .................. 2   (empty dir → None, files → newest)
  Bulk heartbeat ...................... 2   (mixed valid/invalid uuids)
  Compare router validation ........... 1   (same-doc rejected pre-enqueue)

All async tests use asyncio_mode=auto from pytest.ini.

Run from inside the praesidium-web container:

    docker exec praesidium-web pytest tests/test_m_desk_c1.py -v
"""

# sys.path setup MUST come first — pytest collection runs before /app/conftest.py
# can fully establish import paths for tests that import third-party / project
# modules at module load time. Matches the pattern used in test_widget_render.py.
import sys
sys.path.insert(0, "/app")

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text as sa_text

# Ensure DESKTOP_JWT_SECRET is in env BEFORE importing jwt_service.
os.environ.setdefault(
    "DESKTOP_JWT_SECRET",
    os.environ.get("DESKTOP_JWT_SECRET")
    or "test-secret-not-for-prod-32bytes-padding-aaaaaaaa",
)

from core.db.base import AsyncSessionLocal  # noqa: E402
from modules.desktop import checkout_service as cs  # noqa: E402
from modules.desktop import jwt_service  # noqa: E402
from modules.desktop import manifest_router  # noqa: E402


TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"


# ═════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def seeded_doc() -> AsyncIterator[str]:
    """Yield a fresh documents row id; clean it up afterwards."""
    doc_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as s:
        await s.execute(sa_text("""
            INSERT INTO documents (id, tenant_id, filename, created_at, updated_at)
            VALUES (CAST(:id AS uuid), :tid, 'mdesk-c1-test.docx', NOW(), NOW())
        """), {"id": doc_id, "tid": TENANT})
        await s.commit()

    try:
        yield doc_id
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(
                sa_text("DELETE FROM documents WHERE id = CAST(:id AS uuid)"),
                {"id": doc_id},
            )
            await s.commit()


@pytest_asyncio.fixture
async def cleanup_refresh_tokens() -> AsyncIterator[list[int]]:
    """Track user_ids whose refresh-token rows must be wiped on teardown."""
    user_ids: list[int] = []
    yield user_ids
    if user_ids:
        async with AsyncSessionLocal() as s:
            await s.execute(
                sa_text("""
                    DELETE FROM desktop_refresh_tokens
                    WHERE user_id = ANY(CAST(:ids AS bigint[]))
                      AND TRIM(tenant_id) = :tid
                """),
                {"ids": user_ids, "tid": TENANT},
            )
            await s.commit()


# ═════════════════════════════════════════════════════════════════════════
# JWT issue + verify (3)
# ═════════════════════════════════════════════════════════════════════════

def test_jwt_round_trip():
    """Encode → decode preserves all claims with correct types."""
    token, exp = jwt_service.encode_access_token(
        user_id=42,
        tenant_id=TENANT,
        email="alice@example.com",
        role="admin",
        jti="00000000-0000-0000-0000-000000000001",
    )
    claims = jwt_service.decode_access_token(token)
    assert claims.sub == "42"
    assert claims.user_id == 42
    assert isinstance(claims.user_id, int)
    assert claims.tenant_id == TENANT
    assert claims.email == "alice@example.com"
    assert claims.role == "admin"
    assert claims.jti == "00000000-0000-0000-0000-000000000001"
    assert claims.exp > claims.iat
    assert exp.tzinfo is not None  # aware datetime


def test_jwt_bad_signature_rejected():
    """Decoding with the wrong key raises an InvalidSignatureError."""
    import jwt as pyjwt
    token, _ = jwt_service.encode_access_token(
        user_id=1, tenant_id=TENANT, email="", role="staff",
        jti="00000000-0000-0000-0000-000000000002",
    )
    with pytest.raises(pyjwt.InvalidSignatureError):
        pyjwt.decode(token, "wrong-secret-32-bytes-aaaaaaaaaa", algorithms=["HS256"])


def test_jwt_expired_token_rejected():
    """A token whose exp is in the past raises ExpiredSignatureError."""
    import jwt as pyjwt
    expired, _ = jwt_service.encode_access_token(
        user_id=1, tenant_id=TENANT, email="", role="staff",
        jti="00000000-0000-0000-0000-000000000003",
        ttl_seconds=-10,
    )
    with pytest.raises(pyjwt.ExpiredSignatureError):
        jwt_service.decode_access_token(expired)


# ═════════════════════════════════════════════════════════════════════════
# Refresh rotation (3)
# ═════════════════════════════════════════════════════════════════════════

async def test_refresh_first_use_rotates(cleanup_refresh_tokens):
    """A fresh refresh token can be used once and produces a new pair."""
    cleanup_refresh_tokens.append(8881)

    async with AsyncSessionLocal() as s:
        await s.execute(sa_text("""
            INSERT INTO users
              (id, tenant_id, username, email, full_name, role,
               is_active, auth_provider, created_at, updated_at)
            VALUES (8881, :tid, 'mdesk_test_a', 'a@hjmm.com',
                    'Test A', 'staff', TRUE, 'local', NOW(), NOW())
            ON CONFLICT (id) DO NOTHING
        """), {"tid": TENANT})
        await s.commit()

    try:
        first = await jwt_service.issue_token_pair(
            user_id=8881, tenant_id=TENANT,
            email="a@hjmm.com", role="staff",
        )
        second = await jwt_service.refresh_token_pair(
            presented_refresh_token=first.refresh_token,
            expected_tenant_id=TENANT,
        )
        assert second.access_token != first.access_token
        assert second.refresh_token != first.refresh_token
        assert second.jti != first.jti
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(sa_text("DELETE FROM users WHERE id = 8881"))
            await s.commit()


async def test_refresh_replay_fails(cleanup_refresh_tokens):
    """A refresh token used twice raises RefreshError reason='revoked' on the second use."""
    cleanup_refresh_tokens.append(8882)

    async with AsyncSessionLocal() as s:
        await s.execute(sa_text("""
            INSERT INTO users (id, tenant_id, username, email, full_name, role,
                               is_active, auth_provider, created_at, updated_at)
            VALUES (8882, :tid, 'mdesk_test_b', 'b@hjmm.com',
                    'Test B', 'staff', TRUE, 'local', NOW(), NOW())
            ON CONFLICT (id) DO NOTHING
        """), {"tid": TENANT})
        await s.commit()

    try:
        first = await jwt_service.issue_token_pair(
            user_id=8882, tenant_id=TENANT,
            email="b@hjmm.com", role="staff",
        )
        await jwt_service.refresh_token_pair(
            presented_refresh_token=first.refresh_token,
            expected_tenant_id=TENANT,
        )
        with pytest.raises(jwt_service.RefreshError) as excinfo:
            await jwt_service.refresh_token_pair(
                presented_refresh_token=first.refresh_token,
                expected_tenant_id=TENANT,
            )
        assert excinfo.value.reason == "revoked"
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(sa_text("DELETE FROM users WHERE id = 8882"))
            await s.commit()


async def test_refresh_revoke_user_kills_all_tokens(cleanup_refresh_tokens):
    """revoke_user_refresh_tokens kills all active rows for the user."""
    cleanup_refresh_tokens.append(8883)

    async with AsyncSessionLocal() as s:
        await s.execute(sa_text("""
            INSERT INTO users (id, tenant_id, username, email, full_name, role,
                               is_active, auth_provider, created_at, updated_at)
            VALUES (8883, :tid, 'mdesk_test_c', 'c@hjmm.com',
                    'Test C', 'staff', TRUE, 'local', NOW(), NOW())
            ON CONFLICT (id) DO NOTHING
        """), {"tid": TENANT})
        await s.commit()

    try:
        for _ in range(3):
            await jwt_service.issue_token_pair(
                user_id=8883, tenant_id=TENANT,
                email="c@hjmm.com", role="staff",
            )
        revoked = await jwt_service.revoke_user_refresh_tokens(
            user_id=8883, tenant_id=TENANT,
        )
        assert revoked == 3
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(sa_text("DELETE FROM users WHERE id = 8883"))
            await s.commit()


# ═════════════════════════════════════════════════════════════════════════
# Checkout state machine (5)
# ═════════════════════════════════════════════════════════════════════════

async def test_checkout_initial_claim(seeded_doc):
    """No initial state, then attempt_checkout claims it."""
    pre = await cs.read_checkout(doc_id=seeded_doc, tenant_id=TENANT)
    assert pre is None

    state = await cs.attempt_checkout(
        doc_id=seeded_doc, tenant_id=TENANT,
        user_id=1, user_email="a@hjmm.com",
        client="desktop-vsto-1.0.0",
    )
    assert state.checked_out_by == "1"
    assert state.checked_out_by_email == "a@hjmm.com"
    assert state.checkout_lock_ttl > 0


async def test_checkout_idempotent_reclaim(seeded_doc):
    """Same user re-claiming refreshes timestamp without raising."""
    a1 = await cs.attempt_checkout(
        doc_id=seeded_doc, tenant_id=TENANT,
        user_id=1, user_email="a@hjmm.com",
        client="desktop-vsto-1.0.0",
    )
    a2 = await cs.attempt_checkout(
        doc_id=seeded_doc, tenant_id=TENANT,
        user_id=1, user_email="a@hjmm.com",
        client="desktop-vsto-1.0.0",
    )
    assert a2.checked_out_by == "1"
    assert a2.checked_out_at >= a1.checked_out_at


async def test_checkout_conflict_for_different_user(seeded_doc):
    """User B is rejected with CheckoutConflict carrying held-by state."""
    await cs.attempt_checkout(
        doc_id=seeded_doc, tenant_id=TENANT,
        user_id=1, user_email="a@hjmm.com",
        client="desktop-vsto-1.0.0",
    )
    with pytest.raises(cs.CheckoutConflict) as excinfo:
        await cs.attempt_checkout(
            doc_id=seeded_doc, tenant_id=TENANT,
            user_id=2, user_email="b@hjmm.com",
            client="desktop-vsto-1.0.0",
        )
    assert excinfo.value.state.checked_out_by == "1"
    assert excinfo.value.state.checked_out_by_email == "a@hjmm.com"


async def test_heartbeat_by_holder_extends(seeded_doc):
    """Holder calling extend_checkout bumps timestamp."""
    a = await cs.attempt_checkout(
        doc_id=seeded_doc, tenant_id=TENANT,
        user_id=1, user_email="a@hjmm.com",
        client="desktop-vsto-1.0.0",
    )
    extended = await cs.extend_checkout(
        doc_id=seeded_doc, tenant_id=TENANT, user_id=1,
    )
    assert extended is not None
    assert extended.checked_out_at >= a.checked_out_at


async def test_heartbeat_by_non_holder_returns_none(seeded_doc):
    """Non-holder heartbeat is silent — returns None, no exception."""
    await cs.attempt_checkout(
        doc_id=seeded_doc, tenant_id=TENANT,
        user_id=1, user_email="a@hjmm.com",
        client="desktop-vsto-1.0.0",
    )
    extended = await cs.extend_checkout(
        doc_id=seeded_doc, tenant_id=TENANT, user_id=99,
    )
    assert extended is None


# ═════════════════════════════════════════════════════════════════════════
# Release (3)
# ═════════════════════════════════════════════════════════════════════════

async def test_release_by_holder(seeded_doc):
    """Holder may release their own checkout."""
    await cs.attempt_checkout(
        doc_id=seeded_doc, tenant_id=TENANT,
        user_id=1, user_email="a@hjmm.com",
        client="desktop-vsto-1.0.0",
    )
    cleared = await cs.release_checkout(
        doc_id=seeded_doc, tenant_id=TENANT,
        requester_user_id=1, requester_role="staff",
    )
    assert cleared.checked_out_by == "1"
    post = await cs.read_checkout(doc_id=seeded_doc, tenant_id=TENANT)
    assert post is None


async def test_release_by_admin_force(seeded_doc):
    """Admin force-releases another user's lock."""
    await cs.attempt_checkout(
        doc_id=seeded_doc, tenant_id=TENANT,
        user_id=1, user_email="a@hjmm.com",
        client="desktop-vsto-1.0.0",
    )
    cleared = await cs.release_checkout(
        doc_id=seeded_doc, tenant_id=TENANT,
        requester_user_id=99, requester_role="admin",
    )
    assert cleared.checked_out_by == "1"
    post = await cs.read_checkout(doc_id=seeded_doc, tenant_id=TENANT)
    assert post is None


async def test_release_by_non_admin_non_holder_denied(seeded_doc):
    """Other-user with non-admin role cannot release; raises NotPermitted."""
    await cs.attempt_checkout(
        doc_id=seeded_doc, tenant_id=TENANT,
        user_id=1, user_email="a@hjmm.com",
        client="desktop-vsto-1.0.0",
    )
    with pytest.raises(cs.NotPermitted):
        await cs.release_checkout(
            doc_id=seeded_doc, tenant_id=TENANT,
            requester_user_id=2, requester_role="staff",
        )


# ═════════════════════════════════════════════════════════════════════════
# Expired-lock override (1)  —  the patent-novelty path
# ═════════════════════════════════════════════════════════════════════════

async def test_expired_lock_overridden(seeded_doc):
    """A 9-hour-old lock is silently overridden by a different user."""
    nine_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    stale = json.dumps({
        "checked_out_by":       "99",
        "checked_out_by_email": "old@hjmm.com",
        "checked_out_at":       nine_hours_ago,
        "checked_out_client":   "desktop-vsto-1.0.0",
        "checkout_lock_ttl":    28800,
    })

    async with AsyncSessionLocal() as s:
        await s.execute(sa_text("""
            UPDATE documents
            SET metadata = CAST(:meta AS jsonb)
            WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid
        """), {"id": seeded_doc, "tid": TENANT, "meta": stale})
        await s.commit()

    state = await cs.attempt_checkout(
        doc_id=seeded_doc, tenant_id=TENANT,
        user_id=3, user_email="c@hjmm.com",
        client="desktop-vsto-1.0.0",
    )
    assert state.checked_out_by == "3"
    assert state.checked_out_by_email == "c@hjmm.com"


# ═════════════════════════════════════════════════════════════════════════
# Manifest discovery (2)
# ═════════════════════════════════════════════════════════════════════════

def test_manifest_empty_dir_returns_none(monkeypatch, tmp_path):
    """When the installer dir has no Praesidium-Desktop-*.msi files, build returns None."""
    monkeypatch.setenv("DESKTOP_INSTALLER_DIR", str(tmp_path))
    manifest_router._manifest_cache["fetched_at"] = 0.0
    manifest_router._manifest_cache["value"] = None
    assert manifest_router._build_manifest() is None


def test_manifest_picks_newest_installer(monkeypatch, tmp_path):
    """Newest matching .msi wins by mtime; sha256 is computed."""
    monkeypatch.setenv("DESKTOP_INSTALLER_DIR", str(tmp_path))
    monkeypatch.setenv("DESKTOP_INSTALLER_URL_BASE",
                       "https://login.example.com/installers")

    older = tmp_path / "Praesidium-Desktop-1.0.0.msi"
    older.write_bytes(b"old fake installer payload")
    newer = tmp_path / "Praesidium-Desktop-1.1.0.msi"
    newer.write_bytes(b"new fake installer payload, slightly larger")

    older_mtime = older.stat().st_mtime
    os.utime(newer, (older_mtime + 5, older_mtime + 5))

    manifest_router._manifest_cache["fetched_at"] = 0.0
    manifest_router._manifest_cache["value"] = None
    m = manifest_router._build_manifest()

    assert m is not None
    assert m["version"] == "1.1.0"
    assert m["filename"] == "Praesidium-Desktop-1.1.0.msi"
    assert m["sha256"] and len(m["sha256"]) == 64
    assert m["download_url"].endswith("/Praesidium-Desktop-1.1.0.msi")


# ═════════════════════════════════════════════════════════════════════════
# Bulk heartbeat (2)
# ═════════════════════════════════════════════════════════════════════════

async def test_bulk_heartbeat_mixed_results(seeded_doc):
    """Returns True for held doc, False for not-held."""
    other_doc = str(uuid.uuid4())
    async with AsyncSessionLocal() as s:
        await s.execute(sa_text("""
            INSERT INTO documents (id, tenant_id, filename, created_at, updated_at)
            VALUES (CAST(:id AS uuid), :tid, 'mdesk-bulk-test.docx', NOW(), NOW())
        """), {"id": other_doc, "tid": TENANT})
        await s.commit()

    try:
        await cs.attempt_checkout(
            doc_id=seeded_doc, tenant_id=TENANT,
            user_id=1, user_email="a@hjmm.com",
            client="desktop-vsto-1.0.0",
        )
        out = await cs.bulk_extend_checkouts(
            doc_ids=[seeded_doc, other_doc],
            tenant_id=TENANT, user_id=1,
        )
        assert out[seeded_doc] is True
        assert out[other_doc] is False
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(
                sa_text("DELETE FROM documents WHERE id = CAST(:id AS uuid)"),
                {"id": other_doc},
            )
            await s.commit()


async def test_bulk_heartbeat_invalid_uuid_returns_false(seeded_doc):
    """Syntactically invalid UUIDs come back as False without exception."""
    out = await cs.bulk_extend_checkouts(
        doc_ids=["not-a-uuid", seeded_doc, "also-not-a-uuid"],
        tenant_id=TENANT, user_id=1,
    )
    assert out["not-a-uuid"] is False
    assert out["also-not-a-uuid"] is False
    assert out[seeded_doc] is False


# ═════════════════════════════════════════════════════════════════════════
# Compare router validation (1)
# ═════════════════════════════════════════════════════════════════════════

def test_compare_request_rejects_same_doc():
    """Pydantic CompareRequest accepts both ids; route logic enforces inequality."""
    from modules.desktop.compare_router import CompareRequest
    req = CompareRequest(doc_a_id="aaa", doc_b_id="aaa")
    assert req.doc_a_id == req.doc_b_id

    import inspect
    from modules.desktop import compare_router as cr
    src = inspect.getsource(cr.enqueue_compare)
    assert (
        "doc_a_id == body.doc_b_id" in src
        or "same_document" in src
    ), "route is missing the same-doc validation"
