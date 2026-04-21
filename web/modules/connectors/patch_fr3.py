"""
patch_fr3.py
Adds endpoints for folder-first reconciliation:
  GET /tenant-admin/folder-reconciliation/mapped-index
  GET /tenant-admin/folder-reconciliation/search-matters
  GET /tenant-admin/folder-reconciliation/matter-mappings/{matter_id}
Updates GET /tenant-admin/folder-reconciliation to supply new context vars.
Run: docker exec praesidium-web python /tmp/patch_fr3.py
"""

content = open('/app/modules/connectors/registry_router.py').read()
changes = []

# ── 1. Update the GET route to supply mapped_count and unmapped_estimate ──
old_return = '''    return _templates.TemplateResponse(request, "tenant_admin/folder_reconciliation.html", {
        "user": user, "branding": branding,
        "clients": clients_out,
        "page": page, "total_pages": total_pages,
        "total_clients": total_clients, "total_matters": total_matters,
        "mapped_count": mapped_count, "unmapped_count": unmapped_count,
        "per_page": per_page, "q": q,
    })'''

new_return = '''    # For folder-first view, also check query param
    view = request.query_params.get("view", "folder")

    return _templates.TemplateResponse(request, "tenant_admin/folder_reconciliation.html", {
        "user": user, "branding": branding,
        "clients": clients_out,
        "page": page, "total_pages": total_pages,
        "total_clients": total_clients, "total_matters": total_matters,
        "mapped_count": mapped_count, "unmapped_count": unmapped_count,
        "unmapped_estimate": max(0, total_matters - mapped_count),
        "per_page": per_page, "q": q, "view": view,
    })'''

if old_return in content:
    content = content.replace(old_return, new_return)
    changes.append('GET route updated with unmapped_estimate')
else:
    changes.append('GET return NOT FOUND - skipping')

# ── 2. Add new endpoints ──────────────────────────────────────────────────
new_endpoints = '''

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

'''

# Insert before the clients endpoint
target = '@router.get("/tenant-admin/folder-reconciliation/clients")'
if target in content:
    content = content.replace(target, new_endpoints + target)
    changes.append('new endpoints added')
else:
    content += new_endpoints
    changes.append('endpoints appended at end')

open('/app/modules/connectors/registry_router.py', 'w').write(content)
print('Done:')
for c in changes:
    print(' ', c)
