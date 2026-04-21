"""
patch_folder_reconciliation.py
Adds /search-paths endpoint and updates the GET route to supply counts.
Run: docker exec praesidium-web python /tmp/patch_folder_reconciliation.py
"""

content = open('/app/modules/connectors/registry_router.py').read()
changes = []

# ── 1. Add search-paths endpoint before accept-batch ────────────────────
search_paths_endpoint = '''
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
                        CASE WHEN POSITION('\\\\' IN SUBSTRING(file_path, LENGTH(folder_root)+2)) > 0
                        THEN POSITION('\\\\' IN SUBSTRING(file_path, LENGTH(folder_root)+2))-1
                        ELSE 100 END) as top_folder,
                    folder_root,
                    COUNT(*) as file_count
                FROM dms_documents
                WHERE trim(tenant_id) = trim(:tid)
                  AND SUBSTRING(file_path, LENGTH(folder_root)+2,
                      CASE WHEN POSITION('\\\\' IN SUBSTRING(file_path, LENGTH(folder_root)+2)) > 0
                      THEN POSITION('\\\\' IN SUBSTRING(file_path, LENGTH(folder_root)+2))-1
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

'''

old_accept = '@router.post("/tenant-admin/folder-reconciliation/accept-batch")'
if old_accept in content:
    content = content.replace(old_accept, search_paths_endpoint + old_accept)
    changes.append('search-paths endpoint added')
else:
    changes.append('accept-batch NOT FOUND for search-paths insertion')

# ── 2. Add counts to the GET route context ────────────────────────────────
old_context = '''    branding = getattr(request.state, "branding", None)
    from fastapi.templating import Jinja2Templates as _J2T
    _templates = _J2T(directory=["core/templates", "modules/connectors/templates"])
    return _templates.TemplateResponse(request, "tenant_admin/folder_reconciliation.html", {
        "user": user, "branding": branding, "rows": rows,
        "page": page, "total_pages": total_pages, "total": total,
        "filter": filter, "q": q, "per_page": per_page,
    })'''

new_context = '''    # Counts for header stats
    async with AsyncSessionLocal() as session2:
        stats = await session2.execute(text("""
            SELECT
                COUNT(*) as total,
                COUNT(CASE WHEN score >= 0.7 THEN 1 END) as high_count,
                COUNT(CASE WHEN score >= 0.4 AND score < 0.7 THEN 1 END) as medium_count,
                COUNT(CASE WHEN score < 0.4 THEN 1 END) as low_count,
                COUNT(CASE WHEN accepted = TRUE THEN 1 END) as accepted_count
            FROM dms_folder_matches
            WHERE trim(tenant_id) = trim(:tid)
        """), {"tid": tenant_id})
        s = stats.fetchone()
        stat_total    = s[0] or 0
        stat_high     = s[1] or 0
        stat_medium   = s[2] or 0
        stat_low      = s[3] or 0
        stat_accepted = s[4] or 0

    branding = getattr(request.state, "branding", None)
    from fastapi.templating import Jinja2Templates as _J2T
    _templates = _J2T(directory=["core/templates", "modules/connectors/templates"])
    return _templates.TemplateResponse(request, "tenant_admin/folder_reconciliation.html", {
        "user": user, "branding": branding, "rows": rows,
        "page": page, "total_pages": total_pages, "total": total,
        "filter": filter, "q": q, "per_page": per_page,
        "high_count": stat_high, "medium_count": stat_medium,
        "low_count": stat_low, "accepted_count": stat_accepted,
    })'''

if old_context in content:
    content = content.replace(old_context, new_context)
    changes.append('counts added to GET route context')
else:
    changes.append('GET context NOT FOUND')

# ── 3. Ensure JSONResponse is imported ───────────────────────────────────
if 'from fastapi.responses import' in content and 'JSONResponse' not in content:
    content = content.replace(
        'from fastapi.responses import',
        'from fastapi.responses import JSONResponse,'
    )
    changes.append('JSONResponse import added')
elif 'JSONResponse' not in content:
    content = content.replace(
        'from fastapi import',
        'from fastapi.responses import JSONResponse\nfrom fastapi import'
    )
    changes.append('JSONResponse import added (new line)')

open('/app/modules/connectors/registry_router.py', 'w').write(content)
print(f'Done. Changes:')
for c in changes:
    print(f'  {c}')
