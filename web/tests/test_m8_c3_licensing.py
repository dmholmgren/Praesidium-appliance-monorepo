"""
test_m8_c3_licensing.py
Module 8 Component 3 — Licensing & Tenant Admin
Pytest suite — 18 tests

T-01  GET /admin/licensing — authenticated returns 200
T-02  GET /admin/licensing — unauthenticated redirects
T-03  GET /admin/api/licensing/features — returns all 19 feature flags
T-04  GET /admin/api/licensing/features — each entry has flag, label, tier
T-05  GET /admin/api/licensing/tenants — returns tenant list
T-06  GET /admin/api/licensing/tenant/{id} — unknown tenant returns 404
T-07  POST /admin/api/licensing/tenant/{id} — invalid tier returns 422
T-08  POST /admin/api/licensing/tenant/{id} — valid provision succeeds
T-09  GET /admin/api/licensing/overrides — returns overrides list
T-10  POST /admin/api/licensing/overrides — set valid override
T-11  POST /admin/api/licensing/overrides — unknown flag returns 422
T-12  DELETE /admin/api/licensing/overrides/{flag} — removes override
T-13  DELETE /admin/api/licensing/overrides/{flag} — non-existent returns 404
T-14  GET /admin/api/licensing/check/{id}/{flag} — returns enabled bool
T-15  GET /admin/htmx/license-grid — returns HTML fragment
T-16  POST /admin/htmx/license/provision — returns provision result fragment
T-17  TIER_DEFAULTS: intelligence tier includes ediscovery features
T-18  TIER_DEFAULTS: enterprise tier includes all features

Run inside container:
  pytest tests/test_m8_c3_licensing.py -v
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    import httpx
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    DEPS_AVAILABLE = True
except ImportError:
    DEPS_AVAILABLE = False

SKIP = not DEPS_AVAILABLE


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def set_token():
    os.environ["PLATFORM_ADMIN_PASSWORD_HASH"] = ""
    yield
    os.environ.pop("PLATFORM_ADMIN_PASSWORD_HASH", None)


TEST_ADMIN_TOKEN = "test-c3-token"


@pytest.fixture
def client():
    if SKIP:
        pytest.skip("deps not available")
    try:
        import importlib
        import modules.admin.licensing_api as lic_mod
        import modules.admin.admin_panel as ap_mod
        importlib.reload(ap_mod)
        importlib.reload(lic_mod)

        with patch.object(ap_mod, "templates") as mock_tmpl, \
             patch.object(lic_mod, "templates") as mock_lic_tmpl:
            from fastapi.responses import HTMLResponse
            mock_tmpl.TemplateResponse = lambda n, c, **kw: HTMLResponse(
                f"<html>{n}:{c.get('page','')}</html>",
                status_code=kw.get("status_code", 200)
            )
            mock_lic_tmpl.TemplateResponse = lambda n, c, **kw: HTMLResponse(
                f"<html>{n}</html>",
                status_code=kw.get("status_code", 200)
            )

            app = FastAPI()
            app.include_router(ap_mod.router)
            app.include_router(lic_mod.router)

            c = TestClient(app, headers={"X-Platform-Admin": TEST_ADMIN_TOKEN},
                          follow_redirects=False)

            # Inject token into admin_panel session store directly
            import os as _os
            _os.environ["PLATFORM_ADMIN_PASSWORD_HASH"] = ""
            import importlib; importlib.reload(ap_mod)
            token = ap_mod._create_session()
            yield c, token, lic_mod

    except ImportError as exc:
        pytest.skip(f"App deps unavailable: {exc}")


def _mock_session(rows=None, fetchone_val=None):
    mock_sess = AsyncMock()
    mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
    mock_sess.__aexit__ = AsyncMock(return_value=False)
    mock_sess.execute = AsyncMock(
        return_value=MagicMock(
            mappings=lambda: MagicMock(
                fetchall=lambda: rows or [],
                fetchone=lambda: fetchone_val
            ),
            fetchall=lambda: [],
            fetchone=lambda: fetchone_val,
            rowcount=1,
        )
    )
    mock_sess.commit = AsyncMock()
    return mock_sess


# ══════════════════════════════════════════════════════════════════════════════
# T-01 to T-02: Page auth
# ══════════════════════════════════════════════════════════════════════════════

def test_t01_licensing_page_authenticated(client):
    """T-01: GET /admin/licensing authenticated returns 200."""
    c, cookie, _ = client
    with patch("modules.admin.licensing_api.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value = _mock_session(rows=[])
        resp = c.get("/admin/licensing", cookies={"praesidium_admin_session": cookie})
    assert resp.status_code == 200


def test_t02_licensing_page_unauthenticated(client):
    """T-02: GET /admin/licensing without session redirects."""
    c, _, _ = client
    resp = c.get("/admin/licensing")
    assert resp.status_code == 302


# ══════════════════════════════════════════════════════════════════════════════
# T-03 to T-04: Feature list
# ══════════════════════════════════════════════════════════════════════════════

def test_t03_features_returns_all_flags(client):
    """T-03: GET /admin/api/licensing/features returns all 19 feature flags."""
    c, cookie, _ = client
    resp = c.get("/admin/api/licensing/features",
                 cookies={"praesidium_admin_session": cookie})
    assert resp.status_code == 200
    features = resp.json()["features"]
    assert len(features) == 19


def test_t04_features_have_required_fields(client):
    """T-04: Each feature entry has flag, label, tier."""
    c, cookie, _ = client
    resp = c.get("/admin/api/licensing/features",
                 cookies={"praesidium_admin_session": cookie})
    for f in resp.json()["features"]:
        assert "flag" in f
        assert "label" in f
        assert "tier" in f


# ══════════════════════════════════════════════════════════════════════════════
# T-05 to T-08: Tenant licensing API
# ══════════════════════════════════════════════════════════════════════════════

def test_t05_tenants_returns_list(client):
    """T-05: GET /admin/api/licensing/tenants returns tenant list."""
    c, cookie, _ = client
    with patch("modules.admin.licensing_api.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value = _mock_session(rows=[])
        resp = c.get("/admin/api/licensing/tenants",
                     cookies={"praesidium_admin_session": cookie})
    assert resp.status_code == 200
    assert "tenants" in resp.json()


def test_t06_tenant_not_found(client):
    """T-06: GET /admin/api/licensing/tenant/{id} — unknown ID returns 404."""
    c, cookie, _ = client
    with patch("modules.admin.licensing_api.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value = _mock_session(rows=[], fetchone_val=None)
        resp = c.get("/admin/api/licensing/tenant/nonexistent-id",
                     cookies={"praesidium_admin_session": cookie})
    assert resp.status_code == 404


def test_t07_provision_invalid_tier(client):
    """T-07: POST with invalid tier returns 422."""
    c, cookie, _ = client
    resp = c.post(
        "/admin/api/licensing/tenant/some-id",
        json={"tier": "invalid_tier", "feature_flags": {}},
        cookies={"praesidium_admin_session": cookie}
    )
    assert resp.status_code == 422


def test_t08_provision_valid_tier(client):
    """T-08: POST with valid tier calls provision_tenant_license."""
    c, cookie, _ = client
    with patch("modules.admin.licensing_api.provision_tenant_license",
               new_callable=AsyncMock, return_value=True):
        resp = c.post(
            "/admin/api/licensing/tenant/test-tenant-id",
            json={"tier": "intelligence", "feature_flags": {"feature_ediscovery": True}},
            cookies={"praesidium_admin_session": cookie}
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ══════════════════════════════════════════════════════════════════════════════
# T-09 to T-13: Global overrides
# ══════════════════════════════════════════════════════════════════════════════

def test_t09_list_overrides(client):
    """T-09: GET /admin/api/licensing/overrides returns list."""
    c, cookie, _ = client
    with patch("modules.admin.licensing_api.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value = _mock_session(rows=[])
        resp = c.get("/admin/api/licensing/overrides",
                     cookies={"praesidium_admin_session": cookie})
    assert resp.status_code == 200
    assert "overrides" in resp.json()


def test_t10_set_override_valid(client):
    """T-10: POST valid override succeeds."""
    c, cookie, _ = client
    with patch("modules.admin.licensing_api.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value = _mock_session()
        resp = c.post(
            "/admin/api/licensing/overrides",
            json={"feature_flag": "feature_ediscovery", "enabled_globally": False, "reason": "test"},
            cookies={"praesidium_admin_session": cookie}
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_t11_set_override_unknown_flag(client):
    """T-11: POST with unknown flag returns 422."""
    c, cookie, _ = client
    resp = c.post(
        "/admin/api/licensing/overrides",
        json={"feature_flag": "feature_does_not_exist", "enabled_globally": False},
        cookies={"praesidium_admin_session": cookie}
    )
    assert resp.status_code == 422


def test_t12_delete_override(client):
    """T-12: DELETE existing override returns ok."""
    c, cookie, _ = client
    with patch("modules.admin.licensing_api.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value = _mock_session()
        resp = c.delete(
            "/admin/api/licensing/overrides/feature_ediscovery",
            cookies={"praesidium_admin_session": cookie}
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_t13_delete_override_not_found(client):
    """T-13: DELETE non-existent override returns 404."""
    c, cookie, _ = client
    with patch("modules.admin.licensing_api.AsyncSessionLocal") as mock_sl:
        sess = _mock_session()
        # Override rowcount to 0 (nothing deleted)
        sess.execute = AsyncMock(
            return_value=MagicMock(rowcount=0)
        )
        sess.commit = AsyncMock()
        mock_sl.return_value = sess
        resp = c.delete(
            "/admin/api/licensing/overrides/nonexistent_flag",
            cookies={"praesidium_admin_session": cookie}
        )
    assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# T-14: Live feature check
# ══════════════════════════════════════════════════════════════════════════════

def test_t14_check_feature_live(client):
    """T-14: GET /admin/api/licensing/check returns enabled bool."""
    c, cookie, _ = client
    with patch("modules.admin.licensing_api.check_feature",
               new_callable=AsyncMock, return_value=True):
        resp = c.get(
            "/admin/api/licensing/check/test-tenant/feature_ediscovery",
            cookies={"praesidium_admin_session": cookie}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert "checked_at" in data


# ══════════════════════════════════════════════════════════════════════════════
# T-15 to T-16: HTMX partials
# ══════════════════════════════════════════════════════════════════════════════

def test_t15_htmx_license_grid(client):
    """T-15: GET /admin/htmx/license-grid returns HTML."""
    c, cookie, _ = client
    with patch("modules.admin.licensing_api.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value = _mock_session(rows=[])
        resp = c.get("/admin/htmx/license-grid",
                     cookies={"praesidium_admin_session": cookie})
    assert resp.status_code == 200


def test_t16_htmx_provision(client):
    """T-16: POST /admin/htmx/license/provision returns HTML fragment."""
    c, cookie, _ = client
    with patch("modules.admin.licensing_api.provision_tenant_license",
               new_callable=AsyncMock, return_value=True):
        resp = c.post(
            "/admin/htmx/license/provision",
            data={"tenant_id": "test-id", "tier": "intelligence"},
            cookies={"praesidium_admin_session": cookie}
        )
    assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# T-17 to T-18: Tier defaults
# ══════════════════════════════════════════════════════════════════════════════

def test_t17_intelligence_tier_includes_ediscovery(client):
    """T-17: Intelligence tier defaults include ediscovery features."""
    _, _, lic_mod = client
    defaults = lic_mod.TIER_DEFAULTS["intelligence"]
    assert "feature_ediscovery" in defaults
    assert "feature_knowledge_graph" in defaults


def test_t18_enterprise_tier_includes_all(client):
    """T-18: Enterprise tier defaults include all 19 features."""
    _, _, lic_mod = client
    defaults = lic_mod.TIER_DEFAULTS["enterprise"]
    all_flags = {f for f, _, _ in lic_mod.ALL_FEATURES}
    assert defaults == all_flags, f"Enterprise missing: {all_flags - defaults}"
