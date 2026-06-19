"""Tab Service — sync accessor for the canonical layout_tabs registry.

The platform already drives tab bars from the `layout_tabs` table (served async
to React/mobile via /api/v1/nav-tabs and to matter_detail via dashboard._get_tabs).
This module is the *synchronous* accessor used by server-rendered Jinja tab
strips (e.g. billing/layout.html) that build their context in sync helpers — it
injects `module_tabs` the way get_nav_context injects `nav_items`.

Reads the `navigate`-type rows for a layout_slug (those carry a target_ref URL,
i.e. a real navigation strip, vs `tab_scope` in-page React tabs). Tenant-scoped,
role-gated, in-memory TTL cache, graceful []-on-error so callers fall back to
their hardcoded markup and the UI never breaks.
"""
import os
import time
import logging

logger = logging.getLogger(__name__)

_TTL = 300                      # seconds; tabs change rarely
_CACHE: dict = {}               # layout_slug -> (expires_ts, rows)


def _conn():
    import psycopg2
    raw = os.environ.get("DATABASE_URL", "") \
        .replace("postgresql+asyncpg://", "").replace("postgresql://", "")
    at = raw.rfind("@")
    userpass, hostdb = raw[:at], raw[at + 1:]
    colon = userpass.find(":")
    user, password = userpass[:colon], userpass[colon + 1:]
    slash = hostdb.rfind("/")
    hostport, dbname = hostdb[:slash], hostdb[slash + 1:].split("?")[0]
    hp = hostport.split(":")
    host = hp[0]
    port = int(hp[1]) if len(hp) > 1 else 5432
    c = psycopg2.connect(host=host, port=port, dbname=dbname, user=user, password=password)
    c.autocommit = True
    return c


def _load(layout_slug: str):
    now = time.time()
    hit = _CACHE.get(layout_slug)
    if hit and hit[0] > now:
        return hit[1]
    rows = []
    try:
        c = _conn()
        cur = c.cursor()
        cur.execute(
            "SELECT tab_slug, display_name, target_ref, display_order, "
            "required_role, is_active, is_visible, tenant_id "
            "FROM layout_tabs "
            "WHERE layout_slug = %s AND target_type = 'navigate' "
            "ORDER BY display_order",
            (layout_slug,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()
        c.close()
    except Exception as exc:
        logger.warning("tab_service load failed for %s: %s", layout_slug, exc)
        rows = []
    _CACHE[layout_slug] = (now + _TTL, rows)
    return rows


def _role_ok(user_role, required_role):
    if not required_role:
        return True
    try:
        from core.services.nav_service import _role_satisfies
        return _role_satisfies(user_role, required_role)
    except Exception:
        return True   # graceful: never hide a tab on a gating error


def get_tabs_sync(layout_slug: str, tenant_id=None, user_role=None):
    """Resolved, role-gated, ordered nav-tab list for a layout. [] on failure.

    Each item: {tab_key, label, url_path} (consumed by the tab-strip partial).
    Tenant rows (tenant_id match) override platform-default (NULL) rows; an
    inactive tenant row suppresses the default.
    """
    rows = _load(layout_slug)
    tid = ((tenant_id or "").strip() or None)

    suppressed = {
        r["tab_slug"] for r in rows
        if r["tenant_id"] is not None
        and (r["tenant_id"] or "").strip() == tid and not r["is_active"]
    }
    by_key: dict = {}
    for r in rows:
        if not r.get("is_visible", True):
            continue
        k = r["tab_slug"]
        if k in suppressed:
            by_key.pop(k, None)
            continue
        if r["tenant_id"] is not None:
            if (r["tenant_id"] or "").strip() == tid and r["is_active"]:
                by_key[k] = r
        elif r["is_active"] and k not in by_key:
            by_key[k] = r

    items = sorted(by_key.values(), key=lambda x: x["display_order"])
    return [
        {"tab_key": i["tab_slug"], "label": i["display_name"], "url_path": i["target_ref"]}
        for i in items
        if _role_ok(user_role, i.get("required_role"))
    ]


def invalidate(layout_slug: str = None):
    """Drop cached tabs (call after editing layout_tabs)."""
    if layout_slug:
        _CACHE.pop(layout_slug, None)
    else:
        _CACHE.clear()
