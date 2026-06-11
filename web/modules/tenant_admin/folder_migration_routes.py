"""
Folder Migration / Sync Report — route module v5.
Pre-aggregated queries, HTMX batch rendering, inline assign with sync.
"""
import os, logging
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text as sa_text

log = logging.getLogger("app")
LEGACY_ROOT = "/mnt/legacy-qnap/D Drive Backup/Clients"
PRAESIDIUM_ROOT = "/mnt/praesidium"
PAGE_SIZE = 50


def _esc(s):
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;").replace("'","&#39;")


def _build_row(fn, fp, lc, match, pc, status, synced_at):
    """Build one HTML table row."""
    bg = {"synced":"#F0FDF4","partial":"#FFFBEB","unsynced":"#FEF2F2"}.get(status,"")
    badge = {
        "synced": '<span class="badge badge-green" style="font-size:10px;">✓ Synced</span>',
        "partial": '<span class="badge" style="font-size:10px;background:#FEF3C7;color:#92400E;">⚠ Partial</span>',
        "unsynced": '<span class="badge" style="font-size:10px;background:#FEE2E2;color:#991B1B;">✕ Not Synced</span>',
        "unmatched": '<span class="badge badge-gray" style="font-size:10px;">— Unmatched</span>',
    }.get(status, "")

    if match and match.get("matter_name"):
        sc = f'{match["score"]*100:.0f}%' if match.get("score") else ""
        acc = ' · <span style="color:#166534;">Accepted</span>' if match.get("accepted") else ""
        matter_html = (f'<span style="font-weight:500;">{_esc(match["matter_name"])}</span>'
                      f'<span style="font-size:10px;color:var(--muted);"> · {_esc(match.get("client_name") or "")}</span>'
                      f'<div style="font-size:10px;color:var(--muted);">{sc}{acc}</div>')
    else:
        matter_html = '<span style="color:#9CA3AF;font-style:italic;font-size:11px;">No match</span>'

    if pc > 0:
        miss = lc - pc
        miss_h = f' <span style="font-size:10px;color:#B45309;">({miss} missing)</span>' if miss > 0 else ""
        prae_html = f'<span style="color:#166534;">{pc}</span>{miss_h}'
    else:
        prae_html = '<span style="color:#9CA3AF;">—</span>'

    fe = _esc(fp)
    ne = _esc(fn)

    # Actions by status
    if match and match.get("matter_id") and match.get("accepted"):
        actions = (f'<button onclick="syncFolder(\'{match["match_id"]}\',\'{match["matter_id"]}\',\'smart\')" '
                  f'class="btn-primary" style="padding:2px 8px;font-size:10px;">🗂 Smart</button>'
                  f'<button onclick="syncFolder(\'{match["match_id"]}\',\'{match["matter_id"]}\',\'raw\')" '
                  f'class="btn-secondary" style="padding:2px 8px;font-size:10px;">📋 Raw</button>')
    elif match and match.get("matter_id") and not match.get("accepted"):
        actions = (f'<button onclick="acceptAndSync(\'{match["match_id"]}\',\'{match["matter_id"]}\',\'smart\')" '
                  f'class="btn-primary" style="padding:2px 8px;font-size:10px;background:#166534;">✓ Accept</button>')
    else:
        # Inline assign with typeahead + sync mode
        rid = ne.replace(' ','_').replace(',','_').replace("'","_").replace('&amp;','_').replace('(','').replace(')','')
        actions = (
            f'<div style="display:flex;gap:3px;align-items:center;flex-wrap:wrap;position:relative;" id="arow-{rid}">'
            f'<input type="text" id="ta-{rid}" placeholder="Search matter..." '
            f'oninput="_taSearch(this,\'{rid}\')" onfocus="_taSearch(this,\'{rid}\')" '
            f'onblur="setTimeout(function(){{document.getElementById(\'td-{rid}\').style.display=\'none\'}},200)" '
            f'style="width:120px;padding:2px 6px;font-size:10px;border:1px solid var(--border);border-radius:3px;">'
            f'<input type="hidden" id="tv-{rid}">'
            f'<div id="td-{rid}" style="display:none;position:absolute;top:100%;left:0;z-index:50;background:#fff;'
            f'border:1px solid var(--border);border-radius:4px;box-shadow:0 4px 12px rgba(0,0,0,0.1);'
            f'max-height:180px;overflow-y:auto;font-size:11px;min-width:240px;"></div>'
            f'<select id="sm-{rid}" style="padding:1px 4px;font-size:9px;border:1px solid var(--border);border-radius:3px;background:#fff;">'
            f'<option value="smart">Smart</option><option value="raw">Raw</option><option value="none">No sync</option></select>'
            f'<button onclick="_inlineAssign(\'{rid}\',\'{fe}\')" class="btn-primary" style="padding:2px 6px;font-size:10px;">Assign</button>'
            f'<button onclick="openAssignModal(\'{ne}\',\'{fe}\')" class="btn-secondary" style="padding:2px 6px;font-size:10px;">+ New</button>'
            f'</div>'
        )

    sat_h = f'<div style="font-size:9px;color:var(--muted);margin-top:1px;">{synced_at}</div>' if synced_at else ""

    return (f'<tr class="folder-row" data-name="{ne.lower()}" style="border-bottom:1px solid var(--border);'
            f'{"background:"+bg+";" if bg else ""}">'
            f'<td style="padding:5px 8px;"><div style="font-size:12px;font-weight:500;">{ne}</div>'
            f'<div style="font-size:10px;color:var(--muted);margin-top:1px;max-width:340px;overflow:hidden;'
            f'text-overflow:ellipsis;white-space:nowrap;" title="{fe}">{fe}</div></td>'
            f'<td style="padding:5px 8px;font-size:12px;">{matter_html}</td>'
            f'<td style="padding:5px 8px;text-align:center;font-size:12px;font-weight:500;">{lc}</td>'
            f'<td style="padding:5px 8px;text-align:center;font-size:12px;font-weight:500;">{prae_html}</td>'
            f'<td style="padding:5px 8px;text-align:center;">{badge}{sat_h}</td>'
            f'<td style="padding:5px 8px;">{actions}</td>'
            f'</tr>')


def register_folder_migration_routes(router, AsyncSessionLocal, _unused=None):

    def _t(request):
        from app import templates
        return templates

    def _tid(request):
        tid = getattr(request.state, "tenant_id", None)
        if tid:
            return tid.strip()
        return (request.query_params.get("tenant_id") or "").strip()

    async def _get_prae_counts(session, tid):
        rows = (await session.execute(sa_text("""
            SELECT folder_root, COUNT(*) as cnt FROM dms_documents
            WHERE TRIM(tenant_id) = :tid AND source = 'matter_sync' AND folder_root LIKE '/mnt/praesidium/%'
            GROUP BY folder_root
        """), {"tid": tid})).mappings().fetchall()
        return {r["folder_root"]: r["cnt"] for r in rows}

    async def _get_matches(session, tid):
        rows = (await session.execute(sa_text("""
            SELECT fm.id::text as match_id, fm.matter_id::text,
                   fm.folder_path, fm.best_disk_path, fm.score, fm.accepted,
                   fm.synced_at, fm.synced_file_count, fm.disk_file_count,
                   m.matter_name, c.client_name
            FROM dms_folder_matches fm
            LEFT JOIN matters m ON fm.matter_id = m.id
            LEFT JOIN clients c ON m.client_id = c.id AND TRIM(c.tenant_id) = TRIM(m.tenant_id)
            WHERE TRIM(fm.tenant_id) = :tid
        """), {"tid": tid})).mappings().fetchall()
        by_path = {}
        for r in rows:
            bp = r["best_disk_path"] or ""
            if bp:
                by_path[bp] = dict(r)
        return by_path

    def _prae_count(match, prae_counts, tid):
        if not match: return 0
        pc = match.get("synced_file_count") or 0
        mn, cn = match.get("matter_name"), match.get("client_name")
        if mn and cn:
            pc = max(pc, prae_counts.get(f"{PRAESIDIUM_ROOT}/{tid}/matters/{cn}/{mn}", 0))
        return pc

    def _status(match, lc, pc):
        if not match: return "unmatched"
        if pc > 0 and pc >= lc: return "synced"
        if pc > 0: return "partial"
        return "unsynced"

    # ── Main page ─────────────────────────────────────────────────────
    @router.get("/folder-migration", response_class=HTMLResponse)
    async def folder_migration_report(request: Request):
        tid = _tid(request)
        filt = request.query_params.get("filter", "all")

        async with AsyncSessionLocal() as session:
            prae_counts = await _get_prae_counts(session, tid)
            matches = await _get_matches(session, tid)
            cr = (await session.execute(sa_text("""
                SELECT COUNT(*) as cnt FROM file_inventory
                WHERE TRIM(tenant_id) = :tid AND entry_type = 'folder' AND depth = 1 AND full_path LIKE :p
            """), {"tid": tid, "p": LEGACY_ROOT + "%"})).fetchone()
            total = cr[0] if cr else 0

        summary = {"total_folders": total, "synced": 0, "partial": 0, "unsynced": 0, "unmatched": 0}
        for m in matches.values():
            pc = _prae_count(m, prae_counts, tid)
            lc = m.get("disk_file_count") or 0
            st = _status(m, lc, pc)
            if st in summary: summary[st] += 1
        summary["unmatched"] = total - len(matches)

        recon = None
        try:
            async with AsyncSessionLocal() as session:
                rr = (await session.execute(sa_text("""
                    SELECT run_id::text, created_at,
                        COUNT(*) FILTER (WHERE issue_type = 'orphan') as orphans,
                        COUNT(*) FILTER (WHERE issue_type = 'ghost') as ghosts,
                        COUNT(*) FILTER (WHERE issue_type = 'size_mismatch') as mismatches
                    FROM praesidium_reconciliation WHERE TRIM(tenant_id) = :tid
                    GROUP BY run_id, created_at ORDER BY created_at DESC LIMIT 1
                """), {"tid": tid})).mappings().fetchone()
                if rr: recon = dict(rr)
        except Exception: pass

        return _t(request).TemplateResponse(request, "tenant_admin/folder_migration.html", {
            "tenant_id": tid, "summary": summary, "current_filter": filt,
            "recon": recon, "brand": None, "page_size": PAGE_SIZE,
        })

    # ── Batch ─────────────────────────────────────────────────────────
    @router.get("/folder-migration/batch", response_class=HTMLResponse)
    async def folder_migration_batch(request: Request):
        tid = _tid(request)
        filt = request.query_params.get("filter", "all")
        offset = int(request.query_params.get("offset", "0"))
        limit = int(request.query_params.get("limit", str(PAGE_SIZE)))

        async with AsyncSessionLocal() as session:
            prae_counts = await _get_prae_counts(session, tid)
            matches = await _get_matches(session, tid)
            rows = (await session.execute(sa_text("""
                SELECT full_path, root_folder as folder_name, child_file_count as legacy_count
                FROM file_inventory WHERE TRIM(tenant_id) = :tid AND entry_type = 'folder'
                AND depth = 1 AND full_path LIKE :p ORDER BY root_folder
            """), {"tid": tid, "p": LEGACY_ROOT + "%"})).mappings().fetchall()

        filtered = []
        for r in rows:
            fp = r["full_path"]
            match = matches.get(fp)
            lc = r["legacy_count"] or 0
            if lc == 0 and match and match.get("disk_file_count"):
                lc = match["disk_file_count"]
            pc = _prae_count(match, prae_counts, tid)
            st = _status(match, lc, pc)
            if filt != "all" and st != filt: continue
            filtered.append((r, match, lc, pc, st))

        page = filtered[offset:offset+limit]
        html = []
        for r, match, lc, pc, st in page:
            sa = ""
            if match and match.get("synced_at"):
                try: sa = match["synced_at"].strftime("%Y-%m-%d %H:%M")
                except: pass
            html.append(_build_row(r["folder_name"] or "", r["full_path"] or "", lc, match, pc, st, sa))

        if len(page) >= limit and (offset + limit) < len(filtered):
            noff = offset + limit
            html.append(f'<tr hx-get="/tenant-admin/folder-migration/batch?tenant_id={tid}&filter={filt}&offset={noff}&limit={limit}" '
                        f'hx-trigger="revealed" hx-swap="afterend" hx-target="this">'
                        f'<td colspan="6" style="padding:12px;text-align:center;color:var(--muted);font-size:11px;">Loading more...</td></tr>')

        return HTMLResponse("\n".join(html))

    # ── Search matters ────────────────────────────────────────────────
    @router.get("/folder-migration/search-matters")
    async def fm_search_matters(request: Request):
        tid = _tid(request)
        q = request.query_params.get("q", "").strip()
        if len(q) < 2: return JSONResponse({"matters": []})
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(sa_text("""
                SELECT m.id::text, m.matter_name as name, m.matter_number as number, c.client_name as client
                FROM matters m LEFT JOIN clients c ON m.client_id = c.id AND TRIM(c.tenant_id) = TRIM(m.tenant_id)
                WHERE TRIM(m.tenant_id) = :tid AND (m.matter_name ILIKE :q OR c.client_name ILIKE :q OR m.matter_number ILIKE :q)
                ORDER BY c.client_name, m.matter_name LIMIT 20
            """), {"tid": tid, "q": f"%{q}%"})).mappings().fetchall()
        return JSONResponse({"matters": [dict(r) for r in rows]})

    # ── Sync ──────────────────────────────────────────────────────────
    @router.post("/folder-migration/sync")
    async def fm_sync(request: Request):
        body = await request.json()
        tid = _tid(request)
        matter_id = body.get("matter_id", ""); match_id = body.get("match_id", ""); sync_mode = body.get("sync_mode", "smart")
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("UPDATE dms_folder_matches SET accepted = true WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"), {"mid": match_id, "tid": tid})
            await session.commit()
        async with AsyncSessionLocal() as session:
            mr = (await session.execute(sa_text("""
                SELECT fm.folder_path, fm.best_disk_path, m.matter_name, c.client_name
                FROM dms_folder_matches fm LEFT JOIN matters m ON fm.matter_id = m.id
                LEFT JOIN clients c ON m.client_id = c.id AND TRIM(c.tenant_id) = TRIM(m.tenant_id)
                WHERE fm.id = CAST(:mid AS uuid) AND TRIM(fm.tenant_id) = :tid
            """), {"mid": match_id, "tid": tid})).mappings().fetchone()
        if not mr: return JSONResponse({"status": "error", "detail": "Match not found"}, status_code=404)
        import importlib.util, sys, asyncio, uuid as _uuid
        if "matter_sync_fm" in sys.modules: del sys.modules["matter_sync_fm"]
        spec = importlib.util.spec_from_file_location("matter_sync_fm", "/app/modules/dms/jobs/matter_sync.py")
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        try:
            r = await asyncio.to_thread(mod.sync_mapping_files, tenant_id=tid, matter_id=matter_id,
                folder_path=mr["folder_path"] or "", disk_root=mr["best_disk_path"] or "",
                parent_job_id=str(_uuid.uuid4()), mapping_idx=0,
                legacy_source_path=mr["folder_path"] or "", skip_remap=(sync_mode == "raw"))
        except Exception as e:
            return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)
        copied = r.get("copied", 0); skipped = r.get("skipped", 0); errors = r.get("errors", 0)
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("UPDATE dms_folder_matches SET synced_at = NOW(), synced_file_count = :fc WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"),
                                  {"mid": match_id, "tid": tid, "fc": copied + skipped})
            await session.commit()
        return JSONResponse({"status": "ok", "message": f"{copied} copied, {skipped} existing, {errors} errors ({sync_mode} mode)"})

    @router.post("/folder-migration/accept-and-sync")
    async def fm_accept_and_sync(request: Request):
        body = await request.json(); tid = _tid(request); match_id = body.get("match_id", "")
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("UPDATE dms_folder_matches SET accepted = true WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"), {"mid": match_id, "tid": tid})
            await session.commit()
        return await fm_sync(request)

    @router.post("/folder-migration/assign")
    async def fm_assign(request: Request):
        body = await request.json(); tid = _tid(request)
        legacy_path = body.get("legacy_path", ""); sync_mode = body.get("sync_mode", "none"); matter_id = body.get("matter_id")
        if not matter_id:
            nc = body.get("new_client", "").strip(); nm = body.get("new_matter", "").strip()
            if not nc or not nm: return JSONResponse({"status": "error", "detail": "Client and matter name required"}, status_code=400)
            async with AsyncSessionLocal() as session:
                ex = (await session.execute(sa_text("SELECT id::text FROM clients WHERE TRIM(tenant_id) = :tid AND client_name ILIKE :cn LIMIT 1"), {"tid": tid, "cn": nc})).fetchone()
                cid = ex[0] if ex else (await session.execute(sa_text("INSERT INTO clients (id, tenant_id, client_name) VALUES (gen_random_uuid(), :tid, :cn) RETURNING id::text"), {"tid": tid, "cn": nc})).fetchone()[0]
                matter_id = (await session.execute(sa_text("INSERT INTO matters (id, tenant_id, client_id, matter_name, status) VALUES (gen_random_uuid(), :tid, CAST(:cid AS uuid), :mn, 'active') RETURNING id::text"), {"tid": tid, "cid": cid, "mn": nm})).fetchone()[0]
                await session.commit()
        fn = os.path.basename(legacy_path)
        async with AsyncSessionLocal() as session:
            match_id = (await session.execute(sa_text("""
                INSERT INTO dms_folder_matches (id, tenant_id, matter_id, folder_path, best_disk_path, score, accepted, computed_at)
                VALUES (gen_random_uuid(), :tid, CAST(:mid AS uuid), :fp, :bp, 1.0, true, NOW())
                ON CONFLICT (tenant_id, matter_id) DO UPDATE SET folder_path = EXCLUDED.folder_path, best_disk_path = EXCLUDED.best_disk_path, accepted = true
                RETURNING id::text"""), {"tid": tid, "mid": matter_id, "fp": fn, "bp": legacy_path})).fetchone()[0]
            await session.commit()
        if sync_mode == "none": return JSONResponse({"status": "ok", "message": "Assigned. No sync."})
        body["match_id"] = match_id; body["matter_id"] = matter_id
        return await fm_sync(request)

    return router
