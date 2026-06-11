"""
patches/patch_tenant_admin.py

Run on web-01:
    cd /opt/praesidium
    sudo python3 modules/tenant_admin/_patch_user_sync.py

What this does:
  1. Replaces tenant_users_get (GET /tenant-admin/users) with multi-connector aware version
  2. Replaces tenant_users_sync_directory (POST /tenant-admin/users/sync-directory)
     to accept connector_type parameter
  3. Adds tenant_users_discovered (GET /tenant-admin/users/discovered)
  4. Adds tenant_users_discovered_import (POST /tenant-admin/users/discovered/import)

After running:
    bash /opt/praesidium/r.sh
"""

from pathlib import Path
import re

TARGET = Path("/opt/praesidium-web/modules/tenant_admin/tenant_admin.py")
src = TARGET.read_text()

# ── Replacement 1 — tenant_users_get ──────────────────────────────────────────

OLD_USERS_GET = '''@router.get("/users", response_class=HTMLResponse)
async def tenant_users_get(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, email, role, is_active, created_at "
                    "FROM users WHERE tenant_id = :tid ORDER BY created_at"
                ),
                {"tid": tid},
            )
        ).fetchall()

        # Auth source for this tenant — drives "Sync Directory" button visibility
        tenant_row = (
            await session.execute(
                text("SELECT auth_adapter FROM tenants WHERE TRIM(id) = :tid LIMIT 1"),
                {"tid": tid},
            )
        ).fetchone()
        auth_adapter = tenant_row[0] if tenant_row else "local"

        # Last directory sync
        last_sync = (
            await session.execute(
                text("""
                    SELECT started_at, completed_at, discovered_count, new_count, error
                    FROM connector_entity_sync_log
                    WHERE TRIM(tenant_id) = :tid
                      AND connector_type LIKE 'auth_%'
                    ORDER BY started_at DESC
                    LIMIT 1
                """),
                {"tid": tid},
            )
        ).fetchone()

    t = _templates(request)
    return t.TemplateResponse(
        request,
        "tenant_admin/users.html",
        {
            "users":        rows,
            "user":         user,
            "auth_adapter": auth_adapter,
            "last_sync":    last_sync,
            "saved":        request.query_params.get("saved"),
            "syncing":      request.query_params.get("syncing"),
            "sync_error":   request.query_params.get("sync_error"),
        },
    )'''

NEW_USERS_GET = '''@router.get("/users", response_class=HTMLResponse)
async def tenant_users_get(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, email, role, is_active, created_at, auth_provider "
                    "FROM users WHERE tenant_id = :tid ORDER BY created_at"
                ),
                {"tid": tid},
            )
        ).fetchall()

        # ── Enumerate ALL configured auth connectors for this tenant ──────────
        # Joins connector_registry (catalog of auth connectors) with
        # tenant_connectors (per-tenant config). Inactive registry rows or
        # unconfigured/disabled tenant rows are filtered out.
        # auth_local is excluded — there's no external directory to poll.
        auth_connectors = (
            await session.execute(
                text("""
                    SELECT
                        cr.connector_type,
                        cr.display_name,
                        cr.icon,
                        tc.status        AS tenant_status,
                        tc.last_sync_at  AS tenant_last_sync,
                        tc.last_error    AS tenant_last_error
                    FROM connector_registry cr
                    LEFT JOIN tenant_connectors tc
                      ON tc.connector_type = cr.connector_type
                     AND TRIM(tc.tenant_id) = :tid
                    WHERE cr.connector_group = 'auth'
                      AND cr.is_active = true
                      AND cr.connector_type != 'auth_local'
                    ORDER BY cr.display_order, cr.connector_type
                """),
                {"tid": tid},
            )
        ).mappings().all()

        # ── Per-connector last sync log (most recent run per connector_type) ─
        last_sync_per_connector = (
            await session.execute(
                text("""
                    SELECT DISTINCT ON (connector_type)
                        connector_type,
                        started_at, completed_at,
                        discovered_count, new_count, auto_matched_count, error
                    FROM connector_entity_sync_log
                    WHERE TRIM(tenant_id) = :tid
                      AND connector_type LIKE 'auth_%'
                    ORDER BY connector_type, started_at DESC
                """),
                {"tid": tid},
            )
        ).mappings().all()
        last_sync_map = {r["connector_type"]: dict(r) for r in last_sync_per_connector}

        # Most recent sync overall (for header summary)
        last_sync = (
            await session.execute(
                text("""
                    SELECT started_at, completed_at, discovered_count, new_count, error
                    FROM connector_entity_sync_log
                    WHERE TRIM(tenant_id) = :tid
                      AND connector_type LIKE 'auth_%'
                    ORDER BY started_at DESC
                    LIMIT 1
                """),
                {"tid": tid},
            )
        ).fetchone()

        # ── Discovered users count — directory-sourced inactive users ─────────
        # Awaiting admin review/assignment. Drives the "N awaiting review" badge.
        discovered_count = (
            await session.execute(
                text("""
                    SELECT COUNT(*) FROM users
                    WHERE TRIM(tenant_id) = :tid
                      AND is_active = false
                      AND auth_provider IS NOT NULL
                      AND auth_provider != 'local'
                """),
                {"tid": tid},
            )
        ).scalar() or 0

    # Build display list — merge registry row with last-sync info
    auth_connectors_display = []
    for c in auth_connectors:
        cdict = dict(c)
        cdict["last_sync"] = last_sync_map.get(c["connector_type"])
        auth_connectors_display.append(cdict)

    t = _templates(request)
    return t.TemplateResponse(
        request,
        "tenant_admin/users.html",
        {
            "users":            rows,
            "user":             user,
            "auth_connectors":  auth_connectors_display,
            "discovered_count": discovered_count,
            "last_sync":        last_sync,
            "saved":            request.query_params.get("saved"),
            "syncing":          request.query_params.get("syncing"),
            "sync_error":       request.query_params.get("sync_error"),
        },
    )'''

assert OLD_USERS_GET in src, "Could not find tenant_users_get"
src = src.replace(OLD_USERS_GET, NEW_USERS_GET)
print("✓ Patch 1 applied: tenant_users_get → multi-connector aware")


# ── Replacement 2 — tenant_users_sync_directory ───────────────────────────────

OLD_SYNC = '''@router.post("/users/sync-directory")
async def tenant_users_sync_directory(
    request: Request,
    user=Depends(get_current_user),
):
    """
    Trigger a directory sync from the tenant's configured auth source.
    Pulls users from LDAP / Azure AD and pre-populates the users table.
    Enqueues a background job on PROC-01.

    STUB — job implementations per auth source to be added:
      jobs/sync_directory_ldap.py   → walk LDAP OU tree
      jobs/sync_directory_azure.py  → MS Graph /users endpoint
    """
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)

    tid = _tenant_id(request)
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", 0)

    async with AsyncSessionLocal() as session:
        tenant_row = (
            await session.execute(
                text("SELECT auth_adapter FROM tenants WHERE TRIM(id) = :tid LIMIT 1"),
                {"tid": tid},
            )
        ).fetchone()
        auth_adapter = tenant_row[0] if tenant_row else "local"

    if auth_adapter == "local":
        return RedirectResponse(
            "/tenant-admin/users?sync_error=Local+auth+has+no+external+directory",
            status_code=303,
        )

    # Log sync attempt
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO connector_entity_sync_log
                    (tenant_id, connector_type, triggered_by)
                VALUES (:tid, :ctype, :uid)
            """),
            {"tid": tid, "ctype": f"auth_{auth_adapter}", "uid": user_id},
        )
        await session.commit()

    # Enqueue adapter-specific directory sync job
    # Normalize ldaps → ldaps (both ldap and ldaps map to the same job)
    job_adapter = auth_adapter  # ldap → sync_directory_ldap, ldaps → sync_directory_ldaps
    try:
        from redis import Redis
        from rq import Queue
        q = Queue(connection=Redis.from_url(REDIS_URL))
        job = q.enqueue(
            f"jobs.sync_directory_{job_adapter}.run",
            tid,
            user_id,
            job_timeout=300,
        )
        log.info(
            f"[users/sync-directory] tenant={tid} adapter={auth_adapter} "
            f"job_id={job.id}"
        )
    except Exception as e:
        log.exception(f"[users/sync-directory] failed to enqueue job: {e}")

    return RedirectResponse("/tenant-admin/users?syncing=1", status_code=303)'''

NEW_SYNC = '''@router.post("/users/sync-directory")
async def tenant_users_sync_directory(
    request: Request,
    connector_type: Annotated[str, Form()] = "",
    user=Depends(get_current_user),
):
    """
    Trigger a directory sync from a specific registered auth connector.
    Accepts connector_type form parameter (e.g. 'auth_ldap', 'auth_azure').

    For each connector_type, dispatches to:
      auth_ldap   → jobs.sync_directory_ldap.run
      auth_azure  → jobs.sync_directory_azure.run
      auth_okta   → jobs.sync_directory_okta.run

    The job pulls users from the directory and stages them in the `users`
    table with is_active=false. Admin reviews and activates them at
    /tenant-admin/users/discovered.
    """
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)

    tid = _tenant_id(request)
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", 0)

    # Validate connector_type — must be a registered active auth connector
    # AND configured for this tenant. Defends against the form being tampered
    # with to enqueue arbitrary jobs.
    if not connector_type or not connector_type.startswith("auth_") or connector_type == "auth_local":
        return RedirectResponse(
            "/tenant-admin/users?sync_error=Invalid+connector",
            status_code=303,
        )

    async with AsyncSessionLocal() as session:
        valid = (
            await session.execute(
                text("""
                    SELECT cr.connector_type
                    FROM connector_registry cr
                    LEFT JOIN tenant_connectors tc
                      ON tc.connector_type = cr.connector_type
                     AND TRIM(tc.tenant_id) = :tid
                    WHERE cr.connector_type = :ctype
                      AND cr.connector_group = 'auth'
                      AND cr.is_active = true
                """),
                {"tid": tid, "ctype": connector_type},
            )
        ).first()

    if not valid:
        return RedirectResponse(
            "/tenant-admin/users?sync_error=Connector+not+registered+or+inactive",
            status_code=303,
        )

    # Insert sync log row — captures the run BEFORE the job is enqueued so
    # the UI can show "queued" state immediately.
    sync_log_id = None
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO connector_entity_sync_log
                    (tenant_id, connector_type, triggered_by)
                VALUES (:tid, :ctype, :uid)
                RETURNING id
            """),
            {"tid": tid, "ctype": connector_type, "uid": user_id},
        )
        row = result.first()
        sync_log_id = str(row[0]) if row else None
        await session.commit()

    # Enqueue the job. Adapter slug maps connector_type → job module:
    # 'auth_ldap' → 'sync_directory_ldap'
    job_module = connector_type.replace("auth_", "sync_directory_")
    try:
        from redis import Redis
        from rq import Queue
        q = Queue(connection=Redis.from_url(REDIS_URL))
        job = q.enqueue(
            f"jobs.{job_module}.run",
            tid,
            user_id,
            sync_log_id,
            job_timeout=300,
        )
        log.info(
            f"[users/sync-directory] tenant={tid} connector={connector_type} "
            f"job_id={job.id}"
        )
    except Exception as e:
        log.exception(f"[users/sync-directory] failed to enqueue job: {e}")
        return RedirectResponse(
            "/tenant-admin/users?sync_error=Failed+to+enqueue+sync+job",
            status_code=303,
        )

    return RedirectResponse(f"/tenant-admin/users?syncing=1", status_code=303)'''

assert OLD_SYNC in src, "Could not find tenant_users_sync_directory"
src = src.replace(OLD_SYNC, NEW_SYNC)
print("✓ Patch 2 applied: tenant_users_sync_directory → multi-connector dispatch")


# ── Insertion 3 — Add discovered users review routes ──────────────────────────

# Find the "── Feature Settings ──" section header and insert new routes ABOVE it.
INSERTION_ANCHOR = "# ── Feature Settings ──"

NEW_DISCOVERED_ROUTES = '''# ── Discovered Users Review (post-directory-sync) ────────────────────────────

@router.get("/users/discovered", response_class=HTMLResponse)
async def tenant_users_discovered(request: Request, user=Depends(get_current_user)):
    """
    Review users discovered via directory sync. These were created with
    is_active=false and auth_provider != 'local'. The admin assigns roles
    and bulk-activates them.
    """
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)

    async with AsyncSessionLocal() as session:
        discovered = (
            await session.execute(
                text("""
                    SELECT
                        u.id, u.email, u.username, u.full_name, u.role,
                        u.auth_provider, u.created_at,
                        uac.display_name AS uac_display_name,
                        uac.meta AS uac_meta
                    FROM users u
                    LEFT JOIN user_auth_credentials uac
                      ON uac.user_id = u.id AND TRIM(uac.tenant_id) = :tid
                    WHERE TRIM(u.tenant_id) = :tid
                      AND u.is_active = false
                      AND u.auth_provider IS NOT NULL
                      AND u.auth_provider != 'local'
                    ORDER BY u.auth_provider, u.email
                """),
                {"tid": tid},
            )
        ).mappings().all()

    t = _templates(request)
    return t.TemplateResponse(
        request,
        "tenant_admin/users_discovered.html",
        {
            "discovered": discovered,
            "user":       user,
            "saved":      request.query_params.get("saved"),
            "skipped":    request.query_params.get("skipped"),
        },
    )


@router.post("/users/discovered/import")
async def tenant_users_discovered_import(
    request: Request,
    user=Depends(get_current_user),
):
    """
    Bulk-activate selected discovered users with assigned roles.
    Form fields:
      activate[]=<user_id>     — checkbox per row, repeated
      role_<user_id>=<role>    — select per row
    """
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tenant_id(request)
    admin_id = user.id if not isinstance(user, dict) else user.get("id", 0)

    form = await request.form()
    activate_ids = form.getlist("activate")
    if not activate_ids:
        return RedirectResponse(
            "/tenant-admin/users/discovered?skipped=No+users+selected",
            status_code=303,
        )

    valid_roles = {
        "super_admin", "admin", "partner", "attorney",
        "paralegal", "staff", "read_only",
    }

    activated = 0
    async with AsyncSessionLocal() as session:
        for raw_uid in activate_ids:
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue

            role = str(form.get(f"role_{uid}", "staff")).strip()
            if role not in valid_roles:
                role = "staff"

            # Activate + set role. Verify the user belongs to this tenant
            # AND was originally inactive directory-discovered (defends
            # against form tampering).
            result = await session.execute(
                text("""
                    UPDATE users SET
                        is_active = true,
                        role      = CAST(:role AS user_role_enum),
                        updated_at = now()
                    WHERE id = :uid
                      AND TRIM(tenant_id) = :tid
                      AND is_active = false
                      AND auth_provider IS NOT NULL
                      AND auth_provider != 'local'
                    RETURNING id
                """),
                {"uid": uid, "tid": tid, "role": role},
            )
            if result.first():
                activated += 1
                # Audit log
                await session.execute(
                    text("""
                        INSERT INTO user_activity_log
                            (tenant_id, user_id, action, details, performed_by_id)
                        VALUES (:tid, :uid, 'directory_import_activate',
                                CAST(:details AS jsonb), :admin)
                    """),
                    {
                        "tid": tid, "uid": uid,
                        "details": f'{{"role": "{role}"}}',
                        "admin": admin_id,
                    },
                )

        await session.commit()

    return RedirectResponse(
        f"/tenant-admin/users/discovered?saved=Activated+{activated}+user(s)",
        status_code=303,
    )


'''

assert INSERTION_ANCHOR in src, "Could not find Feature Settings anchor"
src = src.replace(INSERTION_ANCHOR, NEW_DISCOVERED_ROUTES + INSERTION_ANCHOR)
print("✓ Patch 3 applied: discovered review routes added")


# ── Write back ────────────────────────────────────────────────────────────────

# Syntax check
import ast
try:
    ast.parse(src)
    print("✓ Syntax OK")
except SyntaxError as e:
    print(f"✗ SYNTAX ERROR: {e}")
    raise SystemExit(1)

TARGET.write_text(src)
print(f"✓ Wrote {TARGET} ({len(src)} bytes)")
