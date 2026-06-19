# Praesidium — Active Matter Pre-Population — Implementation Plan (v1)

**Author:** drafted for Claude Code execution on the appliance (10.10.0.10)
**Scope:** Make a matter selected once in the global topbar picker pre-fill the
pickers/links inside individual modules, **without** locking global state.
**Status of dependency:** Topbar picker + persistence are LIVE and verified
(write side). This plan is the **read side**.

---

## 1. Goal & design stance

Selecting a matter in the topbar should seed each module's picker as a *default*.
It is a **sticky default, not a lock** (per the v15.2 decision: picker behaves
like an email "To:" field; URL stays the source of truth; multi-window stays
first-class). A module that receives an explicit matter (in its URL or by the
user choosing one on that page) must override the seed. Nothing is hidden or
filtered globally.

Non-goals: global filtering, hiding non-active matters, forcing a single matter
across tabs/windows.

---

## 2. What already exists (do not rebuild)

- `GET/PUT /api/v1/active-matter` (session-authed) in
  `modules/dashboard/routes/matter_dashboard_api.py`.
  Persists to `users.user_preferences` JSONB keys `active_matter_id` +
  `active_matter_set_at`. Same keys the desktop/VSTO client reads
  (`modules/desktop/desktop_c3_router.py`) — single source of truth.
- `GET /api/v1/matters/search?q=` typeahead (respects is_personal / owner /
  chinese_wall_exclusions).
- `static/js/matter-picker.js` — topbar chip; on load/change it fetches
  `/api/v1/active-matter`, holds the value in a closure, and fires
  `window` CustomEvent `praesidium:matter-changed`.
- DB confirmed populating: user 19 (dennis@hjmmlegal.com) has a live
  `active_matter_id`.

**Gap:** the value is persisted but never *published* for other modules to read,
and no module consumes it. Drafting in particular is a React island
(`drafting-react-root`) whose working matter is `useState(null)` with its own
typeahead hitting `/api/v1/drafting/matters`; it never looks at the global value.

---

## 3. Architecture: one publisher, many consumers

### 3a. Publisher — `matter-picker.js` owns a small global API
`matter-picker.js` already loads in BOTH shells (`shell.html`, `base.html`), so
make it the single publisher. Add:

```
window.__ACTIVE_MATTER__        // the current value or null (see contract)
window.PraesidiumMatter = {
  getActive(): obj|null,        // synchronous best-known value
  isReady(): bool,              // true once first fetch/inject resolved
  onReady(cb),                  // cb(active|null) once, when first known
  onChange(cb),                 // cb(active|null) on every change
}
```

Value contract (matches `GET /api/v1/active-matter`):
```
{ matter_id, matter_name, matter_number, client_name, set_at, is_stale } | null
```

Behavior:
- On load: if `window.__ACTIVE_MATTER__` was injected server-side (3b), treat it
  as the immediate value and fire `onReady`; still revalidate via fetch.
- Otherwise fetch `/api/v1/active-matter`, set the global, fire `onReady`.
- On set/clear via the chip: update the global, fire `onChange`, keep the
  existing `praesidium:matter-changed` event for back-comat.

This makes consumers indifferent to whether the server injected a synchronous
value (shell.html pages) or it arrives async (base.html pages, which do NOT get
server nav context today).

### 3b. Optional sync seed — inject in shell render (removes first-paint flash)
- Create `core/services/active_matter.py`:
  `async def read_active_matter(uid: int, tid: str) -> dict | None`
  Move the SELECT currently inlined in `matter_dashboard_api.web_get_active_matter`
  into this function; call it from BOTH the API endpoint and nav_context (DRY —
  prevents the two reads from drifting). Function opens its own
  `AsyncSessionLocal`.
- `core/services/nav_context.py::get_nav_context` — add
  `active_matter` to the returned dict (uid/tid from `request.state`).
- `core/templates/shell.html` — beside the existing
  `window.__NAV_ITEMS__` line (~290) add:
  `<script>window.__ACTIVE_MATTER__ = {{ active_matter|tojson if active_matter else 'null' }};</script>`
- `base.html` pages mostly do NOT call `get_nav_context`; do NOT try to wire all
  of them. They rely on 3a's async path. (If a specific base.html route wants the
  sync seed, have that route add `active_matter` to its template context.)

asyncpg rules for `read_active_matter`: `TRIM(u.tenant_id)=:tid`,
`TRIM(m.tenant_id)=:tid`, `CAST(NULLIF(...,'') AS uuid)`, bind `uid` as int.
Never bind None into a CAST.

---

## 4. Consumers — seed pattern (apply per surface)

General rule for every module picker:
1. Determine explicit matter: URL param (e.g. `?matter_id=`) or in-page route
   segment. **If present, it wins — do not seed.**
2. Else `PraesidiumMatter.onReady(m => { if (m && !userTouched) seed(m); })`.
3. Subscribe `PraesidiumMatter.onChange(m => { if (!userTouched) seed(m); })` so
   a topbar switch updates an open page (skip if a surface should be sticky once
   opened — note per surface).
4. Any manual selection on the page sets `userTouched = true` and (optionally)
   writes back via `PUT /api/v1/active-matter` so the rest of the app follows.
5. Staleness: seed even when `is_stale` is true (sticky default); the topbar
   already shows the amber nudge. Do not suppress seeding on stale.

### Surface inventory & status
| Surface | Picker | Type | Seed point |
|---|---|---|---|
| Drafting (`/drafting`) | "Working Matter" (`K` comp) | **React island** | `he()` root: init state from `PraesidiumMatter.getActive()` instead of `null` |
| DMS | matter scope | server-rendered | seed default in route context or JS |
| eDiscovery search | matter picker | server-rendered (verify) | JS seed |
| Billing / time entry | matter picker | verify | JS seed |
| Matter dashboard | matter context | server-rendered | usually URL-scoped; low priority |

Order: **Drafting first as the proof surface**, then DMS, eDiscovery, billing.

### 4a. Drafting (React) — IMPORTANT build caveat
There is **no React build tooling on the appliance** (no `package.json`,
`vite.config.*`, or `node_modules` under `/opt/praesidium-web`). The
`static/js/drafting-home.js` bundle is pre-built and minified — **do not hand-edit
the minified file.**

Required:
1. Locate the drafting React **source** in the off-appliance frontend project
   (Dennis's dev workstation / the frontend repo used by the multi-chat build).
   Look for the `he()` root, the `K` matter-picker component, "Working Matter".
2. In the root component, replace the working-matter init:
   ```
   const seed = window.PraesidiumMatter?.getActive?.();
   const [o, n] = useState(seed ? {
     id: seed.matter_id, matter_name: seed.matter_name,
     matter_number: seed.matter_number, client_name: seed.client_name,
   } : null);
   ```
   Respect a URL `?matter_id=` if drafting supports one (that wins).
   Optionally `PraesidiumMatter.onChange` to follow topbar switches until the
   user picks on-page.
3. Rebuild the bundle in the frontend project and copy the built
   `drafting-home.js` to `/opt/praesidium-web/static/js/`. No appliance restart
   needed for a static asset; hard-refresh to bust cache.

If the source cannot be located, flag it — do not attempt to drive React state
from an external script (fragile).

---

## 5. File touch list

NEW:
- `core/services/active_matter.py` (shared reader)
- `docs/` this plan

EDIT (on-box, bind-mounted, live for existing files):
- `core/services/nav_context.py`            (inject `active_matter`)
- `modules/dashboard/routes/matter_dashboard_api.py` (call shared reader)
- `core/templates/shell.html`               (emit `window.__ACTIVE_MATTER__`)
- `static/js/matter-picker.js`              (publisher API + set global)

EDIT (OFF-box frontend repo, then rebuild + copy bundle):
- drafting React source -> rebuild -> copy `static/js/drafting-home.js`
- later: DMS / eDiscovery / billing React or server templates

---

## 6. Engineering rules (Praesidium non-negotiables)

- **asyncpg:** `TRIM(tenant_id)=:tid`; `CAST(:p AS uuid)`; never bind `None` into
  a CAST (guard the branch). Bind integer user ids as int.
- **New Python route/module load:** `docker restart praesidium-web` (uvicorn has
  no --reload); clear stale `.pyc` for touched modules (file-delete, NOT
  recursive `rm -rf` — the ops floor blocks recursive rm of protected paths).
- **Existing files** under `/opt/praesidium-web` are live via the
  bind mount to `/app`. **New files** may need `docker cp` into the container if
  not served as static under the mount — verify with
  `docker exec praesidium-web ...`.
- **Ownership:** repo files are owned by an orphaned pre-Claude-Code uid; write
  with `sudo`, then `chown` to match siblings.
- **Deploy pattern:** self-contained idempotent `/tmp/deploy_*.py`, run with
  `sudo python3`; exact single-occurrence string replacements (assert count==1);
  verify anchors live with `sed -n 'N,Mp'`; timestamped `.bak-*` backups.
- **git:** `sudo -n -u dmholmgren git` from `/datapool/opt/praesidium-web`; stage
  explicit paths; never `git add -A`; `.bak` is gitignored.
- **Frontend:** built OFF-appliance; edit source, rebuild, copy built JS to
  `static/js/`. Never edit minified bundles in place.

---

## 7. Test checklist

1. Select matter M in topbar. Open `/drafting` with no URL matter -> Working
   Matter pre-filled with M.
2. Open `/drafting?matter_id=<other>` (if supported) -> the URL matter wins,
   not M.
3. Clear active matter in topbar -> `/drafting` opens empty.
4. With drafting open and untouched, switch matter in topbar -> drafting follows
   (if onChange wired).
5. After manually choosing a matter on the drafting page, a topbar switch does
   NOT yank it (userTouched honored).
6. Stale matter (set_at > 12h): still seeds; topbar shows amber nudge.
7. Repeat 1/3 on a `base.html` page that has a picker -> async path still seeds
   (slightly after load).
8. `curl /api/v1/active-matter` still 200; no new startup errors in
   `docker logs praesidium-web`.

---

## 8. Rollback

- Per-deploy `.bak-*` backups on every touched file.
- `git` checkpoint before/after (explicit paths).
- The publisher API is additive; removing the `<script>` inject + reverting
  `matter-picker.js` returns to current behavior. Module seeds are independent
  and revert per-file.

---

## 9. Separate optional track — Registry tabs -> database (DO NOT BLOCK on this)

Moving per-module tabs from hardcoded HTML into the DB does **not** fix
pre-population (orthogonal: pre-population is state-binding, tabs are
definitions). Pursue only for its own payoff: per-tenant config, per-tab
permissions / feature-gating, consistency with the already-data-driven top nav
(`nav_service` + `ui_nav_items`).

Sketch (own session, after pre-population lands):
1. Investigate the archived `core/db/migrations/.../0020_page_registry.py` —
   reuse/extend rather than inventing a parallel table if it fits.
2. Schema `ui_tabs`: id, tenant_id, module_key, tab_key, label, route/view,
   sort_order, icon, permission_key, feature_flag, is_active.
3. `core/services/tab_service.py` mirroring `nav_service` (Redis-cached,
   tenant-scoped, graceful fallback).
4. Shared `_tabs.html` partial that loops tabs from context (like the nav rail
   loops `nav_items`).
5. Migrate module-by-module (suggest billing or contacts first), seeding rows via
   Alembic migration; replace hardcoded tab markup with the partial.
6. Add per-tab permission / feature gating.

---

*End of plan v1.*
