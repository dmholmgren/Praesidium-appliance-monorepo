"""
registry_router.py — M10 Connector Registry Router
Generic GET/POST routes for all connector configure pages.
Connectors are DATA not code — this file never changes when new connectors are added.

Routes:
  GET  /tenant-admin/connectors                              — list all available connectors
  GET  /tenant-admin/connectors/{connector_type}/configure   — render config form from registry
  POST /tenant-admin/connectors/{connector_type}/configure   — save config
  GET  /tenant-admin/connectors/{connector_type}/status      — JSON status
  POST /tenant-admin/connectors/{connector_type}/toggle      — enable/disable

  GET  /tenant-admin/folder-reconciliation/clients           — JSON: all clients
  GET  /tenant-admin/folder-reconciliation/matters           — JSON: matters for client
  GET  /tenant-admin/folder-reconciliation                   — Import Reconciliation page
  POST /tenant-admin/folder-reconciliation/accept-batch      — batch accept into matter_folders

  GET  /tenant-admin/client-merge                            — Client Merge screen
  GET  /tenant-admin/client-merge/preview                    — JSON: preview merge (matter count)
  POST /tenant-admin/client-merge/execute                    — execute merge

Architectural constraints enforced:
  - AsyncSessionLocal() only
  - trim(tenant_id) in all WHERE clauses
  - tenant_connectors uses 'connector' and 'is_active' columns
  - credentials_vault uses 'encrypted_key' column
  - CAST(:value AS jsonb) not CAST(:value AS jsonb)
  - Templates from ui_templates DB table, filesystem fallback
  - All AI calls via AIService (none here)
  - BrandingService for all branding
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# credentials_vault crypto — Fernet symmetric encryption keyed off SECRET_KEY.
# Matches the pattern in modules/tenant_admin/tenant_admin.py::tenant_byok_post
# so that BYOK and connector credential rows use the same ciphertext format.
# ---------------------------------------------------------------------------

import base64 as _vault_base64
import os as _vault_os


def _vault_get_fernet():
    """Return a Fernet keyed off SECRET_KEY (env). Same derivation everywhere."""
    from cryptography.fernet import Fernet
    secret = _vault_os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    key_bytes = (secret[:32]).encode().ljust(32, b"0")
    fernet_key = _vault_base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def _vault_encrypt(plaintext: str) -> str:
    """Encrypt a credential before INSERT into credentials_vault.encrypted_key."""
    if plaintext is None:
        return ""
    f = _vault_get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def _vault_decrypt(stored: str) -> str:
    """
    Decrypt a credentials_vault.encrypted_key value.
    Tries Fernet first; falls back to returning the raw value as plaintext
    if decrypt fails. The fallback exists ONLY for transition — once all
    rows are re-saved through the patched writer, the fallback is dead code.
    """
    if not stored:
        return ""
    try:
        f = _vault_get_fernet()
        return f.decrypt(stored.encode()).decode()
    except Exception:
        # Legacy plaintext row from before the encryption fix.
        # Log at info level so we can see how many rows still need re-saving.
        logger.info("_vault_decrypt: row appears to be plaintext (pre-encryption fix)")
        return stored



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_registry_entry(session, connector_type: str) -> Optional[dict]:
    result = await session.execute(
        text("""
            SELECT connector_type, display_name, description, icon, sync_type,
                   config_fields, credential_fields, schedule_options, ingest_endpoint,
                   is_active
            FROM connector_registry
            WHERE connector_type = :ct
        """),
        {"ct": connector_type},
    )
    row = result.mappings().fetchone()
    if not row:
        return None
    entry = dict(row)
    for col in ("config_fields", "credential_fields", "schedule_options"):
        val = entry.get(col)
        if isinstance(val, str):
            entry[col] = json.loads(val) if val else []
        elif val is None:
            entry[col] = []
    return entry


async def _get_tenant_connector(session, tenant_id: str, connector_type: str):
    """
    Get the tenant's instance of a connector.
    Returns dict with {id, connector, config, is_active, last_sync_at} or None.
    """
    import json
    from sqlalchemy import text

    result = await session.execute(
        text("""
            SELECT id, connector, config, is_active, last_sync_at
            FROM tenant_connectors
            WHERE trim(tenant_id) = trim(:tid)
              AND connector = :ct
            LIMIT 1
        """),
        {"tid": tenant_id, "ct": connector_type},
    )
    row = result.mappings().fetchone()
    if not row:
        return None
    rec = dict(row)
    config = rec.get("config")
    if isinstance(config, str):
        rec["config"] = json.loads(config) if config else {}
    elif config is None:
        rec["config"] = {}
    return rec


async def _get_credential(session, tenant_id: str, key_name: str) -> str | None:
    """
    Retrieve a credential from credentials_vault. Decrypts via Fernet.

    key_name format from the router: "{connector_type}.{field_name}"
    Maps to: provider=connector_type, key_type=field_name
    """
    from sqlalchemy import text

    # Parse "auth_ldap.bind_password" → provider="auth_ldap", key_type="bind_password"
    if "." in key_name:
        provider, key_type = key_name.split(".", 1)
    else:
        provider = key_name
        key_type = key_name

    result = await session.execute(
        text("""
            SELECT encrypted_key
            FROM credentials_vault
            WHERE trim(tenant_id) = trim(:tid)
              AND provider = :provider
              AND key_type = :key_type
            LIMIT 1
        """),
        {"tid": tenant_id, "provider": provider, "key_type": key_type},
    )
    row = result.fetchone()
    if not row:
        return None
    return _vault_decrypt(row[0])


async def _upsert_tenant_connector(session, tenant_id: str, connector_type: str, config: dict, is_active: bool):
    """
    Upsert a tenant connector row.

    No unique constraint on (tenant_id, connector), so we do SELECT-then-INSERT/UPDATE.
    """
    import json
    from sqlalchemy import text

    existing = await session.execute(
        text("""
            SELECT id FROM tenant_connectors
            WHERE trim(tenant_id) = trim(:tid) AND connector = :ct
            LIMIT 1
        """),
        {"tid": tenant_id, "ct": connector_type},
    )
    row = existing.fetchone()

    if row:
        await session.execute(
            text("""
                UPDATE tenant_connectors
                SET config = CAST(:cfg AS jsonb),
                    is_active = :active,
                    status = 'configured',
                    updated_at = NOW()
                WHERE id = :row_id
            """),
            {"cfg": json.dumps(config), "active": is_active, "row_id": row[0]},
        )
    else:
        await session.execute(
            text("""
                INSERT INTO tenant_connectors
                    (tenant_id, connector, connector_type, config, is_active, status, sync_frequency, created_at, updated_at)
                VALUES
                    (:tid, :ct, :ct, CAST(:cfg AS jsonb), :active, 'configured', 'manual', NOW(), NOW())
            """),
            {"tid": tenant_id, "ct": connector_type, "cfg": json.dumps(config), "active": is_active},
        )


async def _upsert_credential(session, tenant_id: str, key_name: str, value: str):
    """
    Upsert a credential into credentials_vault.

    key_name format from the router: "{connector_type}.{field_name}"
    Maps to: provider=connector_type, key_type=field_name

    Uses ON CONFLICT on the unique index (tenant_id, provider, key_type).

    Fernet-encrypts the value before insert. Mirrors the BYOK route's
    encryption pattern — column `encrypted_key` holds Fernet ciphertext.
    """
    from sqlalchemy import text

    if "." in key_name:
        provider, key_type = key_name.split(".", 1)
    else:
        provider = key_name
        key_type = key_name

    encrypted = _vault_encrypt(value)

    await session.execute(
        text("""
            INSERT INTO credentials_vault (tenant_id, provider, key_type, encrypted_key, updated_at)
            VALUES (:tid, :provider, :key_type, :val, NOW())
            ON CONFLICT (tenant_id, provider, key_type)
            DO UPDATE SET encrypted_key = :val, updated_at = NOW()
        """),
        {"tid": tenant_id, "provider": provider, "key_type": key_type, "val": encrypted},
    )



async def _get_template(session, template_name: str) -> Optional[str]:
    result = await session.execute(
        text("""
            SELECT content FROM ui_templates
            WHERE template_name = :tn AND is_active = TRUE
            ORDER BY version DESC LIMIT 1
        """),
        {"tn": template_name},
    )
    row = result.fetchone()
    return row[0] if row else None


async def _render_template(request: Request, template_name: str, context: dict) -> str:
    async with AsyncSessionLocal() as session:
        db_content = await _get_template(session, template_name)

    if db_content:
        try:
            templates_env = request.app.state.templates
            tmpl = templates_env.env.from_string(db_content)
            return tmpl.render(**context)
        except Exception as exc:
            logger.warning("DB template render failed for %s: %s — falling back", template_name, exc)

    templates = request.app.state.templates
    tmpl = templates.get_template(template_name)
    return tmpl.render(**context)


def _build_context(request, user, entry, tenant_connector, branding=None):
    existing_config = tenant_connector.get("config", {}) if tenant_connector else {}
    is_active = tenant_connector.get("is_active", False) if tenant_connector else False
    return {
        "request": request, "user": user, "branding": branding,
        "connector": entry, "existing_config": existing_config,
        "is_active": is_active,
        "last_sync_at": tenant_connector.get("last_sync_at") if tenant_connector else None,
        "flash": request.session.get("flash") if hasattr(request, "session") else None,
    }


# ---------------------------------------------------------------------------
# Connector routes
# ---------------------------------------------------------------------------

@router.get("/tenant-admin/connectors", response_class=HTMLResponse)
async def list_connectors(request: Request, user=Depends(get_current_user)):
    tenant_id = user.tenant_id
    async with AsyncSessionLocal() as session:
        reg_result = await session.execute(text("""
            SELECT connector_type, display_name, description, icon, sync_type, is_active
            FROM connector_registry ORDER BY display_name
        """))
        registry = [dict(r) for r in reg_result.mappings().fetchall()]
        tc_result = await session.execute(text("""
            SELECT connector, is_active, last_sync_at FROM tenant_connectors
            WHERE trim(tenant_id) = trim(:tid)
        """), {"tid": tenant_id})
        tenant_map = {r["connector"]: dict(r) for r in tc_result.mappings().fetchall()}

    for entry in registry:
        tc = tenant_map.get(entry["connector_type"])
        entry["tenant_enabled"] = tc["is_active"] if tc else False
        entry["last_sync_at"] = tc["last_sync_at"] if tc else None

    branding = getattr(request.state, "branding", None)
    try:
        html = await _render_template(request, "tenant_admin/connector_list.html",
            {"request": request, "user": user, "branding": branding, "registry": registry})
        return HTMLResponse(html)
    except Exception as exc:
        logger.error("connector list render error: %s", exc)
        raise HTTPException(status_code=500, detail="Template render failed") from exc


@router.get("/tenant-admin/connectors/{connector_type}/configure", response_class=HTMLResponse)
async def configure_connector_get(connector_type: str, request: Request, user=Depends(get_current_user)):
    tenant_id = user.tenant_id
    async with AsyncSessionLocal() as session:
        entry = await _get_registry_entry(session, connector_type)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Connector '{connector_type}' not found")
        tenant_connector = await _get_tenant_connector(session, tenant_id, connector_type)
        if tenant_connector:
            for field in entry.get("credential_fields", []):
                fname = field.get("name")
                if fname and fname in tenant_connector["config"]:
                    tenant_connector["config"][fname] = "••••••••"
    branding = getattr(request.state, "branding", None)
    context = _build_context(request, user, entry, tenant_connector, branding)
    try:
        html = await _render_template(request, "tenant_admin/connector_configure.html", context)
        return HTMLResponse(html)
    except Exception as exc:
        logger.error("configure GET render error [%s]: %s", connector_type, exc)
        raise HTTPException(status_code=500, detail="Template render failed") from exc


@router.post("/tenant-admin/connectors/{connector_type}/configure")
async def configure_connector_post(connector_type: str, request: Request, user=Depends(get_current_user)):
    tenant_id = user.tenant_id
    form = await request.form()
    async with AsyncSessionLocal() as session:
        entry = await _get_registry_entry(session, connector_type)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Connector '{connector_type}' not found")
        credential_field_names = {f["name"] for f in entry.get("credential_fields", []) if "name" in f}
        config_field_names = {f["name"] for f in entry.get("config_fields", []) if "name" in f}
        config_data: dict = {}
        credential_data: dict = {}
        for field_name in config_field_names:
            val = form.get(field_name, "").strip()
            if val:
                config_data[field_name] = val
        for field_name in credential_field_names:
            val = form.get(field_name, "").strip()
            if val and val != "••••••••":
                credential_data[field_name] = val
        is_active = form.get("is_active", "off") in ("on", "true", "1", "yes")
        try:
            await _upsert_tenant_connector(session, tenant_id, connector_type, config_data, is_active)
            for key_name, secret_value in credential_data.items():
                await _upsert_credential(session, tenant_id, f"{connector_type}.{key_name}", secret_value)
            await session.commit()
            logger.info("Connector %s configured for tenant %s", connector_type, tenant_id.strip())
        except Exception as exc:
            await session.rollback()
            logger.error("configure POST error [%s]: %s", connector_type, exc)
            raise HTTPException(status_code=500, detail="Failed to save connector configuration") from exc
    return RedirectResponse(url=f"/tenant-admin/connectors/{connector_type}/configure?saved=1", status_code=303)


@router.get("/tenant-admin/connectors/{connector_type}/status")
async def connector_status(connector_type: str, request: Request, user=Depends(get_current_user)):
    tenant_id = user.tenant_id
    async with AsyncSessionLocal() as session:
        entry = await _get_registry_entry(session, connector_type)
        if not entry:
            return JSONResponse({"error": "not_found"}, status_code=404)
        tenant_connector = await _get_tenant_connector(session, tenant_id, connector_type)
    return JSONResponse({
        "connector_type": connector_type,
        "display_name": entry["display_name"],
        "sync_type": entry["sync_type"],
        "registry_active": entry["is_active"],
        "tenant_enabled": tenant_connector["is_active"] if tenant_connector else False,
        "last_sync_at": str(tenant_connector["last_sync_at"]) if tenant_connector and tenant_connector.get("last_sync_at") else None,
        "configured": tenant_connector is not None,
    })


@router.post("/tenant-admin/connectors/{connector_type}/toggle")
async def toggle_connector(connector_type: str, request: Request, user=Depends(get_current_user)):
    tenant_id = user.tenant_id
    async with AsyncSessionLocal() as session:
        entry = await _get_registry_entry(session, connector_type)
        if not entry:
            raise HTTPException(status_code=404, detail="Connector not found")
        tenant_connector = await _get_tenant_connector(session, tenant_id, connector_type)
        if not tenant_connector:
            raise HTTPException(status_code=400, detail="Connector not configured — save configuration first")
        new_state = not tenant_connector["is_active"]
        try:
            await session.execute(text("""
                UPDATE tenant_connectors SET is_active = :active, updated_at = NOW()
                WHERE trim(tenant_id) = trim(:tid) AND connector = :ct
            """), {"active": new_state, "tid": tenant_id, "ct": connector_type})
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.error("toggle error [%s]: %s", connector_type, exc)
            raise HTTPException(status_code=500, detail="Toggle failed") from exc
    return JSONResponse({"connector_type": connector_type, "is_active": new_state,
                         "message": "enabled" if new_state else "disabled"})


# ---------------------------------------------------------------------------
# Import Reconciliation
# Static sub-paths registered BEFORE the dynamic GET route.
# ---------------------------------------------------------------------------


@router.get("/tenant-admin/folder-reconciliation/browse")
async def browse_folders(
    request: Request, user=Depends(get_current_user),
    share: str = "Clients", prefix: str = "",
):
    """List folders/files on a storage mount for the browser modal.
    Reads directly via LocalMountStorageAdapter — no CIFS bridge dependency.
 
    Path contract (matches prior bridge behavior):
      - Caller passes `share` (Clients | DocSend | Praesidium | eDiscovery) and
        a share-relative `prefix` (e.g. "SomeClient/Matter 2024-001").
      - Response folders/files carry share-relative paths (same shape as prefix),
        so the HTMX template can concatenate child names and recurse.
      - parent_path is included so the template can render a "← Back" row.
    """
    from modules.dms.adapters.local_mount_storage import get_storage_adapter
 
    tenant_id = user.tenant_id
    storage = get_storage_adapter()
 
    # Map share name to adapter mount prefix
    share_norm = (share or "").lower().strip().strip("/")
    if share_norm in ("", "clients"):
        mount_prefix = "clients"
    elif share_norm in ("docsend", "praesidium", "ediscovery"):
        mount_prefix = share_norm
    else:
        return JSONResponse({"folders": [], "files": [], "prefix": prefix, "parent_path": ""})
 
    # Normalize the caller-supplied share-relative prefix
    prefix_clean = (prefix or "").strip("/")
 
    # Build the logical path the adapter wants: "<mount>/<relative>" or just "<mount>"
    logical_path = f"{mount_prefix}/{prefix_clean}" if prefix_clean else mount_prefix
 
    try:
        entries = await storage.list(tenant_id, logical_path, recursive=False)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("browse_folders error share=%s prefix=%s: %s",
                                          share, prefix, exc)
        return JSONResponse({"folders": [], "files": [],
                             "prefix": prefix, "parent_path": "",
                             "error": str(exc)})
 
    include_files = request.query_params.get("include_files", "false").lower() == "true"
 
    # Adapter returns paths of the form "<mount>/<relative-from-mount>".
    # Strip the leading mount so the caller sees share-relative paths.
    mount_strip = f"{mount_prefix}/"
 
    def to_share_relative(p: str) -> str:
        p = (p or "").lstrip("/")
        if p.startswith(mount_strip):
            return p[len(mount_strip):]
        # Exact match of the mount root → empty relative path
        if p == mount_prefix:
            return ""
        return p
 
    folders = [
        {
            "path": to_share_relative(e.path),
            "name": e.name,
            "modified_at": e.modified_at or "",
            "is_directory": True,
        }
        for e in entries if e.is_directory
    ]
    files = [
        {
            "path": to_share_relative(e.path),
            "name": e.name,
            "modified_at": e.modified_at or "",
            "size": e.size,
            "is_directory": False,
        }
        for e in entries if not e.is_directory
    ] if include_files else []
 
    # Parent-path hint for the "← Back" affordance in the HTMX template.
    if prefix_clean:
        parent_path = "/".join(prefix_clean.split("/")[:-1])
    else:
        parent_path = ""
 
    return JSONResponse({
        "folders": folders,
        "files": files,
        "prefix": prefix_clean,
        "parent_path": parent_path,
        "share": share,
    })

    return JSONResponse({"folders": folders, "files": files})


@router.post("/tenant-admin/folder-reconciliation/accept-all")
async def accept_all_pending(request: Request, user=Depends(get_current_user)):
    """Accept all pending (non-accepted) folder matches."""
    tenant_id = user.tenant_id
    async with AsyncSessionLocal() as session:
        r = await session.execute(text("""
            UPDATE dms_folder_matches
            SET accepted = TRUE
            WHERE trim(tenant_id) = trim(:tid) AND accepted = FALSE
              AND best_disk_path IS NOT NULL
            RETURNING matter_id::text, best_disk_path, disk_root, disk_file_count
        """), {"tid": tenant_id})
        rows = r.fetchall()

        accepted = 0
        for row in rows:
            matter_id, disk_path, disk_root, file_count = row
            if not disk_path: continue
            await session.execute(text("""
                INSERT INTO matter_folders (id, tenant_id, matter_id, folder_path, disk_root, file_count, added_at, added_by)
                VALUES (gen_random_uuid(), :tid, CAST(:mid AS uuid), :fp, :dr, :fc, NOW(), :uid)
                ON CONFLICT (matter_id, folder_path) DO NOTHING
            """), {"tid": tenant_id, "mid": matter_id, "fp": disk_path,
                   "dr": disk_root, "fc": file_count, "uid": user.id})
            accepted += 1

        await session.commit()
    return JSONResponse({"status": "ok", "accepted": accepted})


@router.delete("/tenant-admin/folder-reconciliation/remove-mapping/{mapping_id}")
async def remove_folder_mapping(
    request: Request, user=Depends(get_current_user), mapping_id: str = ""
):
    """Remove a matter_folders mapping row."""
    tenant_id = user.tenant_id
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            DELETE FROM matter_folders
            WHERE id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)
        """), {"mid": mapping_id, "tid": tenant_id})
        await session.commit()
    return JSONResponse({"status": "ok"})



@router.get("/tenant-admin/folder-reconciliation/mapped-index")
async def mapped_folder_index(request: Request, user=Depends(get_current_user)):
    """Return all mapped folder paths for the current tenant for tree coloring."""
    tenant_id = user.tenant_id
    async with AsyncSessionLocal() as session:
        r = await session.execute(text("""
            SELECT mf.folder_path, mf.id::text as mapping_id,
                   m.matter_name, m.id::text as matter_id
            FROM matter_folders mf
            JOIN matters m ON m.id = mf.matter_id
            WHERE trim(mf.tenant_id) = trim(:tid)
              AND mf.disk_root IS NOT NULL
            ORDER BY mf.folder_path
        """), {"tid": tenant_id})
        mappings = [dict(r) for r in r.mappings().fetchall()]
    return JSONResponse({"mappings": mappings})


@router.get("/tenant-admin/folder-reconciliation/search-matters")
async def search_matters_fr(
    request: Request, user=Depends(get_current_user), q: str = ""
):
    """Search matters for folder assignment typeahead."""
    tenant_id = user.tenant_id
    if not q:
        return JSONResponse({"matters": []})
    async with AsyncSessionLocal() as session:
        r = await session.execute(text("""
            SELECT m.id::text, m.matter_name, m.matter_number,
                   c.client_name,
                   COUNT(mf.id) as folder_count
            FROM matters m
            LEFT JOIN clients c ON c.id = m.client_id
            LEFT JOIN matter_folders mf ON mf.matter_id = m.id
              AND trim(mf.tenant_id) = trim(:tid)
              AND mf.disk_root IS NOT NULL
            WHERE trim(m.tenant_id) = trim(:tid)
              AND m.status = 'active'
              AND (
                m.matter_name ILIKE :q
                OR m.matter_number ILIKE :q
                OR c.client_name ILIKE :q
              )
            GROUP BY m.id, m.matter_name, m.matter_number, c.client_name
            ORDER BY
              CASE WHEN LOWER(m.matter_name) LIKE LOWER(:eq) THEN 0
                   WHEN LOWER(c.client_name) LIKE LOWER(:eq) THEN 1
                   ELSE 2 END,
              m.matter_name
            LIMIT 20
        """), {"tid": tenant_id, "q": f"%{q}%", "eq": f"{q}%"})
        matters = [dict(r) for r in r.mappings().fetchall()]
    return JSONResponse({"matters": matters})


@router.get("/tenant-admin/folder-reconciliation/matter-mappings/{matter_id}")
async def matter_folder_mappings(
    request: Request, user=Depends(get_current_user), matter_id: str = ""
):
    """Get all folder mappings for a specific matter."""
    tenant_id = user.tenant_id
    async with AsyncSessionLocal() as session:
        r = await session.execute(text("""
            SELECT id::text, folder_path, disk_root, file_count, accepted
            FROM matter_folders
            WHERE matter_id = CAST(:mid AS uuid)
              AND trim(tenant_id) = trim(:tid)
              AND disk_root IS NOT NULL
            ORDER BY added_at DESC
        """), {"mid": matter_id, "tid": tenant_id})
        mappings = [dict(r) for r in r.mappings().fetchall()]
    return JSONResponse({"mappings": mappings})

@router.get("/tenant-admin/folder-reconciliation/clients")
async def reconciliation_clients(request: Request, user=Depends(get_current_user)):
    """All active clients for tenant — populates client dropdown."""
    tenant_id = user.tenant_id
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("""
            SELECT id::text, client_name, client_number
            FROM clients
            WHERE trim(tenant_id) = trim(:tid) AND is_active = TRUE
            ORDER BY client_name ASC
        """), {"tid": tenant_id})
        clients = [{"id": r[0], "name": r[1], "number": r[2]} for r in result.fetchall()]
    return JSONResponse({"clients": clients})


@router.get("/tenant-admin/folder-reconciliation/matters")
async def reconciliation_matters(request: Request, user=Depends(get_current_user), client_id: str = ""):
    """Matters for a client — populates matter dropdown."""
    tenant_id = user.tenant_id
    async with AsyncSessionLocal() as session:
        if client_id:
            result = await session.execute(text("""
                SELECT id::text, matter_name, matter_number
                FROM matters
                WHERE trim(tenant_id) = trim(:tid)
                  AND client_id = CAST(:cid AS uuid)
                ORDER BY matter_name ASC
            """), {"tid": tenant_id, "cid": client_id})
        else:
            result = await session.execute(text("""
                SELECT id::text, matter_name, matter_number
                FROM matters
                WHERE trim(tenant_id) = trim(:tid)
                ORDER BY matter_name ASC LIMIT 200
            """), {"tid": tenant_id})
        matters = [{"id": r[0], "name": r[1], "number": r[2]} for r in result.fetchall()]
    return JSONResponse({"matters": matters})


@router.get("/tenant-admin/folder-reconciliation", response_class=HTMLResponse)
async def folder_reconciliation(
    request: Request, user=Depends(get_current_user),
    page: int = 1, q: str = "",
):
    from collections import defaultdict
    tenant_id = user.tenant_id
    per_page  = 30  # clients per page

    async with AsyncSessionLocal() as session:
        # Total active clients with matters
        tc = await session.execute(text("""
            SELECT COUNT(DISTINCT c.id) FROM clients c
            JOIN matters m ON m.client_id = c.id AND trim(m.tenant_id) = trim(:tid)
            WHERE trim(c.tenant_id) = trim(:tid) AND c.is_active = TRUE
              AND c.merged_into_client_id IS NULL
        """), {"tid": tenant_id})
        total_clients = tc.scalar() or 0

        # Overall stats
        stats = await session.execute(text("""
            SELECT
                COUNT(DISTINCT mf.matter_id) as mapped_count,
                COUNT(*) as total_mappings
            FROM matter_folders mf
            WHERE trim(mf.tenant_id) = trim(:tid) AND mf.disk_root IS NOT NULL
        """), {"tid": tenant_id})
        srow = stats.fetchone()
        mapped_count = srow[0] or 0

        tm = await session.execute(text("""
            SELECT COUNT(*) FROM matters WHERE trim(tenant_id)=trim(:tid) AND status='active'
        """), {"tid": tenant_id})
        total_matters = tm.scalar() or 0
        unmapped_count = total_matters - mapped_count

        # Paginated clients
        offset = (page - 1) * per_page
        qclause = ""
        params = {"tid": tenant_id}
        if q:
            qclause = " AND (LOWER(c.client_name) LIKE :q OR LOWER(m.matter_name) LIKE :q)"
            params["q"] = f"%{q.lower()}%"

        clients_r = await session.execute(text(f"""
            SELECT DISTINCT c.id::text, c.client_name
            FROM clients c
            JOIN matters m ON m.client_id = c.id AND trim(m.tenant_id) = trim(:tid)
            WHERE trim(c.tenant_id) = trim(:tid) AND c.is_active = TRUE
              AND c.merged_into_client_id IS NULL
              {qclause}
            ORDER BY c.client_name
            LIMIT {per_page} OFFSET {offset}
        """), params)
        client_rows = [{"id": r[0], "client_name": r[1]} for r in clients_r.fetchall()]

        if not client_rows:
            clients_out = []
        else:
            cids = [r["id"] for r in client_rows]
            ph   = ", ".join([f"CAST(:c{i} AS uuid)" for i in range(len(cids))])
            cparams = {f"c{i}": cid for i, cid in enumerate(cids)}

            # Get all matters for these clients
            matters_r = await session.execute(text(f"""
                SELECT m.id::text, m.matter_name, m.matter_number,
                       m.client_id::text, m.status
                FROM matters m
                WHERE m.client_id IN ({ph})
                  AND trim(m.tenant_id) = trim(:tid)
                  AND m.status = 'active'
                ORDER BY m.matter_name
            """), {**cparams, "tid": tenant_id})
            all_matters = [dict(r) for r in matters_r.mappings().fetchall()]

            # Get all folder mappings for these matters
            mids = [m["id"] for m in all_matters]
            if mids:
                mph = ", ".join([f"CAST(:m{i} AS uuid)" for i in range(len(mids))])
                mparams = {f"m{i}": mid for i, mid in enumerate(mids)}

                # From matter_folders (accepted mappings)
                mf_r = await session.execute(text(f"""
                    SELECT id::text, matter_id::text, folder_path, disk_root,
                           file_count, TRUE as accepted
                    FROM matter_folders
                    WHERE matter_id IN ({mph})
                      AND trim(tenant_id) = trim(:tid)
                      AND disk_root IS NOT NULL
                    ORDER BY added_at
                """), {**mparams, "tid": tenant_id})
                mf_rows = [dict(r) for r in mf_r.mappings().fetchall()]

                # From dms_folder_matches (pending AI suggestions not yet accepted)
                fm_r = await session.execute(text(f"""
                    SELECT id::text, matter_id::text, best_disk_path as folder_path,
                           disk_root, disk_file_count as file_count, accepted,
                           score, folder_path as match_folder_path
                    FROM dms_folder_matches
                    WHERE matter_id IN ({mph})
                      AND trim(tenant_id) = trim(:tid)
                      AND accepted = FALSE
                    ORDER BY score DESC
                """), {**mparams, "tid": tenant_id})
                fm_rows = [dict(r) for r in fm_r.mappings().fetchall()]
            else:
                mf_rows, fm_rows = [], []

            # Index by matter_id
            mappings_by_matter: dict = defaultdict(list)
            for mf in mf_rows:
                mf["best_disk_path"] = mf["folder_path"]
                mappings_by_matter[mf["matter_id"]].append(mf)
            for fm in fm_rows:
                mappings_by_matter[fm["matter_id"]].append(fm)

            # Group matters by client
            matters_by_client: dict = defaultdict(list)
            for m in all_matters:
                m["mappings"] = mappings_by_matter.get(m["id"], [])
                matters_by_client[m["client_id"]].append(m)

            clients_out = []
            for cr in client_rows:
                matters = matters_by_client.get(cr["id"], [])
                mc = sum(1 for m in matters if m["mappings"] and any(x["accepted"] for x in m["mappings"]))
                uc = sum(1 for m in matters if not any(x.get("accepted") for x in m["mappings"]))
                cr["matters"] = matters
                cr["mapped_count"] = mc
                cr["unmapped_count"] = uc
                clients_out.append(cr)

    total_pages = max(1, (total_clients + per_page - 1) // per_page)
    branding = getattr(request.state, "branding", None)
    from fastapi.templating import Jinja2Templates as _J2T
    _templates = _J2T(directory=["core/templates", "modules/connectors/templates"])
    # For folder-first view, also check query param
    view = request.query_params.get("view", "folder")

    return _templates.TemplateResponse(request, "tenant_admin/folder_reconciliation.html", {
        "user": user, "branding": branding,
        "clients": clients_out,
        "page": page, "total_pages": total_pages,
        "total_clients": total_clients, "total_matters": total_matters,
        "mapped_count": mapped_count, "unmapped_count": unmapped_count,
        "unmapped_estimate": max(0, total_matters - mapped_count),
        "per_page": per_page, "q": q, "view": view,
    })


@router.get("/tenant-admin/folder-reconciliation/search-paths")
async def search_folder_paths(request: Request, user=Depends(get_current_user), q: str = ""):
    """Live folder path search for reassignment typeahead."""
    if not q or len(q) < 2:
        return JSONResponse({"paths": []})
    tenant_id = user.tenant_id
    async with AsyncSessionLocal() as session:
        r = await session.execute(text("""
            SELECT DISTINCT
                folder_path,
                disk_root,
                file_count,
                added_at
            FROM matter_folders
            WHERE trim(tenant_id) = trim(:tid)
              AND folder_path ILIKE :q
              AND disk_root IS NOT NULL
            ORDER BY folder_path
            LIMIT 30
        """), {"tid": tenant_id, "q": f"%{q}%"})
        paths = [{"path": row[0], "root": row[1] or "", "file_count": row[2]}
                 for row in r.fetchall()]

        # Also search dms_documents for top-level folder paths not yet in matter_folders
        if len(paths) < 15:
            r2 = await session.execute(text("""
                SELECT DISTINCT
                    SUBSTRING(file_path, LENGTH(folder_root)+2,
                        CASE WHEN POSITION('\\' IN SUBSTRING(file_path, LENGTH(folder_root)+2)) > 0
                        THEN POSITION('\\' IN SUBSTRING(file_path, LENGTH(folder_root)+2))-1
                        ELSE 100 END) as top_folder,
                    folder_root,
                    COUNT(*) as file_count
                FROM dms_documents
                WHERE trim(tenant_id) = trim(:tid)
                  AND SUBSTRING(file_path, LENGTH(folder_root)+2,
                      CASE WHEN POSITION('\\' IN SUBSTRING(file_path, LENGTH(folder_root)+2)) > 0
                      THEN POSITION('\\' IN SUBSTRING(file_path, LENGTH(folder_root)+2))-1
                      ELSE 100 END) ILIKE :q
                GROUP BY top_folder, folder_root
                ORDER BY top_folder
                LIMIT 20
            """), {"tid": tenant_id, "q": f"%{q}%"})
            existing = {p["path"] for p in paths}
            for row in r2.fetchall():
                if row[0] and row[0] not in existing:
                    paths.append({"path": row[0], "root": row[1] or "", "file_count": row[2]})
                    existing.add(row[0])

    return JSONResponse({"paths": paths[:30]})

@router.post("/tenant-admin/folder-reconciliation/accept-batch")
async def accept_folder_match_batch(request: Request, user=Depends(get_current_user)):
    tenant_id = user.tenant_id
    body = await request.json()
    items = body.get("items", [])
    if not items:
        raise HTTPException(status_code=400, detail="No items provided")

    results = []
    errors = []

    async with AsyncSessionLocal() as session:
        try:
            for item in items:
                matter_id      = item.get("matter_id")
                disk_path      = item.get("disk_path")
                disk_root      = item.get("disk_root")
                file_count     = item.get("file_count")
                orig_matter_id = item.get("orig_matter_id", matter_id)

                if not matter_id or not disk_path:
                    errors.append({"matter_id": matter_id, "error": "missing matter_id or disk_path"})
                    continue

                check = await session.execute(text("""
                    SELECT id, matter_name FROM matters
                    WHERE id = CAST(:matter_id AS uuid) AND trim(tenant_id) = trim(:tid)
                """), {"matter_id": matter_id, "tid": tenant_id})
                matter_row = check.mappings().fetchone()
                if not matter_row:
                    errors.append({"matter_id": matter_id, "error": "matter not found"})
                    continue

                await session.execute(text("""
                    INSERT INTO matter_folders (
                        tenant_id, matter_id, folder_path, disk_root, file_count, added_at, added_by
                    ) VALUES (
                        :tid, CAST(:matter_id AS uuid), :disk_path, :disk_root, :file_count, NOW(), :user_id
                    )
                    ON CONFLICT (matter_id, folder_path) DO UPDATE
                        SET disk_root = EXCLUDED.disk_root, file_count = EXCLUDED.file_count,
                            added_at = NOW(), added_by = EXCLUDED.added_by
                """), {"tid": tenant_id, "matter_id": matter_id, "disk_path": disk_path,
                       "disk_root": disk_root, "file_count": file_count, "user_id": user.id})

                await session.execute(text("""
                    UPDATE matters SET folder_path = :disk_path, updated_at = NOW()
                    WHERE id = CAST(:matter_id AS uuid) AND trim(tenant_id) = trim(:tid)
                """), {"disk_path": disk_path, "matter_id": matter_id, "tid": tenant_id})

                await session.execute(text("""
                    UPDATE dms_folder_matches
                    SET accepted = TRUE, best_disk_path = :disk_path, computed_at = NOW()
                    WHERE matter_id = CAST(:orig_matter_id AS uuid) AND trim(tenant_id) = trim(:tid)
                """), {"disk_path": disk_path, "orig_matter_id": orig_matter_id, "tid": tenant_id})

                results.append({"matter_id": matter_id, "matter_name": matter_row["matter_name"],
                                 "folder_path": disk_path})

            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.error("accept_batch error: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    logger.info("import_reconciliation: batch accepted %d folders, %d errors — user=%s",
                len(results), len(errors), user.id)
    return JSONResponse({"status": "ok", "accepted": len(results), "errors": errors, "results": results})


# ---------------------------------------------------------------------------
# Client Merge
# Static sub-paths registered BEFORE the page GET route.
# ---------------------------------------------------------------------------

@router.get("/tenant-admin/client-merge/preview")
async def client_merge_preview(
    request: Request,
    user=Depends(get_current_user),
    keep_id: str = "",
    merge_ids: str = "",   # comma-separated UUIDs
):
    """
    Preview a merge — returns matter counts and matter lists for each client
    being merged, so the user can confirm before committing.
    """
    tenant_id = user.tenant_id
    if not keep_id or not merge_ids:
        raise HTTPException(status_code=400, detail="keep_id and merge_ids required")

    merge_id_list = [m.strip() for m in merge_ids.split(",") if m.strip()]
    if not merge_id_list:
        raise HTTPException(status_code=400, detail="No merge_ids provided")

    async with AsyncSessionLocal() as session:
        # Verify keep client
        keep_result = await session.execute(text("""
            SELECT id::text, client_name, client_number FROM clients
            WHERE id = CAST(:kid AS uuid) AND trim(tenant_id) = trim(:tid) AND is_active = TRUE
        """), {"kid": keep_id, "tid": tenant_id})
        keep_row = keep_result.mappings().fetchone()
        if not keep_row:
            raise HTTPException(status_code=404, detail="Keep client not found")

        # For each merge client, get name + matter list
        merge_clients = []
        total_matters = 0
        for mid in merge_id_list:
            client_result = await session.execute(text("""
                SELECT id::text, client_name, client_number FROM clients
                WHERE id = CAST(:cid AS uuid) AND trim(tenant_id) = trim(:tid) AND is_active = TRUE
            """), {"cid": mid, "tid": tenant_id})
            client_row = client_result.mappings().fetchone()
            if not client_row:
                continue

            matters_result = await session.execute(text("""
                SELECT id::text, matter_name, matter_number FROM matters
                WHERE client_id = CAST(:cid AS uuid) AND trim(tenant_id) = trim(:tid)
                ORDER BY matter_name ASC
            """), {"cid": mid, "tid": tenant_id})
            matters = [dict(r) for r in matters_result.mappings().fetchall()]
            total_matters += len(matters)

            merge_clients.append({
                "id": client_row["id"],
                "name": client_row["client_name"],
                "number": client_row["client_number"],
                "matter_count": len(matters),
                "matters": matters,
            })

    return JSONResponse({
        "keep": {"id": keep_row["id"], "name": keep_row["client_name"], "number": keep_row["client_number"]},
        "merge_clients": merge_clients,
        "total_matters_to_reassign": total_matters,
    })


@router.post("/tenant-admin/client-merge/execute")
async def client_merge_execute(request: Request, user=Depends(get_current_user)):
    """
    Execute a client merge.
    Body: {keep_id, merge_ids: [uuid, ...]}

    For each merge client:
      1. Reassign all matters to keep client
      2. Set clients.is_active = FALSE
      3. Set clients.merged_into_client_id = keep_id
      4. Set clients.merged_at = NOW(), merged_by = user.id

    All changes in a single transaction.
    """
    tenant_id = user.tenant_id
    body = await request.json()
    keep_id = body.get("keep_id")
    merge_ids = body.get("merge_ids", [])

    if not keep_id or not merge_ids:
        raise HTTPException(status_code=400, detail="keep_id and merge_ids required")

    results = []
    errors = []

    async with AsyncSessionLocal() as session:
        try:
            # Verify keep client
            keep_check = await session.execute(text("""
                SELECT id, client_name FROM clients
                WHERE id = CAST(:kid AS uuid) AND trim(tenant_id) = trim(:tid) AND is_active = TRUE
            """), {"kid": keep_id, "tid": tenant_id})
            if not keep_check.fetchone():
                raise HTTPException(status_code=404, detail="Keep client not found or inactive")

            for merge_id in merge_ids:
                # Count matters being reassigned
                count_result = await session.execute(text("""
                    SELECT COUNT(*) FROM matters
                    WHERE client_id = CAST(:cid AS uuid) AND trim(tenant_id) = trim(:tid)
                """), {"cid": merge_id, "tid": tenant_id})
                matter_count = count_result.scalar() or 0

                # Reassign all matters to keep client
                await session.execute(text("""
                    UPDATE matters
                    SET client_id  = CAST(:keep_id AS uuid),
                        updated_at = NOW()
                    WHERE client_id = CAST(:merge_id AS uuid)
                      AND trim(tenant_id) = trim(:tid)
                """), {"keep_id": keep_id, "merge_id": merge_id, "tid": tenant_id})

                # Mark merge client as inactive + record audit trail
                merge_result = await session.execute(text("""
                    UPDATE clients
                    SET is_active              = FALSE,
                        merged_into_client_id  = CAST(:keep_id AS uuid),
                        merged_at              = NOW(),
                        merged_by              = :user_id,
                        updated_at             = NOW()
                    WHERE id = CAST(:merge_id AS uuid)
                      AND trim(tenant_id) = trim(:tid)
                    RETURNING client_name
                """), {"keep_id": keep_id, "merge_id": merge_id,
                       "user_id": user.id, "tid": tenant_id})
                merge_row = merge_result.fetchone()

                results.append({
                    "merged_client_id":   merge_id,
                    "merged_client_name": merge_row[0] if merge_row else "unknown",
                    "matters_reassigned": matter_count,
                })

            await session.commit()

        except HTTPException:
            raise
        except Exception as exc:
            await session.rollback()
            logger.error("client_merge_execute error: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    logger.info("client_merge: keep=%s merged=%s matters=%d user=%s",
                keep_id, merge_ids, sum(r["matters_reassigned"] for r in results), user.id)
    return JSONResponse({
        "status": "ok",
        "keep_id": keep_id,
        "merged": results,
        "total_matters_reassigned": sum(r["matters_reassigned"] for r in results),
    })


@router.get("/tenant-admin/client-merge", response_class=HTMLResponse)
async def client_merge_page(request: Request, user=Depends(get_current_user)):
    """Client Merge screen — select a keep client and one or more clients to merge in."""
    branding = getattr(request.state, "branding", None)

    from fastapi.templating import Jinja2Templates as _J2T
    _templates = _J2T(directory=["core/templates", "modules/connectors/templates"])
    return _templates.TemplateResponse(request, "tenant_admin/client_merge.html", {
        "user": user,
        "branding": branding,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Matter Sync — enqueue two-tier parallel sync (coordinator + per-mapping workers)
#
# ADD to modules/connectors/registry_router.py alongside the other
# /tenant-admin/folder-reconciliation/* routes.
#
# All necessary imports (HTTPException, text, AsyncSessionLocal, Depends,
# Request, JSONResponse, get_current_user, router) are already imported at
# the top of registry_router.py. No new top-level imports needed — the RQ
# and Redis imports are lazy inside the handlers.
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/tenant-admin/folder-reconciliation/sync-matter/{matter_id}")
async def sync_matter_now(
    matter_id: str,
    request: Request,
    user=Depends(get_current_user),
):
    """Enqueue a coordinator job that seeds the matter's folder tree, then
    fans out one sub-job per accepted mapping onto the 'migration' queue.
    Each sub-job runs on PROC-01 with a 4-thread internal pool.

    Returns the parent job_id (= RQ coordinator job_id) for status polling.
    """
    import os
    from redis import Redis
    from rq import Queue

    tenant_id = user.tenant_id

    # Sanity-check matter + count accepted mappings
    async with AsyncSessionLocal() as session:
        check = await session.execute(text("""
            SELECT
                m.matter_name,
                m.matter_number,
                m.matter_type,
                COUNT(mf.id) FILTER (WHERE mf.disk_root IS NOT NULL) as mapping_count
            FROM matters m
            LEFT JOIN matter_folders mf
              ON mf.matter_id = m.id
             AND TRIM(mf.tenant_id) = TRIM(:tid)
            WHERE m.id = CAST(:mid AS uuid)
              AND TRIM(m.tenant_id) = TRIM(:tid)
            GROUP BY m.matter_name, m.matter_number, m.matter_type
        """), {"tid": tenant_id, "mid": matter_id})
        row = check.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Matter not found")

    matter_name, matter_number, matter_type, mapping_count = row

    # NOTE: mapping_count == 0 is still allowed — coordinator will seed the
    # folder tree even if no files are copyable. Change to require mappings
    # if you want to disallow seed-only runs.

    redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
    try:
        q = Queue("migration", connection=Redis.from_url(redis_url))
        job = q.enqueue(
            "modules.dms.jobs.matter_sync.sync_matter_files",
            tenant_id, matter_id,
            job_timeout=3600,
            result_ttl=86400,
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error(
            "sync_matter_now enqueue failed tenant=%s matter=%s: %s",
            tenant_id, matter_id, exc,
        )
        raise HTTPException(status_code=500, detail=f"Queue error: {exc}")

    return JSONResponse({
        "job_id": job.id,
        "matter_id": matter_id,
        "matter_name": matter_name,
        "matter_number": matter_number,
        "matter_type": matter_type,
        "mapping_count": mapping_count,
        "status": "queued",
        "message": (
            f"Sync queued for matter {matter_number} "
            f"({mapping_count} mapped folder(s); folder tree will be seeded first)."
        ),
    })


@router.get("/tenant-admin/folder-reconciliation/sync-status/{job_id}")
async def sync_status(
    job_id: str,
    user=Depends(get_current_user),
):
    """Poll the coordinator job + all sub-jobs, return aggregated progress.

    Reads parent Redis hash at matter_sync:parent:{job_id} plus
    matter_sync:mapping:{job_id}:{idx} for each mapping. Sums counters and
    determines overall status.
    """
    import os
    from redis import Redis
    from rq.job import Job

    redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
    conn = Redis.from_url(redis_url)

    # Coordinator RQ status
    coordinator_status = "unknown"
    coordinator_error = None
    try:
        coord_job = Job.fetch(job_id, connection=conn)
        coordinator_status = coord_job.get_status() or "unknown"
        if coordinator_status == "failed":
            coordinator_error = str(coord_job.exc_info) if coord_job.exc_info else "Unknown"
    except Exception as exc:
        coordinator_error = str(exc)

    # Parent hash
    parent_key = f"matter_sync:parent:{job_id}"
    parent_raw = conn.hgetall(parent_key) or {}
    parent = {
        (k.decode() if isinstance(k, bytes) else k):
        (v.decode() if isinstance(v, bytes) else v)
        for k, v in parent_raw.items()
    }

    if not parent and coordinator_status == "unknown":
        return JSONResponse({
            "job_id": job_id,
            "status": "not_found",
            "error": coordinator_error or "No Redis record and no RQ job",
        })

    mapping_count = int(parent.get("mapping_count", "0"))

    # Per-mapping hashes
    mappings = []
    aggregate = {
        "copied": 0,
        "skipped_existing": 0,
        "skipped_unsupported": 0,
        "errors": 0,
        "ocr_queued": 0,
        "total_bytes": 0,
    }
    mappings_finished = 0
    mappings_failed = 0

    for idx in range(mapping_count):
        mkey = f"matter_sync:mapping:{job_id}:{idx}"
        raw = conn.hgetall(mkey) or {}
        m = {
            (k.decode() if isinstance(k, bytes) else k):
            (v.decode() if isinstance(v, bytes) else v)
            for k, v in raw.items()
        }
        if not m:
            continue

        for field in ("copied", "skipped_existing", "skipped_unsupported",
                      "errors", "ocr_queued", "total_bytes"):
            try:
                aggregate[field] += int(m.get(field, "0") or "0")
            except ValueError:
                pass

        if m.get("status") == "finished":
            mappings_finished += 1
        elif m.get("status") == "failed":
            mappings_failed += 1

        mappings.append({
            "idx": idx,
            "folder_path": m.get("folder_path", ""),
            "disk_root": m.get("disk_root", ""),
            "status": m.get("status", "unknown"),
            "copied": int(m.get("copied", "0") or "0"),
            "errors": int(m.get("errors", "0") or "0"),
            "total_bytes": int(m.get("total_bytes", "0") or "0"),
            "sub_job_id": m.get("sub_job_id", ""),
        })

    # Roll up overall status
    parent_status = parent.get("status", "unknown")
    if coordinator_status in ("failed",):
        overall = "failed"
    elif mapping_count == 0 and parent_status == "finished":
        overall = "finished"
    elif mappings_finished + mappings_failed == mapping_count and mapping_count > 0:
        overall = "finished" if mappings_failed == 0 else "finished_with_errors"
    elif coordinator_status == "queued":
        overall = "queued"
    elif coordinator_status == "started" or parent_status in ("seeding", "running"):
        overall = "running"
    else:
        overall = parent_status or coordinator_status

    return JSONResponse({
        "job_id": job_id,
        "status": overall,
        "coordinator_status": coordinator_status,
        "coordinator_error": coordinator_error,
        "tenant_id": parent.get("tenant_id"),
        "matter_id": parent.get("matter_id"),
        "matter_type_resolved": parent.get("matter_type_resolved"),
        "started_at": parent.get("started_at"),
        "ended_at": parent.get("ended_at"),
        "folders_seeded": int(parent.get("seeded", "0") or "0"),
        "folders_already_present": int(parent.get("existing_folders", "0") or "0"),
        "mapping_count": mapping_count,
        "mappings_finished": mappings_finished,
        "mappings_failed": mappings_failed,
        "aggregate": aggregate,
        "mappings": mappings,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Tenant-wide folder seed — button on reconciliation page header
# ─────────────────────────────────────────────────────────────────────────────




@router.post("/tenant-admin/folder-reconciliation/auto-match")
async def auto_match_folders(user=Depends(get_current_user)):
    """Fuzzy-match disk folder names against matter names/numbers."""
    import os
    tenant_id = user.tenant_id.strip()
    disk_paths = []
    for share in ['/mnt/clients', '/mnt/docsend']:
        if not os.path.exists(share):
            continue
        for client in os.listdir(share):
            cp = os.path.join(share, client)
            if not os.path.isdir(cp):
                continue
            try:
                subs = [s for s in os.listdir(cp) if os.path.isdir(os.path.join(cp, s))]
            except Exception:
                subs = []
            if subs:
                for sub in subs:
                    disk_paths.append((share, client, sub, os.path.join(cp, sub)))
            else:
                disk_paths.append((share, client, None, cp))

    async with AsyncSessionLocal() as db:
        r = await db.execute(text("""
            SELECT m.id::text, m.matter_name, m.matter_number, c.client_name
            FROM matters m
            LEFT JOIN clients c ON c.id = m.client_id
            WHERE trim(m.tenant_id) = :tid
        """), {"tid": tenant_id})
        matters = r.mappings().all()

        matched = 0
        for share, client_folder, matter_folder, full_path in disk_paths:
            client_search = client_folder.lower().strip()
            matter_search = (matter_folder or '').lower().strip()
            best_mid = None
            best_score = 0
            for m in matters:
                mname = (m['matter_name'] or '').lower()
                cname = (m['client_name'] or '').lower()
                mnum = (m['matter_number'] or '').lower()
                score = 0
                if matter_search and matter_search == mname:
                    score = 1.0
                elif matter_search and matter_search == mnum:
                    score = 0.95
                elif matter_search and client_search:
                    c_match = client_search in cname or cname in client_search
                    m_match = matter_search in mname or mname in matter_search
                    if c_match and m_match:
                        score = 0.90
                elif matter_search and matter_search in mname and len(matter_search) >= 4:
                    score = 0.75
                elif matter_search and mname in matter_search and len(mname) >= 4:
                    score = 0.70
                if score > best_score:
                    best_score = score
                    best_mid = str(m['id'])

            if best_mid and best_score >= 0.70:
                fp = f'{client_folder}/{matter_folder}' if matter_folder else client_folder
                await db.execute(text("""
                    INSERT INTO matter_folders
                        (tenant_id, matter_id, folder_path, disk_root, added_at)
                    VALUES (:tid, CAST(:mid AS uuid), :fp, :dr, NOW())
                    ON CONFLICT (matter_id, folder_path)
                    DO UPDATE SET disk_root = :dr
                """), {'tid': tenant_id, 'mid': best_mid, 'fp': fp, 'dr': full_path})
                matched += 1
        await db.commit()

    return JSONResponse({"matched": matched, "total": len(disk_paths)})

@router.post("/tenant-admin/folder-reconciliation/seed-all-matters")
async def seed_all_matters_now(user=Depends(get_current_user)):
    """Seed the standard folder structure for every active matter in the
    tenant that doesn't already have one. Runs on the 'migration' queue
    (PROC-01 only). Idempotent — matters with existing folders are left alone."""
    import os
    from redis import Redis
    from rq import Queue

    tenant_id = user.tenant_id

    async with AsyncSessionLocal() as session:
        res = await session.execute(text("""
            SELECT COUNT(*) FROM matters
            WHERE TRIM(tenant_id) = TRIM(:tid) AND status = 'active'
        """), {"tid": tenant_id})
        active_matter_count = res.scalar() or 0

    if active_matter_count == 0:
        raise HTTPException(status_code=400, detail="No active matters for this tenant")

    redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
    try:
        q = Queue("migration", connection=Redis.from_url(redis_url))
        job = q.enqueue(
            "modules.dms.jobs.folder_seeder.seed_tenant_folders",
            tenant_id,
            job_timeout=3600,
            result_ttl=86400,
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error(
            "seed_all_matters_now enqueue failed tenant=%s: %s", tenant_id, exc)
        raise HTTPException(status_code=500, detail=f"Queue error: {exc}")

    return JSONResponse({
        "job_id": job.id,
        "active_matter_count": active_matter_count,
        "status": "queued",
        "message": f"Folder seed queued for {active_matter_count} active matters",
    })


# ─────────────────────────────────────────────────────────────────────────────
# Client-level folder seed — button on reconciliation page
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/tenant-admin/folder-reconciliation/seed-client-folders/{client_id}")
async def seed_client_folders_now(
    client_id: str,
    user=Depends(get_current_user),
):
    """Seed the standard folder structure for every active matter belonging
    to a client. Runs as an RQ job on the 'migration' queue (PROC-01 only).
    Idempotent — matters with existing folders are left alone."""
    import os
    from redis import Redis
    from rq import Queue

    tenant_id = user.tenant_id

    # Sanity-check client + count active matters
    async with AsyncSessionLocal() as session:
        check = await session.execute(text("""
            SELECT
                c.client_name,
                COUNT(m.id) FILTER (WHERE m.status = 'active') as active_matter_count
            FROM clients c
            LEFT JOIN matters m
              ON m.client_id = c.id
             AND TRIM(m.tenant_id) = TRIM(:tid)
            WHERE c.id = CAST(:cid AS uuid)
              AND TRIM(c.tenant_id) = TRIM(:tid)
            GROUP BY c.client_name
        """), {"tid": tenant_id, "cid": client_id})
        row = check.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Client not found")

    client_name, active_matter_count = row
    if active_matter_count == 0:
        raise HTTPException(
            status_code=400,
            detail="No active matters for this client — nothing to seed",
        )

    redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
    try:
        q = Queue("migration", connection=Redis.from_url(redis_url))
        job = q.enqueue(
            "modules.dms.jobs.folder_seeder.seed_client_folders",
            tenant_id, client_id,
            job_timeout=1800,
            result_ttl=86400,
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error(
            "seed_client_folders_now enqueue failed tenant=%s client=%s: %s",
            tenant_id, client_id, exc,
        )
        raise HTTPException(status_code=500, detail=f"Queue error: {exc}")

    return JSONResponse({
        "job_id": job.id,
        "client_id": client_id,
        "client_name": client_name,
        "active_matter_count": active_matter_count,
        "status": "queued",
        "message": (
            f"Folder seed queued for {client_name}: "
            f"{active_matter_count} active matter(s) will get standard structure."
        ),
    })


@router.get("/tenant-admin/folder-reconciliation/seed-status/{job_id}")
async def seed_status(
    job_id: str,
    user=Depends(get_current_user),
):
    """Poll a seed_client_folders RQ job. Returns status + aggregated stats
    when complete."""
    import os
    from redis import Redis
    from rq.job import Job

    redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
    try:
        conn = Redis.from_url(redis_url)
        job = Job.fetch(job_id, connection=conn)
    except Exception as exc:
        return JSONResponse({"job_id": job_id, "status": "not_found", "error": str(exc)})

    status = job.get_status()
    payload = {
        "job_id": job_id,
        "status": status,
        "enqueued_at": job.enqueued_at.isoformat() if job.enqueued_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "ended_at": job.ended_at.isoformat() if job.ended_at else None,
    }
    if status == "finished":
        result = job.result or {}
        payload["result"] = result
        payload["summary"] = {
            "matters_processed": result.get("matters_processed", 0),
            "total_created": result.get("total_created", 0),
            "total_existing": result.get("total_existing", 0),
            "total_errors": result.get("total_errors", 0),
        }
    elif status == "failed":
        payload["error"] = str(job.exc_info) if job.exc_info else "Unknown error"

    return JSONResponse(payload)
