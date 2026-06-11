"""
tests/test_permissions.py

Tests for core/auth/permissions.py — PermissionService.can() Pattern A.

Each test mocks AsyncSessionLocal so we don't hit the database. The point
is to verify:
  - Decision logic (default-allow vs default-deny, role bypass, matrix
    rows, scope filter assembly)
  - Empty-list edge case for users with no matter access
  - Override semantics (firm admin can override anything)
  - External user scoping

These tests DO NOT verify SQL correctness end-to-end — that's covered by
integration tests against a live DB (separate suite). The focus here is
behavior under controlled inputs.

Run with:
    cd /opt/praesidium-web
    pytest tests/test_permissions.py -v
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.auth.permissions import (
    PermissionService,
    PermissionResult,
    INTERNAL_ROLES,
    EXTERNAL_ROLES,
    MATTER_SCOPED_MODULES,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

TENANT_HJMM = "986c0fee-1390-43bb-ad28-8cd1db6de53f"


@dataclass
class FakeUser:
    """Minimal stand-in for the User ORM model."""
    id: int
    tenant_id: str
    role: str
    is_active: bool = True


def make_user(role: str, **kwargs) -> FakeUser:
    """Build a FakeUser with sensible defaults."""
    return FakeUser(
        id=kwargs.get("id", 100),
        tenant_id=kwargs.get("tenant_id", TENANT_HJMM),
        role=role,
        is_active=kwargs.get("is_active", True),
    )


class FakeResult:
    """Mimics SQLAlchemy Result enough for our queries."""
    def __init__(self, rows: list[Any], mappings_first_value: Optional[dict] = None):
        self._rows = rows
        self._mappings_first = mappings_first_value

    def mappings(self):
        return _Mappings(self._mappings_first)

    def fetchall(self):
        return [(r,) if not isinstance(r, tuple) else r for r in self._rows]


class _Mappings:
    def __init__(self, first_value: Optional[dict]):
        self._first = first_value

    def first(self):
        return self._first


def make_session_mock(*, matrix_row=None, matter_ids=None,
                      client_scopes=None, deal_room_scopes=None,
                      matters_for_clients=None):
    """
    Build a mock async session whose .execute() returns the right FakeResult
    for each query. Identifies the query by inspecting the SQL text.
    """
    session = MagicMock()

    async def _execute(stmt, params=None):
        sql = str(stmt)
        if "FROM permission_matrix" in sql:
            return FakeResult([], mappings_first_value=matrix_row)
        if "FROM matter_timekeepers" in sql:
            return FakeResult(matter_ids or [])
        if "scope_type = 'client'" in sql:
            return FakeResult(client_scopes or [])
        if "scope_type = 'deal_room'" in sql:
            return FakeResult(deal_room_scopes or [])
        if "FROM matters" in sql and "client_id" in sql:
            return FakeResult(matters_for_clients or [])
        return FakeResult([])

    session.execute = AsyncMock(side_effect=_execute)
    return session


@asynccontextmanager
async def _session_cm(session_mock):
    yield session_mock


def patch_session(session_mock):
    """Patch AsyncSessionLocal to return a fresh context manager every call.

    return_value=<single CM> would break — _AsyncGeneratorContextManager is
    one-shot; multiple calls into the service produce multiple AsyncSession-
    Local() invocations and each needs its own CM.
    """
    return patch(
        "core.auth.permissions.AsyncSessionLocal",
        side_effect=lambda: _session_cm(session_mock),
    )


# ── Tests: trivial gates ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_user_denies():
    result = await PermissionService.can(None, "anything", "view")
    assert not result.allowed


@pytest.mark.asyncio
async def test_inactive_user_denies():
    user = make_user("admin", is_active=False)
    result = await PermissionService.can(user, "anything", "view")
    assert not result.allowed


@pytest.mark.asyncio
async def test_user_missing_tenant_denies():
    user = make_user("admin", tenant_id="")
    result = await PermissionService.can(user, "anything", "view")
    assert not result.allowed


# ── Tests: super_admin bypass ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_super_admin_bypasses_everything():
    """super_admin returns allowed=True with empty scope_filters."""
    user = make_user("super_admin")
    # No DB access expected — bypass returns immediately.
    result = await PermissionService.can(user, "permission_matrix", "edit")
    assert result.allowed is True
    assert result.scope_filters == {}


# ── Tests: admin role ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_unrestricted_within_tenant():
    """admin gets unrestricted scope (no scope_filters) for matter-scoped modules."""
    user = make_user("admin")
    session = make_session_mock()  # matrix lookup returns None → default-allow
    with patch_session(session):
        result = await PermissionService.can(user, "billing", "view")
    assert result.allowed
    assert result.scope_filters == {}


# ── Tests: internal default-allow ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_attorney_default_allow_with_matter_scope():
    """Attorney, no matrix row → default-allow + matter_id scope."""
    user = make_user("attorney", id=42)
    session = make_session_mock(
        matter_ids=["m1", "m2", "m3"],
    )
    with patch_session(session):
        result = await PermissionService.can(user, "billing", "view")
    assert result.allowed
    assert result.scope_filters == {"matter_id": ["m1", "m2", "m3"]}


@pytest.mark.asyncio
async def test_attorney_with_no_matters_returns_empty_list():
    """
    Attorney with zero matter assignments gets allowed=True with matter_id=[].
    Caller must treat empty list as deny-all and never emit WHERE x IN ().
    """
    user = make_user("attorney")
    session = make_session_mock(matter_ids=[])
    with patch_session(session):
        result = await PermissionService.can(user, "matters", "view")
    assert result.allowed
    assert result.scope_filters.get("matter_id") == []


# ── Tests: matrix overrides default ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_attorney_denied_when_matrix_says_no():
    """Even an internal role gets denied if a matrix row says allowed=False."""
    user = make_user("attorney")
    session = make_session_mock(
        matrix_row={"allowed": False, "locked_by_admin": False},
    )
    with patch_session(session):
        result = await PermissionService.can(user, "bill_run", "finalize")
    assert not result.allowed


@pytest.mark.asyncio
async def test_partner_explicit_allow_for_finalize():
    """Partner with explicit allow row gets allowed=True."""
    user = make_user("partner")
    session = make_session_mock(
        matrix_row={"allowed": True, "locked_by_admin": False},
        matter_ids=["m1"],
    )
    with patch_session(session):
        result = await PermissionService.can(user, "bill_run", "finalize")
    assert result.allowed
    assert result.scope_filters.get("matter_id") == ["m1"]


# ── Tests: external roles ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_client_default_denies_without_matrix_row():
    """Client role + no matrix row → deny."""
    user = make_user("client")
    session = make_session_mock()
    with patch_session(session):
        result = await PermissionService.can(user, "billing", "view")
    assert not result.allowed


@pytest.mark.asyncio
async def test_client_explicit_allow_with_scope():
    """Client with explicit allow row gets client_id + matter_id scope."""
    user = make_user("client", id=200)
    session = make_session_mock(
        matrix_row={"allowed": True, "locked_by_admin": False},
        client_scopes=["c1", "c2"],
        matters_for_clients=["m10", "m11"],
    )
    with patch_session(session):
        result = await PermissionService.can(user, "matters", "view")
    assert result.allowed
    assert result.scope_filters.get("client_id") == ["c1", "c2"]
    assert result.scope_filters.get("matter_id") == ["m10", "m11"]


@pytest.mark.asyncio
async def test_client_with_no_scopes_returns_empty_lists():
    """Client with no external_user_scopes gets allowed=True with empty filters."""
    user = make_user("client")
    session = make_session_mock(
        matrix_row={"allowed": True, "locked_by_admin": False},
        client_scopes=[],
        matters_for_clients=[],
    )
    with patch_session(session):
        result = await PermissionService.can(user, "dms", "view")
    assert result.allowed
    assert result.scope_filters.get("client_id") == []
    assert result.scope_filters.get("matter_id") == []


@pytest.mark.asyncio
async def test_deal_room_guest_explicit_allow():
    """deal_room_guest with explicit allow gets deal_room_id scope."""
    user = make_user("deal_room_guest", id=300)
    session = make_session_mock(
        matrix_row={"allowed": True, "locked_by_admin": False},
        deal_room_scopes=["dr1"],
    )
    with patch_session(session):
        result = await PermissionService.can(user, "deal_room", "view")
    assert result.allowed
    assert result.scope_filters.get("deal_room_id") == ["dr1"]


@pytest.mark.asyncio
async def test_unknown_role_denies():
    """Unknown role gets defensive deny — never accidentally allow."""
    user = make_user("rando_role")
    session = make_session_mock()
    with patch_session(session):
        result = await PermissionService.can(user, "billing", "view")
    assert not result.allowed


# ── Tests: result helper ─────────────────────────────────────────────────────

def test_filter_for_returns_none_when_unrestricted():
    """filter_for returns None when the dimension is absent (= unrestricted)."""
    result = PermissionResult(allowed=True, scope_filters={})
    assert result.filter_for("matter_id") is None


def test_filter_for_returns_empty_list_when_explicit():
    """filter_for returns [] when explicitly empty (caller must treat as deny-all)."""
    result = PermissionResult(allowed=True, scope_filters={"matter_id": []})
    assert result.filter_for("matter_id") == []


def test_filter_for_returns_list_when_populated():
    result = PermissionResult(
        allowed=True,
        scope_filters={"matter_id": ["m1", "m2"]},
    )
    assert result.filter_for("matter_id") == ["m1", "m2"]


# ── Tests: configuration sanity ──────────────────────────────────────────────

def test_role_classes_disjoint():
    """A role belongs to exactly one class (internal XOR external)."""
    assert INTERNAL_ROLES.isdisjoint(EXTERNAL_ROLES)


def test_matter_scoped_modules_includes_billing():
    """Smoke test the configuration we depend on."""
    assert "billing" in MATTER_SCOPED_MODULES
    assert "bill_run" in MATTER_SCOPED_MODULES
    assert "widgets" not in MATTER_SCOPED_MODULES  # widgets handled by per-slug filter
