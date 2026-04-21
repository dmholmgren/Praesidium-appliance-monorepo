"""
patch_fr2.py
Replaces the folder-reconciliation GET route with nested client/matter structure
and adds /browse and /accept-all and /remove-mapping endpoints.
Run: docker exec praesidium-web python /tmp/patch_fr2.py
"""
import re

content = open('/app/modules/connectors/registry_router.py').read()
changes = []

# ── 1. Replace the GET route ───────────────────────────────────────────────
old_get_start = '@router.get("/tenant-admin/folder-reconciliation", response_class=HTMLResponse)'
old_get_end   = "    return _templates.TemplateResponse(request, \"tenant_admin/folder_reconciliation.html\","

# Find the full GET route block
start_idx = content.find(old_get_start)
end_idx   = content.find('\n@router', start_idx + 10)
if start_idx == -1:
    print('GET route NOT FOUND')
else:
    new_get = '''@router.get("/tenant-admin/folder-reconciliation", response_class=HTMLResponse)
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
    return _templates.TemplateResponse(request, "tenant_admin/folder_reconciliation.html", {
        "user": user, "branding": branding,
        "clients": clients_out,
        "page": page, "total_pages": total_pages,
        "total_clients": total_clients, "total_matters": total_matters,
        "mapped_count": mapped_count, "unmapped_count": unmapped_count,
        "per_page": per_page, "q": q,
    })

'''
    content = content[:start_idx] + new_get + content[end_idx:]
    changes.append('GET route replaced')

# ── 2. Add /browse endpoint ────────────────────────────────────────────────
browse_endpoint = '''
@router.get("/tenant-admin/folder-reconciliation/browse")
async def browse_folders(
    request: Request, user=Depends(get_current_user),
    share: str = "Clients", prefix: str = "",
):
    """Proxy the CIFS bridge folder list for the browser modal."""
    import httpx as _httpx
    CIFS_URL = "http://10.10.60.13:8080"
    tenant_id = user.tenant_id
    try:
        async with _httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{CIFS_URL}/api/v1/files/list",
                params={"tenant_id": tenant_id, "prefix": prefix, "recursive": "false"})
        if resp.status_code != 200:
            return JSONResponse({"folders": []})
        data = resp.json()
        folders = [
            {"path": f["path"], "name": f["name"], "modified_at": f.get("modified_at","")}
            for f in data.get("files", [])
            if f.get("is_directory")
        ]
        return JSONResponse({"folders": folders})
    except Exception as exc:
        import logging; logging.getLogger(__name__).error("browse_folders: %s", exc)
        return JSONResponse({"folders": [], "error": str(exc)})


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

'''

# Insert before the clients endpoint
target = '@router.get("/tenant-admin/folder-reconciliation/clients")'
if target in content:
    content = content.replace(target, browse_endpoint + target)
    changes.append('browse/accept-all/remove-mapping endpoints added')
else:
    # Append before end
    content += browse_endpoint
    changes.append('endpoints appended at end')

# ── 3. Ensure JSONResponse imported ────────────────────────────────────────
if 'JSONResponse' not in content:
    content = content.replace('from fastapi import', 'from fastapi.responses import JSONResponse\nfrom fastapi import', 1)
    changes.append('JSONResponse import added')

open('/app/modules/connectors/registry_router.py', 'w').write(content)
print('Done:')
for c in changes: print(' ', c)
