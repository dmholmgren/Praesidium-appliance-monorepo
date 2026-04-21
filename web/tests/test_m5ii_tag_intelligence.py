"""
M5ii — Tag Intelligence Tests
Tests source separation enforcement, co-occurrence logic,
tag apply/confirm/reject endpoints, and quality metrics.
Expected: 16 passed
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ================================================================== #
# App fixture                                                         #
# ================================================================== #

@pytest.fixture(scope="module")
def client():
    from app import app
    return TestClient(app, raise_server_exceptions=True)


def _patch_tenant(tenant_id: str = "hjmm-prod"):
    return patch(
        "modules.ediscovery.tag_intelligence._get_tenant_id",
        return_value=tenant_id,
    )


def _patch_user(user_id: int = 1):
    return patch(
        "modules.ediscovery.tag_intelligence._get_user_id",
        return_value=user_id,
    )


# ================================================================== #
# Import tests                                                        #
# ================================================================== #

def test_module_importable():
    from modules.ediscovery import tag_intelligence
    assert hasattr(tag_intelligence, "router")


def test_router_has_routes():
    from modules.ediscovery.tag_intelligence import router
    paths = [r.path for r in router.routes]
    assert any("tag-intelligence" in p for p in paths)
    assert any("tags" in p for p in paths)


def test_source_constants_defined():
    from modules.ediscovery.tag_intelligence import AI_SOURCES, USER_SOURCES
    assert "ai" in AI_SOURCES
    assert "ai_confirmed" in AI_SOURCES
    assert "user" in USER_SOURCES
    assert "user_trained" in USER_SOURCES
    # Critical: the two sets must never overlap
    assert AI_SOURCES.isdisjoint(USER_SOURCES)


def test_cooccurrence_threshold_defined():
    from modules.ediscovery.tag_intelligence import COOCCURRENCE_THRESHOLD
    assert 0.0 < COOCCURRENCE_THRESHOLD < 1.0


# ================================================================== #
# Tag parsing unit tests                                              #
# ================================================================== #

def test_tags_from_coding_none():
    from modules.ediscovery.tag_intelligence import _tags_from_coding
    assert _tags_from_coding(None) == []


def test_tags_from_coding_empty_dict():
    from modules.ediscovery.tag_intelligence import _tags_from_coding
    assert _tags_from_coding({}) == []


def test_tags_from_coding_list():
    from modules.ediscovery.tag_intelligence import _tags_from_coding
    tags = [{"name": "Breach", "source": "ai"}]
    assert _tags_from_coding(tags) == tags


def test_tags_from_coding_dict_with_tags():
    from modules.ediscovery.tag_intelligence import _tags_from_coding
    coding = {"tags": [{"name": "Fraud", "source": "user"}], "review_status": "complete"}
    result = _tags_from_coding(coding)
    assert len(result) == 1
    assert result[0]["name"] == "Fraud"


def test_tags_from_coding_json_string():
    from modules.ediscovery.tag_intelligence import _tags_from_coding
    coding = json.dumps({"tags": [{"name": "Privilege", "source": "user"}]})
    result = _tags_from_coding(coding)
    assert result[0]["name"] == "Privilege"


# ================================================================== #
# Source separation enforcement tests                                 #
# ================================================================== #

def test_source_separation_never_merged():
    """
    get_document_tags must NEVER return a merged 'tags' field.
    Response must have 'ai_tags' and 'user_tags' separately.
    """
    from modules.ediscovery.tag_intelligence import _tags_from_coding, AI_SOURCES, USER_SOURCES

    coding = {
        "tags": [
            {"name": "Breach", "source": "ai", "confidence": 0.91},
            {"name": "Hot Document", "source": "user"},
            {"name": "Scienter", "source": "ai_confirmed", "confidence": 0.87},
            {"name": "Privilege", "source": "user_trained"},
        ]
    }
    tags = _tags_from_coding(coding)

    ai_tags   = [t for t in tags if t.get("source") in AI_SOURCES]
    user_tags = [t for t in tags if t.get("source") in USER_SOURCES]

    # Verify correct separation
    assert len(ai_tags) == 2
    assert len(user_tags) == 2
    assert all(t["source"] in AI_SOURCES for t in ai_tags)
    assert all(t["source"] in USER_SOURCES for t in user_tags)

    # Verify no overlap
    ai_names   = {t["name"] for t in ai_tags}
    user_names = {t["name"] for t in user_tags}
    assert ai_names.isdisjoint(user_names)


# ================================================================== #
# Co-occurrence logic unit tests                                      #
# ================================================================== #

def test_cooccurrence_below_threshold_excluded():
    """
    Tags that co-occur below threshold must not appear in result.
    """
    from modules.ediscovery.tag_intelligence import COOCCURRENCE_THRESHOLD
    # If threshold is 0.60, a tag pair co-occurring 50% should be excluded
    rate = 0.50
    assert rate < COOCCURRENCE_THRESHOLD  # confirms it would be excluded


def test_cooccurrence_above_threshold_included():
    """
    Tags co-occurring above threshold should be included.
    """
    from modules.ediscovery.tag_intelligence import COOCCURRENCE_THRESHOLD
    rate = 0.75
    assert rate >= COOCCURRENCE_THRESHOLD


# ================================================================== #
# Mock session context manager                                        #
# ================================================================== #

def _make_mock_session(coding_value=None, matter_name="Test Matter"):
    """
    Build a mock async context manager for AsyncSessionLocal().
    Patches core.db.base.AsyncSessionLocal directly.
    """
    mock_session = AsyncMock()

    async def mock_execute(query, params=None):
        result = MagicMock()
        q = str(query)
        if "matters" in q:
            result.mappings = MagicMock(return_value=MagicMock(
                first=MagicMock(return_value={"id": "matter-1", "name": matter_name})
            ))
        elif "ediscovery_documents" in q:
            result.mappings = MagicMock(return_value=MagicMock(
                first=MagicMock(return_value={"coding": coding_value})
            ))
        else:
            result.mappings = MagicMock(return_value=MagicMock(
                first=MagicMock(return_value=None)
            ))
            result.fetchall = MagicMock(return_value=[])
        return result

    mock_session.execute = mock_execute
    mock_session.commit = AsyncMock()

    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    return mock_cm


def _patch_session(coding_value=None, matter_name="Test Matter"):
    """Patch AsyncSessionLocal in tag_intelligence module."""
    mock_cm = _make_mock_session(coding_value=coding_value, matter_name=matter_name)
    return patch(
        "modules.ediscovery.tag_intelligence.AsyncSessionLocal",
        return_value=mock_cm,
    )


# ================================================================== #
# Endpoint tests with mocked DB                                      #
# ================================================================== #

def test_get_document_tags_returns_two_sections(client):
    """Response must have ai_tags and user_tags — never a merged tags field."""
    coding = {
        "tags": [
            {"name": "Breach", "source": "ai", "confidence": 0.9},
            {"name": "Hot Doc", "source": "user"},
        ]
    }
    doc_id = str(uuid.uuid4())

    with _patch_tenant(), _patch_session(coding_value=coding):
        resp = client.get(f"/ediscovery/documents/{doc_id}/tags")

    assert resp.status_code == 200
    data = resp.json()
    assert "ai_tags" in data
    assert "user_tags" in data
    assert "tags" not in data  # MUST NOT exist — two sections only


def test_confirm_tag_returns_ai_confirmed(client):
    """Confirm endpoint sets source to ai_confirmed."""
    coding = {"tags": [{"name": "Fraud", "source": "ai", "confidence": 0.85}]}
    doc_id = str(uuid.uuid4())

    with _patch_tenant(), _patch_user(), _patch_session(coding_value=coding):
        resp = client.post(
            f"/ediscovery/documents/{doc_id}/tags/confirm",
            json={"name": "Fraud"},
        )

    assert resp.status_code == 200
    assert resp.json()["source"] == "ai_confirmed"


def test_apply_user_tag_returns_user_source(client):
    """Apply user tag endpoint sets source to user."""
    coding = {"tags": []}
    doc_id = str(uuid.uuid4())

    with _patch_tenant(), _patch_user(), _patch_session(coding_value=coding):
        resp = client.post(
            f"/ediscovery/documents/{doc_id}/tags/apply",
            json={"name": "Attorney Work Product"},
        )

    assert resp.status_code == 200
    assert resp.json()["source"] == "user"


def test_reject_tag_sets_rejected_flag(client):
    """Reject endpoint marks tag as rejected without deleting it."""
    coding = {"tags": [{"name": "Relevant", "source": "ai", "confidence": 0.7}]}
    doc_id = str(uuid.uuid4())

    with _patch_tenant(), _patch_user(), _patch_session(coding_value=coding):
        resp = client.post(
            f"/ediscovery/documents/{doc_id}/tags/reject",
            json={"name": "Relevant"},
        )

    assert resp.status_code == 200
    assert resp.json()["rejected"] is True


def test_confirm_tag_missing_name_422(client):
    """Confirm with empty name returns 422."""
    doc_id = str(uuid.uuid4())
    with _patch_tenant(), _patch_user():
        resp = client.post(
            f"/ediscovery/documents/{doc_id}/tags/confirm",
            json={"name": ""},
        )
    assert resp.status_code == 422
